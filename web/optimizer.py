"""Build the schedule optimizer's grid: one table of sessions per day.

The page keeps no state on the server. Each request sends back the ticked
"must attend" boxes and each day's venue limit, and the grid is rebuilt from
the attendee's current schedule.
"""

import csv
import io
from dataclasses import dataclass, field

from aws_events.catalog import session_date, session_start_time, session_venue
from aws_events.schedule import group_schedule_by_date
from aws_events.schedule_optimizer import (
    DEFAULT_VENUES_PER_DAY,
    MAX_VENUES_PER_DAY,
    MIN_VENUES_PER_DAY,
    PlannedSession,
    plan_day,
)
from web.views import format_day, session_label


VENUE_CHOICES = list(range(MIN_VENUES_PER_DAY, MAX_VENUES_PER_DAY + 1))
UNKNOWN_VENUE = "Unknown venue"


@dataclass
class OptimizerDay:
    """One day's table in the grid.

    Attributes:
        day: "YYYY-MM-DD".
        label: The day for display, such as "Tue 1 Dec".
        max_venues: The venue limit chosen for the day.
        rows: Dicts describing each session shown, in time order.
        note: How the optimized day gets around, or "" before optimizing.
    """

    day: str
    label: str
    max_venues: int
    rows: list
    note: str = ""


@dataclass
class OptimizerGrid:
    """Everything the optimizer page shows.

    Attributes:
        days: OptimizerDay tables, in date order.
        optimized: Whether the optimizer has trimmed the sessions.
        total_count: Timed sessions on the schedule.
        remove_ids: Favorites the optimizer left out.
        book_ids: Shown sessions that are not reserved yet.
        missed_must_attend: Codes of must-attend sessions that could not fit.
        unplanned_count: Sessions without a time, which are left alone.
    """

    days: list = field(default_factory=list)
    optimized: bool = False
    total_count: int = 0
    remove_ids: list = field(default_factory=list)
    book_ids: list = field(default_factory=list)
    missed_must_attend: list = field(default_factory=list)
    unplanned_count: int = 0

    def kept_count(self):
        """Return how many sessions the grid shows."""
        total = 0
        for day in self.days:
            total += len(day.rows)
        return total


def parse_max_venues(values):
    """Read the per-day venue limits the page sends.

    Args:
        values: Strings such as "2026-12-01:2", one per day.

    Returns:
        A dict of "YYYY-MM-DD" to limit. Malformed or out-of-range values are
        skipped, so those days fall back to the default.
    """
    max_venues_by_day = {}
    for value in values:
        day, _, count = value.partition(":")
        if not count.isdigit():
            continue
        if int(count) in VENUE_CHOICES:
            max_venues_by_day[day] = int(count)
    return max_venues_by_day


def venue_name(venue):
    """Return a venue for display, naming unknown ones."""
    return venue or UNKNOWN_VENUE


def describe_moves(plan):
    """Return how an optimized day gets around, such as "1 move: A → B (60 min)"."""
    if not plan.sessions:
        return ""
    if not plan.moves:
        return f"Stays at {venue_name(plan.sessions[0].venue)}"
    stops = [venue_name(plan.moves[0][0])]
    for _, to_venue in plan.moves:
        stops.append(venue_name(to_venue))
    move_word = "move" if len(plan.moves) == 1 else "moves"
    return (
        f"{len(plan.moves)} {move_word}: {' → '.join(stops)} "
        f"({plan.travel_minutes} min travel)"
    )


def optimizer_row(entry, planned):
    """Return the values a grid row shows for one scheduled session.

    Args:
        entry: A ScheduledSession.
        planned: The matching PlannedSession.
    """
    session = entry.session
    return {
        "id": session["sessionId"],
        "must_attend": planned.must_attend,
        "locked": entry.reserved,
        "start": f"{planned.start:%H:%M}",
        "end": f"{planned.end:%H:%M}",
        "code": session.get("abbreviation") or "",
        "title": session.get("title") or "",
        "abstract": session.get("abstract") or "",
        "venue": venue_name(planned.venue),
        "reserved": entry.reserved,
        "favorite": entry.favorite,
    }


def build_grid(schedule, catalog, must_attend_ids, max_venues_by_day, optimize):
    """Build the grid from the attendee's reserved and favorite sessions.

    Args:
        schedule: A schedule from EventsClient.get_schedule.
        catalog: SessionCatalog with the session details, or None.
        must_attend_ids: Session IDs ticked "must attend". Booked sessions
            count as must attend whether listed or not.
        max_venues_by_day: Dict of "YYYY-MM-DD" to venue limit; missing days use
            DEFAULT_VENUES_PER_DAY.
        optimize: If True, keep only the sessions the optimizer picks.

    Returns:
        An OptimizerGrid.
    """
    grid = OptimizerGrid(optimized=optimize)
    for day, entries in group_schedule_by_date(schedule, catalog):
        planned_by_id = planned_sessions(entries, must_attend_ids)
        grid.unplanned_count += len(entries) - len(planned_by_id)
        if not planned_by_id:
            continue
        max_venues = max_venues_by_day.get(day, DEFAULT_VENUES_PER_DAY)
        add_day(grid, day, entries, planned_by_id, max_venues, catalog)
    return grid


def planned_sessions(entries, must_attend_ids):
    """Return a dict of session ID to PlannedSession for the timed entries.

    Booked sessions are always must attend: the page can't cancel bookings,
    so the plan has to be built around them.
    """
    planned_by_id = {}
    for entry in entries:
        session_id = entry.session["sessionId"]
        must_attend = entry.reserved or session_id in must_attend_ids
        planned = PlannedSession.from_session(entry.session, must_attend)
        if planned is not None:
            planned_by_id[session_id] = planned
    return planned_by_id


def add_day(grid, day, entries, planned_by_id, max_venues, catalog):
    """Optimize one day if the grid asks for it, then add the day's table.

    Args:
        grid: The OptimizerGrid being built; updated in place.
        day: "YYYY-MM-DD".
        entries: The day's ScheduledSessions, in time order.
        planned_by_id: PlannedSessions for the timed entries, by session ID.
        max_venues: The day's venue limit.
        catalog: SessionCatalog used to name sessions, or None.
    """
    kept_ids = set(planned_by_id)
    note = ""
    if grid.optimized:
        plan = plan_day(list(planned_by_id.values()), max_venues)
        kept_ids = set(plan.session_ids())
        note = describe_moves(plan)
        for session_id in plan.missed_must_attend:
            grid.missed_must_attend.append(session_label(session_id, catalog))

    rows = []
    for entry in entries:
        session_id = entry.session["sessionId"]
        if session_id not in planned_by_id:
            continue
        if session_id in kept_ids:
            rows.append(optimizer_row(entry, planned_by_id[session_id]))
            if not entry.reserved:
                grid.book_ids.append(session_id)
        elif entry.favorite and not entry.reserved:
            # A booked session stays on the schedule anyway, so keep its favorite.
            grid.remove_ids.append(session_id)

    grid.total_count += len(planned_by_id)
    grid.days.append(OptimizerDay(day, format_day(day), max_venues, rows, note))


CSV_COLUMNS = [
    "session_id",
    "code",
    "title",
    "date",
    "start_time",
    "length_minutes",
    "venue",
    "room",
    "booked",
]


def favorites_csv(schedule, catalog):
    """Return the attendee's favorites as CSV text, in time order.

    Args:
        schedule: A schedule from EventsClient.get_schedule.
        catalog: SessionCatalog with the session details, or None. Favorites
            not in it are still listed, by ID.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for _, entries in group_schedule_by_date(schedule, catalog):
        for entry in entries:
            if entry.favorite:
                writer.writerow(favorite_csv_row(entry))
    return output.getvalue()


def favorite_csv_row(entry):
    """Return one ScheduledSession as a list of CSV values."""
    session = entry.session
    session_time = session.get("sessionTime") or {}
    return [
        session["sessionId"],
        session.get("abbreviation") or "",
        session.get("title") or "",
        session_date(session) or "",
        session_start_time(session) or "",
        session_time.get("length") or "",
        session_venue(session) or "",
        session.get("room") or "",
        "yes" if entry.reserved else "no",
    ]
