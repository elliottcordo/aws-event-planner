"""Tests for aws_events.schedule_optimizer."""

import unittest
from datetime import datetime

from aws_events.schedule_optimizer import (
    CAESARS_FORUM,
    CAESARS_PALACE,
    MAX_VENUES_PER_DAY,
    MGM_GRAND,
    VENETIAN,
    WYNN_ENCORE,
    PlannedSession,
    optimize_schedule,
    plan_day,
    session_venue,
    travel_minutes,
)
from tests.fakes import make_session


def planned(session_id, venue, start, end, must_attend=False, day="2026-12-01"):
    """Return a PlannedSession on `day` from "HH:MM" start and end times."""
    return PlannedSession(
        session_id, venue,
        datetime.fromisoformat(f"{day}T{start}"),
        datetime.fromisoformat(f"{day}T{end}"),
        must_attend,
    )


class TravelTests(unittest.TestCase):
    """Travel times and venue names."""

    def test_travel_times(self):
        """Walks are 30 minutes, shuttles 45, MGM Grand 60, in either direction."""
        self.assertEqual(travel_minutes(VENETIAN, VENETIAN), 0)
        self.assertEqual(travel_minutes(VENETIAN, CAESARS_FORUM), 30)
        self.assertEqual(travel_minutes(WYNN_ENCORE, VENETIAN), 30)
        self.assertEqual(travel_minutes(CAESARS_PALACE, CAESARS_FORUM), 30)
        self.assertEqual(travel_minutes(WYNN_ENCORE, CAESARS_FORUM), 45)
        self.assertEqual(travel_minutes(CAESARS_PALACE, WYNN_ENCORE), 45)
        self.assertEqual(travel_minutes(CAESARS_PALACE, MGM_GRAND), 60)
        self.assertEqual(travel_minutes(MGM_GRAND, VENETIAN), 60)
        self.assertEqual(travel_minutes(VENETIAN, "Somewhere new"), 60)

    def test_venue_falls_back_to_room(self):
        """Sessions without a venue take it from the start of the room."""
        with_venue = make_session("a", "A", venue="MGM Grand", room="Level 3")
        room_only = make_session("b", "B", room="Wynn/Encore | Level 1 | Ballroom")
        neither = make_session("c", "C")
        self.assertEqual(session_venue(with_venue), MGM_GRAND)
        self.assertEqual(session_venue(room_only), WYNN_ENCORE)
        self.assertIsNone(session_venue(neither))


class PlannedSessionTests(unittest.TestCase):
    """Building PlannedSessions from catalog sessions."""

    def test_from_session_uses_length(self):
        """The end time is the start plus the session's length."""
        session = make_session("a", "A", venue=VENETIAN, sessionTime={
            "date": "2026-12-01", "time": "10:30", "length": "120"})
        result = PlannedSession.from_session(session, must_attend=True)
        self.assertEqual(result.start, datetime(2026, 12, 1, 10, 30))
        self.assertEqual(result.end, datetime(2026, 12, 1, 12, 30))
        self.assertEqual(result.venue, VENETIAN)
        self.assertTrue(result.must_attend)
        self.assertEqual(result.day(), "2026-12-01")

    def test_missing_length_defaults_to_an_hour(self):
        """A session without a length is taken to last 60 minutes."""
        session = make_session("a", "A", sessionTime={
            "date": "2026-12-01", "time": "10:00"})
        result = PlannedSession.from_session(session)
        self.assertEqual(result.end, datetime(2026, 12, 1, 11, 0))

    def test_untimed_session_cannot_be_planned(self):
        """Sessions without a date or time give None."""
        self.assertIsNone(PlannedSession.from_session(make_session("a", "A")))


class PlanDayTests(unittest.TestCase):
    """Choosing one day's sessions."""

    def test_no_sessions(self):
        """An empty day gives an empty plan."""
        plan = plan_day([])
        self.assertEqual(plan.sessions, [])
        self.assertEqual(plan.moves, [])

    def test_drops_overlapping_sessions(self):
        """At one venue, the most non-overlapping sessions are kept."""
        plan = plan_day([
            planned("long", VENETIAN, "09:00", "12:00"),
            planned("a", VENETIAN, "09:00", "10:00"),
            planned("b", VENETIAN, "10:00", "11:00"),
            planned("c", VENETIAN, "11:00", "12:00"),
        ])
        self.assertEqual(plan.session_ids(), ["a", "b", "c"])
        self.assertEqual(plan.moves, [])

    def test_one_venue_stays_put(self):
        """With a limit of 1 venue, the busiest venue wins and there are no moves."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00"),
            planned("m1", MGM_GRAND, "13:00", "14:00"),
            planned("m2", MGM_GRAND, "14:00", "15:00"),
        ]
        plan = plan_day(sessions, max_venues=1)
        self.assertEqual(plan.session_ids(), ["m1", "m2"])
        self.assertEqual(plan.moves, [])

    def test_one_move_needs_travel_time(self):
        """A move is only made when there is time to travel."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00"),
            planned("m1", MGM_GRAND, "10:30", "11:30"),
            planned("m2", MGM_GRAND, "11:30", "12:30"),
        ]
        # 30 minutes is too short for the 60-minute trip to MGM Grand.
        self.assertEqual(plan_day(sessions, max_venues=2).session_ids(),
                         ["m1", "m2"])

        sessions[1] = planned("m1", MGM_GRAND, "11:00", "12:00")
        sessions[2] = planned("m2", MGM_GRAND, "12:00", "13:00")
        plan = plan_day(sessions, max_venues=2)
        self.assertEqual(plan.session_ids(), ["v1", "m1", "m2"])
        self.assertEqual(plan.moves, [(VENETIAN, MGM_GRAND)])
        self.assertEqual(plan.travel_minutes, 60)

    def test_venue_limit_caps_moves(self):
        """Two moves need a limit of 3 venues."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00"),
            planned("f1", CAESARS_FORUM, "10:30", "11:30"),
            planned("m1", MGM_GRAND, "12:30", "13:30"),
        ]
        self.assertEqual(len(plan_day(sessions, max_venues=2).sessions), 2)
        plan = plan_day(sessions, max_venues=3)
        self.assertEqual(plan.session_ids(), ["v1", "f1", "m1"])
        self.assertEqual(len(plan.moves), 2)

    def test_returning_to_a_venue_counts_as_a_move(self):
        """Going back where you started uses up a move too."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00"),
            planned("f1", CAESARS_FORUM, "10:30", "11:30"),
            planned("v2", VENETIAN, "12:00", "13:00"),
        ]
        self.assertEqual(len(plan_day(sessions, max_venues=2).sessions), 2)
        self.assertEqual(len(plan_day(sessions, max_venues=3).sessions), 3)

    def test_must_attend_beats_more_sessions(self):
        """A must-attend session is kept even if two others clash with it."""
        sessions = [
            planned("keep", VENETIAN, "09:00", "11:00", must_attend=True),
            planned("a", VENETIAN, "09:00", "10:00"),
            planned("b", VENETIAN, "10:00", "11:00"),
        ]
        plan = plan_day(sessions)
        self.assertEqual(plan.session_ids(), ["keep"])
        self.assertEqual(plan.missed_must_attend, [])

    def test_clashing_must_attends_are_reported(self):
        """When must-attend sessions clash, the ones left out are listed."""
        sessions = [
            planned("x", VENETIAN, "09:00", "10:00", must_attend=True),
            planned("y", VENETIAN, "09:30", "10:30", must_attend=True),
        ]
        plan = plan_day(sessions)
        self.assertEqual(len(plan.sessions), 1)
        self.assertEqual(len(plan.missed_must_attend), 1)

    def test_ties_prefer_no_move(self):
        """With equal counts, staying at one venue beats moving."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00"),
            planned("v2", VENETIAN, "11:00", "12:00"),
            planned("f2", CAESARS_FORUM, "11:00", "12:00"),
        ]
        plan = plan_day(sessions, max_venues=2)
        self.assertEqual(plan.session_ids(), ["v1", "v2"])

    def test_ties_prefer_earlier_finish(self):
        """With equal counts and no moves, the plan that ends sooner wins."""
        sessions = [
            planned("later", VENETIAN, "09:30", "10:30"),
            planned("earlier", VENETIAN, "09:00", "10:00"),
        ]
        plan = plan_day(sessions, max_venues=1)
        self.assertEqual(plan.session_ids(), ["earlier"])

    def test_invalid_venue_limit(self):
        """Limits outside 1 to MAX_VENUES_PER_DAY are refused."""
        with self.assertRaises(ValueError):
            plan_day([], max_venues=0)
        with self.assertRaises(ValueError):
            plan_day([], max_venues=MAX_VENUES_PER_DAY + 1)

    def test_demo_day(self):
        """The original demo day: five sessions, one short walk to Caesars Palace.

        Moving to MGM Grand would also keep five, but takes 60 minutes, not 30.
        """
        sessions = [
            planned("1", VENETIAN, "08:30", "09:30"),
            planned("2", VENETIAN, "10:00", "11:00"),
            planned("3", MGM_GRAND, "10:00", "11:00"),
            planned("4", VENETIAN, "11:30", "12:30"),
            planned("5", MGM_GRAND, "13:00", "14:00"),
            planned("6", MGM_GRAND, "14:30", "15:30"),
            planned("7", CAESARS_PALACE, "13:00", "14:00"),
            planned("8", MGM_GRAND, "16:00", "17:00"),
            planned("9", CAESARS_PALACE, "15:00", "16:00"),
        ]
        plan = plan_day(sessions, max_venues=2)
        self.assertEqual(plan.session_ids(), ["1", "2", "4", "7", "9"])
        self.assertEqual(plan.moves, [(VENETIAN, CAESARS_PALACE)])
        self.assertEqual(plan.travel_minutes, 30)


class OptimizeScheduleTests(unittest.TestCase):
    """Planning several days."""

    def test_each_day_uses_its_own_limit(self):
        """The venue limit applies per day; unlisted days use the default."""
        sessions = [
            planned("v1", VENETIAN, "09:00", "10:00", day="2026-12-02"),
            planned("f1", CAESARS_FORUM, "10:30", "11:30", day="2026-12-02"),
            planned("v1", VENETIAN, "09:00", "10:00", day="2026-12-01"),
            planned("f1", CAESARS_FORUM, "10:30", "11:30", day="2026-12-01"),
        ]
        plans = optimize_schedule(sessions, {"2026-12-01": 1})
        self.assertEqual(list(plans), ["2026-12-01", "2026-12-02"])
        self.assertEqual(len(plans["2026-12-01"].sessions), 1)
        self.assertEqual(len(plans["2026-12-02"].sessions), 2)


if __name__ == "__main__":
    unittest.main()
