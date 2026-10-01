"""Pick the most favorite sessions an attendee can really get to, day by day.

Rules:
  - Keep as many sessions as possible. Sessions marked "must attend" come
    first: one of them outweighs all the other sessions together.
  - Kept sessions may not overlap.
  - Each day has a limit on venues: 1 means stay at one venue, 2 allows one
    move between venues, 3 allows two moves, and so on. Going back to a venue
    counts as a move too. The first trip of the day (to the first venue) is
    free.
  - A move from venue X to venue Y needs travel_minutes(X, Y) between the end
    of the last session at X and the start of the first session at Y.
  - Ties: prefer fewer moves, then less travel, then an earlier finish.

Usage:
    sessions = [PlannedSession.from_session(session) for session in favorites]
    plans = optimize_schedule(sessions, max_venues_by_day={"2026-12-01": 1})
    for day, plan in plans.items():
        print(day, plan.session_ids())
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aws_events.catalog import session_date, session_start_time


MGM_GRAND = "MGM Grand"
CAESARS_FORUM = "Caesars Forum"
CAESARS_PALACE = "Caesars Palace"
VENETIAN = "Venetian"
WYNN_ENCORE = "Wynn/Encore"

# Travel times in minutes, buffer included, for each pair of venues.
# AWS asks attendees to allow 30-45 minutes between venues. It runs no shuttle
# between Venetian and Wynn/Encore or Caesars Forum, as those are short walks.
# Sessions start on the hour or half hour, so in practice up to 30 minutes
# means "the next slot" and 31-60 means "the slot an hour later".
WALK_MINUTES = 30
SHUTTLE_MINUTES = 45
MGM_GRAND_MINUTES = 60
TRAVEL_MINUTES = {
    frozenset({VENETIAN, WYNN_ENCORE}): WALK_MINUTES,
    frozenset({VENETIAN, CAESARS_FORUM}): WALK_MINUTES,
    # Not adjacent: the walk passes the Venetian.
    frozenset({WYNN_ENCORE, CAESARS_FORUM}): SHUTTLE_MINUTES,
    # Estimated as a 15-20 minute walk; not confirmed by AWS.
    frozenset({CAESARS_PALACE, VENETIAN}): WALK_MINUTES,
    frozenset({CAESARS_PALACE, CAESARS_FORUM}): WALK_MINUTES,
    frozenset({CAESARS_PALACE, WYNN_ENCORE}): SHUTTLE_MINUTES,
    # MGM Grand is at the south end: 20-45 minutes by shuttle, plus waiting.
    frozenset({MGM_GRAND, VENETIAN}): MGM_GRAND_MINUTES,
    frozenset({MGM_GRAND, WYNN_ENCORE}): MGM_GRAND_MINUTES,
    frozenset({MGM_GRAND, CAESARS_FORUM}): MGM_GRAND_MINUTES,
    frozenset({MGM_GRAND, CAESARS_PALACE}): MGM_GRAND_MINUTES,
}
# Venues missing from the table are assumed to be the longest trip away.
UNKNOWN_VENUE_MINUTES = 60

MIN_VENUES_PER_DAY = 1
MAX_VENUES_PER_DAY = 5
DEFAULT_VENUES_PER_DAY = 2

# The catalog gives every timed session a length; this covers any that don't.
DEFAULT_SESSION_MINUTES = 60


def travel_minutes(from_venue, to_venue):
    """Return the minutes needed to get from one venue to another."""
    if from_venue == to_venue:
        return 0
    pair = frozenset({from_venue, to_venue})
    return TRAVEL_MINUTES.get(pair, UNKNOWN_VENUE_MINUTES)


def session_venue(session):
    """Return a session's venue, or None if it has none.

    Many sessions leave "venue" empty but name it at the start of the room,
    as in "Wynn/Encore | Level 1 | Encore Ballroom", so that is used instead.
    """
    if session.get("venue"):
        return session["venue"]
    room = session.get("room") or ""
    venue = room.split("|")[0].strip()
    return venue or None


@dataclass(frozen=True)
class PlannedSession:
    """A session with the times and venue the optimizer needs.

    Attributes:
        session_id: The session's ID.
        venue: Where it takes place, or None if unknown.
        start: Local start time.
        end: Local end time.
        must_attend: Whether the attendee insists on keeping it.
    """

    session_id: str
    venue: str
    start: datetime
    end: datetime
    must_attend: bool = False

    @classmethod
    def from_session(cls, session, must_attend=False):
        """Build a PlannedSession from a catalog session dict.

        Returns:
            The PlannedSession, or None if the session has no date or start time
            and so can't be planned.
        """
        day = session_date(session)
        start_time = session_start_time(session)
        if not day or not start_time:
            return None
        start = datetime.fromisoformat(f"{day}T{start_time}")
        length = (session.get("sessionTime") or {}).get("length")
        minutes = int(length) if length else DEFAULT_SESSION_MINUTES
        end = start + timedelta(minutes=minutes)
        return cls(session["sessionId"], session_venue(session), start, end,
                   must_attend)

    def day(self):
        """Return the day the session starts, as "YYYY-MM-DD"."""
        return self.start.date().isoformat()


@dataclass
class DayPlan:
    """The sessions kept for one day.

    Attributes:
        sessions: The kept PlannedSessions, in time order.
        moves: (from_venue, to_venue) for each move between venues.
        travel_minutes: Total minutes spent on those moves.
        missed_must_attend: IDs of must-attend sessions that could not fit.
    """

    sessions: list = field(default_factory=list)
    moves: list = field(default_factory=list)
    travel_minutes: int = 0
    missed_must_attend: list = field(default_factory=list)

    def session_ids(self):
        """Return the IDs of the kept sessions, in time order."""
        return [session.session_id for session in self.sessions]


@dataclass
class _Step:
    """The best way found to end a run of sessions with one particular session.

    Following `previous` back to None gives the whole run.
    """

    session: PlannedSession
    score: int
    moves: int
    travel: int
    previous: "_Step" = None

    def rank(self):
        """Return a key where higher is better: score, then fewer moves, less travel."""
        return (self.score, -self.moves, -self.travel)

    def extend(self, session, weight):
        """Return a new step that goes on from this one to `session`."""
        moves = self.moves
        if session.venue != self.session.venue:
            moves += 1
        travel = self.travel + travel_minutes(self.session.venue, session.venue)
        return _Step(session, self.score + weight, moves, travel, self)


def can_follow(earlier, later):
    """Return True if someone at `earlier` can get to `later` before it starts."""
    gap = timedelta(minutes=travel_minutes(earlier.venue, later.venue))
    return earlier.end + gap <= later.start


def plan_day(sessions, max_venues=DEFAULT_VENUES_PER_DAY):
    """Choose the best sessions to attend on one day.

    For each session, and each number of moves, this remembers the best run of
    sessions that ends with it. Runs are extended in start-time order, so every
    session only needs to look back at the ones before it.

    Args:
        sessions: The day's PlannedSessions.
        max_venues: How many venues the attendee is willing to visit, 1 to 3.

    Returns:
        A DayPlan.

    Raises:
        ValueError: If max_venues is out of range.
    """
    if not MIN_VENUES_PER_DAY <= max_venues <= MAX_VENUES_PER_DAY:
        raise ValueError(
            f"Venues per day must be between {MIN_VENUES_PER_DAY} and "
            f"{MAX_VENUES_PER_DAY}, got {max_venues}"
        )
    max_moves = max_venues - 1
    # One must-attend session is worth more than every other session together,
    # so the plan keeps as many of them as it can before counting the rest.
    must_attend_weight = len(sessions) + 1
    ordered = sorted(sessions, key=lambda session: (session.start, session.end,
                                                    session.session_id))

    # best_steps[index][moves] is the best run ending with ordered[index] that
    # uses exactly that many moves, or None if there is no such run.
    best_steps = []
    for index, session in enumerate(ordered):
        weight = must_attend_weight if session.must_attend else 1
        best_steps.append(best_steps_ending_with(
            session, weight, ordered[:index], best_steps, max_moves
        ))
    return build_day_plan(best_final_step(best_steps), sessions)


def best_steps_ending_with(session, weight, earlier_sessions, earlier_steps,
                           max_moves):
    """Return, for each number of moves, the best run that ends with `session`.

    Args:
        session: The PlannedSession the runs end with.
        weight: What attending it is worth.
        earlier_sessions: The sessions that start before it.
        earlier_steps: The best steps already found for each earlier session.
        max_moves: The most moves allowed in a run.

    Returns:
        A list indexed by number of moves, holding a _Step or None.
    """
    steps = [None] * (max_moves + 1)
    steps[0] = _Step(session, weight, 0, 0)
    for earlier, steps_for_earlier in zip(earlier_sessions, earlier_steps):
        if not can_follow(earlier, session):
            continue
        for earlier_step in steps_for_earlier:
            if earlier_step is None:
                continue
            candidate = earlier_step.extend(session, weight)
            if candidate.moves > max_moves:
                continue
            current = steps[candidate.moves]
            if current is None or candidate.rank() > current.rank():
                steps[candidate.moves] = candidate
    return steps


def best_final_step(best_steps):
    """Return the best step to end the day on, or None if there are no sessions.

    Equal runs are settled by which one finishes earlier.
    """
    best = None
    for steps in best_steps:
        for step in steps:
            if step is None:
                continue
            if best is None or final_rank(step) > final_rank(best):
                best = step
    return best


def final_rank(step):
    """Return a step's rank with an earlier finish breaking ties."""
    return step.rank() + (-step.session.end.timestamp(),)


def build_day_plan(final_step, all_sessions):
    """Turn the last step of the best run into a DayPlan."""
    kept = []
    step = final_step
    while step is not None:
        kept.append(step.session)
        step = step.previous
    kept.reverse()

    moves = []
    total_travel = 0
    for earlier, later in zip(kept, kept[1:]):
        if earlier.venue != later.venue:
            moves.append((earlier.venue, later.venue))
            total_travel += travel_minutes(earlier.venue, later.venue)

    kept_ids = {session.session_id for session in kept}
    missed = []
    for session in sorted(all_sessions, key=lambda session: session.start):
        if session.must_attend and session.session_id not in kept_ids:
            missed.append(session.session_id)
    return DayPlan(kept, moves, total_travel, missed)


def optimize_schedule(sessions, max_venues_by_day=None):
    """Plan each day on its own; the venue limit starts afresh every day.

    Args:
        sessions: PlannedSessions over any number of days.
        max_venues_by_day: Dict of "YYYY-MM-DD" to the venue limit for that
            day. Days not in it use DEFAULT_VENUES_PER_DAY.

    Returns:
        A dict of "YYYY-MM-DD" to DayPlan, in date order.
    """
    max_venues_by_day = max_venues_by_day or {}
    sessions_by_day = {}
    for session in sessions:
        sessions_by_day.setdefault(session.day(), []).append(session)

    plans = {}
    for day in sorted(sessions_by_day):
        max_venues = max_venues_by_day.get(day, DEFAULT_VENUES_PER_DAY)
        plans[day] = plan_day(sessions_by_day[day], max_venues)
    return plans
