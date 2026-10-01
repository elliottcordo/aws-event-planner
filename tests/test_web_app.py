"""Tests for the FastAPI web app (web.app and web.services)."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.embeddings import DeterministicFakeEmbedding

from aws_events.catalog import SessionCatalog
from aws_events.client import EventsClient
from aws_events.errors import ApiError, SignInError
from aws_events.vector_store import SessionVectorStore
from tests.fakes import FakeAuthenticator, FakeTransport, make_session
from tests.test_skins import BASE_WSZ
from web.app import SKIN_COOKIE, create_app
from web.jobs import SUCCEEDED
from web.services import SIGN_IN_JOB, Services, rebuild_job_name
from web.skins import SkinError, SkinRegistry


EVENT = "reinvent2026"
EMPTY_SCHEDULE = {"reserved": [], "favorites": [], "personalTime": []}


def sample_sessions():
    """Return three sessions over two venues, two days, two types and three levels."""
    return [
        make_session("s1", "Serverless patterns", "Lambda and SQS in depth",
                     abbreviation="SVS301", venue="MGM Grand",
                     type="Workshop", level="300 - Advanced",
                     sessionTime={"date": "2026-12-01", "time": "10:00"}),
        make_session("s2", "Vector databases", "Embeddings at scale",
                     abbreviation="AIM201", venue="Venetian",
                     type="Breakout session", level="200 - Intermediate",
                     sessionTime={"date": "2026-12-02", "time": "09:00"}),
        make_session("s3", "Lambda tuning", "Cold starts",
                     abbreviation="SVS402", venue="MGM Grand",
                     type="Workshop", level="400 - Expert",
                     sessionTime={"date": "2026-12-02", "time": "13:00"}),
    ]


class ScriptedTransport(FakeTransport):
    """A FakeTransport that answers schedule requests with a fixed schedule.

    Other requests get the queued responses, like FakeTransport.
    """

    def __init__(self, schedule, *responses):
        """Store the schedule to return and the other responses."""
        super().__init__(*responses)
        self.schedule = schedule

    def request(self, method, url, params=None, json_body=None, form=None,
                headers=None):
        """Answer GET .../schedule from the fixed schedule."""
        if method == "GET" and url.endswith("/schedule"):
            self.requests.append({"method": method, "url": url})
            if isinstance(self.schedule, Exception):
                raise self.schedule
            return {"schedule": self.schedule}
        return super().request(method, url, params, json_body, form, headers)


class WebAppTestCase(unittest.TestCase):
    """Builds the app around temporary data, a fake API and a fake skin."""

    def setUp(self):
        """Save a catalog and index, and wire services to fakes."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

        embeddings = DeterministicFakeEmbedding(size=16)
        catalog = SessionCatalog(EVENT, sample_sessions())
        catalog.save(self.catalog_path(EVENT))
        SessionVectorStore.build(catalog, embeddings).save(self.index_path(EVENT))

        registry_path = self.directory / "skins.json"
        registry_path.write_text(json.dumps({
            "default_skin": "base",
            "skins": [{"id": "base", "name": "Base", "url": "https://skins.test/b"}],
        }))
        self.skins = SkinRegistry(registry_path, self.directory / "cache",
                                  download=lambda url: BASE_WSZ)

        self.authenticator = FakeAuthenticator()
        self.transport = ScriptedTransport(
            {"reserved": ["s3"], "favorites": ["s1"], "personalTime": []}
        )
        self.opened_urls = []
        self.services = Services(
            EventsClient(self.transport, self.authenticator),
            self.skins,
            embeddings_factory=lambda: embeddings,
            catalog_path=self.catalog_path,
            index_path=self.index_path,
            open_browser=self.opened_urls.append,
        )
        self.client = TestClient(create_app(self.services))

    def catalog_path(self, event_id):
        """Return the temporary catalog path for an event."""
        return self.directory / f"{event_id}-sessions.json"

    def index_path(self, event_id):
        """Return the temporary index path for an event."""
        return self.directory / f"{event_id}-vectors.json"


class PageTests(WebAppTestCase):
    """The main page and skin choice."""

    def test_default_skin_page(self):
        """The page is skinned with the default skin's colors and filters."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="skinned"', response.text)
        self.assertIn('data-skin-url="/skins/base/"', response.text)
        self.assertIn("--pl-normal: #00FF00", response.text)
        self.assertIn('<option value="MGM Grand">', response.text)
        self.assertIn('<option value="2026-12-02">Wed 2 Dec</option>', response.text)

    def test_logo_on_every_page(self):
        """Both pages show the logo, in its light- and dark-background versions."""
        for path in ("/", "/optimizer"):
            text = self.client.get(path).text
            self.assertIn('src="/static/logo-light-bg.svg" alt="AWS Event AMP"', text)
            self.assertIn('src="/static/logo-dark-bg.svg" alt="AWS Event AMP"', text)
        self.assertEqual(self.client.get("/static/logo-dark-bg.svg").status_code, 200)

    def test_display_shows_year_and_skin_colors(self):
        """kbps and kHz show the year; the visualizer uses the skin's viscolor.txt."""
        text = self.client.get("/").text
        self.assertIn('class="visualizer"', text)
        self.assertIn('data-text=" 20"', text)
        self.assertIn('data-text="26"', text)
        self.assertIn('data-vis-colors="#0A141E,#182129,', text)

    def test_no_skin_cookie_gives_plain_page(self):
        """Choosing "No skin" renders the plain look."""
        self.client.cookies.set(SKIN_COOKIE, "none")
        response = self.client.get("/")
        self.assertIn('class="plain"', response.text)
        self.assertNotIn("data-skin-url", response.text)

    def test_unknown_skin_cookie_uses_default(self):
        """A stale cookie for a removed skin falls back to the default."""
        self.client.cookies.set(SKIN_COOKIE, "deleted-skin")
        self.assertIn('data-skin-url="/skins/base/"', self.client.get("/").text)

    def test_skin_that_fails_to_load_turns_skin_off(self):
        """If a skin can't be downloaded, the page is plain with a warning."""
        def broken_download(_url):
            raise SkinError("offline")

        self.skins.download = broken_download
        response = self.client.get("/")
        self.assertIn('class="plain"', response.text)
        self.assertIn("Could not load the skin", response.text)

    def test_choose_skin_sets_cookie_and_reloads(self):
        """POST /skin remembers the choice and asks HTMX to reload."""
        response = self.client.post("/skin", data={"skin_id": "none"})
        self.assertEqual(response.headers["HX-Refresh"], "true")
        self.assertIn(f"{SKIN_COOKIE}=none", response.headers["set-cookie"])
        self.assertEqual(
            self.client.post("/skin", data={"skin_id": "bogus"}).status_code, 400
        )

    def test_sprite_sheet_served_as_bmp(self):
        """Sprite sheets are served from the skin; other names are refused."""
        response = self.client.get("/skins/base/pledit.bmp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/bmp")
        self.assertEqual(response.content, b"base-pledit")
        self.assertEqual(self.client.get("/skins/base/pledit.txt").status_code, 404)
        self.assertEqual(self.client.get("/skins/other/main.bmp").status_code, 404)

    def test_invalid_event_id_is_rejected(self):
        """Event IDs that could reach other files are refused."""
        self.assertEqual(self.client.get("/?event_id=../../etc").status_code, 400)
        response = self.client.get("/sessions", params={"event_id": "a/b"})
        self.assertEqual(response.status_code, 400)

    def test_event_without_data(self):
        """An event with no saved data explains how to get it."""
        response = self.client.get("/?event_id=other-event")
        self.assertIn("No session data for this event yet", response.text)


class SessionResultsTests(WebAppTestCase):
    """The session table."""

    def test_browse_lists_all_in_time_order_with_icons(self):
        """With no query, every session is listed with its schedule icons."""
        response = self.client.get("/sessions", params={"event_id": EVENT})
        self.assertIn("3 sessions", response.text)
        text = response.text
        self.assertLess(text.index("SVS301"), text.index("AIM201"))
        self.assertIn("❤️", text)
        self.assertIn("✅", text)
        self.assertIn('title="Lambda and SQS in depth"', text)

    def test_code_and_title_share_one_column(self):
        """Each row shows the code above the title in a single Session column."""
        text = self.client.get("/sessions", params={"event_id": EVENT}).text
        self.assertIn("<th>Session</th>", text)
        self.assertNotIn("<th>Code</th>", text)
        self.assertRegex(
            text, r'<td class="session">\s*<span class="code">SVS301</span>'
                  r'\s*<span class="title">Serverless patterns</span>'
        )

    def test_status_column_has_no_mine_header(self):
        """The status column has no visible header, only a screen-reader label."""
        text = self.client.get("/sessions", params={"event_id": EVENT}).text
        self.assertNotIn(">Mine<", text)
        self.assertIn('<span class="visually-hidden">On your schedule</span>', text)

    def test_status_slots_keep_reserved_before_favorite(self):
        """Every row has two slots, so a lone heart lines up with other hearts."""
        text = self.client.get("/sessions", params={"event_id": EVENT}).text
        self.assertEqual(text.count('class="status-slot"'), 2 * 3)
        # s1 is a favorite only: an empty reserved slot, then the heart.
        self.assertRegex(
            text, r'<span class="status-slot"></span>\s*'
                  r'<span class="status-slot" title="Favorite">❤️'
        )
        # s3 is reserved only: the check, then an empty favorite slot.
        self.assertRegex(
            text, r'title="Reserved">✅<span class="visually-hidden">Reserved</span>'
                  r'</span>\s*<span class="status-slot"></span>'
        )

    def test_filters_apply(self):
        """Venue and date filters narrow the list."""
        response = self.client.get("/sessions", params={
            "event_id": EVENT, "venue": "MGM Grand", "date": "2026-12-02",
        })
        self.assertIn("1 sessions", response.text)
        self.assertIn("SVS402", response.text)
        self.assertNotIn("SVS301", response.text)

    def test_type_and_level_filters_apply(self):
        """Type and level narrow the list, and are offered in the search bar."""
        page = self.client.get("/").text
        self.assertIn('<option value="Workshop">Workshop</option>', page)
        self.assertIn('<option value="400 - Expert">400 - Expert</option>', page)

        workshops = self.client.get("/sessions", params={
            "event_id": EVENT, "type": "Workshop",
        }).text
        self.assertIn("2 sessions", workshops)
        self.assertNotIn("AIM201", workshops)

        expert = self.client.get("/sessions", params={
            "event_id": EVENT, "type": "Workshop", "level": "400 - Expert",
        }).text
        self.assertIn("1 sessions", expert)
        self.assertIn("SVS402", expert)

    def test_search_returns_top_matches(self):
        """A query shows the top matches within the filters."""
        response = self.client.get("/sessions", params={
            "event_id": EVENT, "q": "lambda", "venue": "MGM Grand",
        })
        self.assertIn("Top 2 matches", response.text)
        self.assertNotIn("AIM201", response.text)

    def test_paging(self):
        """Browsing pages 100 rows at a time with a Show more link."""
        many = [make_session(f"x{number:03}", f"Talk {number}")
                for number in range(150)]
        SessionCatalog("big", many).save(self.catalog_path("big"))
        SessionVectorStore.build(SessionCatalog("big", many[:1]),
                                 DeterministicFakeEmbedding(size=16)).save(
            self.index_path("big"))

        first = self.client.get("/sessions", params={"event_id": "big"})
        self.assertEqual(first.text.count('name="session_ids"'), 100)
        self.assertIn("offset=100", first.text)

        second = self.client.get("/sessions", params={"event_id": "big", "offset": 100})
        self.assertEqual(second.text.count('name="session_ids"'), 50)
        self.assertNotIn("Show more", second.text)


class ScheduleTests(WebAppTestCase):
    """The schedule panel and sign-in."""

    def test_signed_in_schedule_grouped_by_day(self):
        """Reserved and favorite sessions appear under their days."""
        response = self.client.get("/schedule", params={"event_id": EVENT})
        text = response.text
        self.assertIn("Tue 1 Dec", text)
        self.assertIn("Wed 2 Dec", text)
        self.assertLess(text.index("Tue 1 Dec"), text.index("Wed 2 Dec"))
        self.assertIn('title="Reserved"', text)
        # The status words are only for screen readers, not shown next to icons.
        self.assertNotIn("· ✅", text)
        self.assertIn('<span class="visually-hidden">Favorite</span>', text)
        self.assertIn("Sign out", text)

    def test_signed_out_shows_sign_in_without_calling_api(self):
        """Signed out, the panel offers Sign in and the API isn't called."""
        self.authenticator.signed_in = False
        response = self.client.get("/schedule", params={"event_id": EVENT})
        self.assertIn('hx-post="/sign-in"', response.text)
        self.assertEqual(self.transport.requests, [])

    def test_expired_sign_in_shows_sign_in_with_reason(self):
        """A SignInError while loading shows why, with a Sign in button."""
        self.transport.schedule = SignInError("Your sign-in has expired.")
        response = self.client.get("/schedule", params={"event_id": EVENT})
        self.assertIn("Your sign-in has expired.", response.text)
        self.assertIn('hx-post="/sign-in"', response.text)

    def test_api_error_is_shown(self):
        """Other API errors are shown in the panel."""
        self.transport.schedule = ApiError("Service unavailable", 503)
        response = self.client.get("/schedule", params={"event_id": EVENT})
        self.assertIn("Could not load your schedule", response.text)

    def test_sign_in_flow(self):
        """Sign in shows the link, then the status refreshes the schedule."""
        self.authenticator.signed_in = False
        started = self.client.post("/sign-in")
        self.assertEqual(started.status_code, 200)
        job = self.services.jobs.get(SIGN_IN_JOB)
        job.wait(5)
        self.assertEqual(job.status, SUCCEEDED)
        self.assertEqual(self.opened_urls, [FakeAuthenticator.SIGN_IN_URL])

        status = self.client.get("/sign-in/status")
        self.assertEqual(status.headers["HX-Trigger"], "scheduleChanged")
        self.assertIn("Signed in.", status.text)

    def test_sign_out(self):
        """Sign out forgets the sign-in and refreshes the schedule."""
        response = self.client.post("/sign-out")
        self.assertTrue(self.authenticator.signed_out)
        self.assertEqual(response.headers["HX-Trigger"], "scheduleChanged")


class FavoritesTests(WebAppTestCase):
    """Adding favorites."""

    def test_adds_ticked_sessions_and_refreshes(self):
        """Ticked IDs are sent to the API and the page is told to refresh."""
        self.transport.responses.append(
            {"result": {"successful": ["s1", "s2"], "failed": []}}
        )
        response = self.client.post("/favorites", data={
            "event_id": EVENT, "session_ids": ["s1", "s2"],
        })
        self.assertIn("Added 2 session(s) to favorites.", response.text)
        self.assertEqual(response.headers["HX-Trigger"], "scheduleChanged")
        sent = self.transport.requests[-1]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["json_body"], {"sessionIds": ["s1", "s2"]})

    def test_refusal_is_explained_by_session_code(self):
        """A refused session is named by its code with a plain reason."""
        self.transport.responses.append({"result": {
            "successful": [],
            "failed": [{"sessionId": "s1", "code": "alreadyFavorited"}],
        }})
        response = self.client.post("/favorites", data={
            "event_id": EVENT, "session_ids": ["s1"],
        })
        self.assertIn("SVS301 is already a favorite.", response.text)
        self.assertIn("message error", response.text)
        # Nothing changed, so the page is not told to reload.
        self.assertNotIn("HX-Trigger", response.headers)

    def test_remove_favorites(self):
        """Each ticked session is removed; one that isn't a favorite is reported."""
        self.transport.responses.extend([None, ApiError("Not a favorite", 404)])
        response = self.client.post("/favorites/remove", data={
            "event_id": EVENT, "session_ids": ["s1", "s2"],
        })
        deletes = [r for r in self.transport.requests if r["method"] == "DELETE"]
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in deletes], ["s1", "s2"])
        self.assertIn("Removed 1 session(s) from favorites.", response.text)
        # Jinja escapes the apostrophe in "isn't".
        self.assertIn("AIM201 isn&#39;t a favorite.", response.text)
        self.assertIn("message warning", response.text)
        self.assertEqual(response.headers["HX-Trigger"], "scheduleChanged")

    def test_nothing_ticked(self):
        """Submitting with nothing ticked asks the user to tick something."""
        response = self.client.post("/favorites", data={"event_id": EVENT})
        self.assertIn("Tick at least one session", response.text)
        self.assertEqual(self.transport.requests, [])


class BookingTests(WebAppTestCase):
    """Booking (reserving) sessions."""

    def test_book_selected(self):
        """Ticked sessions are reserved and the page reloads the schedule."""
        self.transport.responses.append(
            {"result": {"successful": ["s1", "s2"], "failed": []}}
        )
        response = self.client.post("/reservations", data={
            "event_id": EVENT, "session_ids": ["s1", "s2"],
        })
        sent = self.transport.requests[-1]
        self.assertEqual(sent["method"], "POST")
        self.assertTrue(sent["url"].endswith(f"/events/{EVENT}/reservations"))
        self.assertEqual(sent["json_body"], {"sessionIds": ["s1", "s2"]})
        self.assertIn("Booked 2 session(s).", response.text)
        self.assertEqual(response.headers["HX-Trigger"], "scheduleChanged")

    def test_clash_names_the_other_sessions(self):
        """A schedule clash says which booked sessions it clashes with."""
        self.transport.responses.append({"result": {
            "successful": ["s2"],
            "failed": [{"sessionId": "s1", "code": "scheduleConflict",
                        "conflictsWith": ["s3"]}],
        }})
        response = self.client.post("/reservations", data={
            "event_id": EVENT, "session_ids": ["s1", "s2"],
        })
        self.assertIn("Booked 1 session(s).", response.text)
        self.assertIn("SVS301 clashes with SVS402 on your schedule.", response.text)
        self.assertIn("message warning", response.text)

    def test_disabled_booking_says_not_yet_enabled(self):
        """When the API has booking switched off, the page says so plainly."""
        self.transport.responses.append(
            ApiError("This operation is currently disabled", 409)
        )
        response = self.client.post("/reservations", data={
            "event_id": EVENT, "session_ids": ["s1"],
        })
        self.assertIn("This feature is not yet enabled", response.text)
        self.assertNotIn("409", response.text)
        self.assertIn("message error", response.text)

    def test_disabled_unfavorite_stops_at_first_session(self):
        """If removing favorites is switched off, it stops and says so."""
        self.transport.responses.append(
            ApiError("This operation is currently disabled", 409)
        )
        response = self.client.post("/favorites/remove", data={
            "event_id": EVENT, "session_ids": ["s1", "s2"],
        })
        self.assertIn("This feature is not yet enabled", response.text)
        deletes = [r for r in self.transport.requests if r["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)

    def test_more_than_ten_is_refused_before_calling_api(self):
        """Booking more than the API's limit gives an error, not a request."""
        ids = [f"x{number}" for number in range(11)]
        response = self.client.post("/reservations", data={
            "event_id": EVENT, "session_ids": ids,
        })
        self.assertIn("message error", response.text)
        self.assertEqual(self.transport.requests, [])

    def test_both_panels_offer_the_actions(self):
        """The results offer favorite and book; the schedule offers remove and book."""
        results = self.client.get("/sessions", params={"event_id": EVENT}).text
        self.assertIn('hx-post="/favorites"', results)
        self.assertIn('hx-post="/reservations"', results)

        schedule = self.client.get("/schedule", params={"event_id": EVENT}).text
        self.assertIn('hx-post="/favorites/remove"', schedule)
        self.assertIn('hx-post="/reservations"', schedule)
        self.assertEqual(schedule.count('name="session_ids"'), 2)


class OptimizerTests(WebAppTestCase):
    """The schedule optimizer page."""

    def setUp(self):
        """Favorite all three sample sessions; s2 and s3 share 2 Dec."""
        super().setUp()
        self.transport.schedule = {
            "reserved": [], "favorites": ["s1", "s2", "s3"], "personalTime": [],
        }

    def test_schedule_panel_links_to_optimizer(self):
        """The schedule panel has a Schedule optimizer button."""
        schedule = self.client.get("/schedule", params={"event_id": EVENT}).text
        self.assertIn('action="/optimizer"', schedule)
        self.assertIn("Schedule optimizer", schedule)

    def test_page_is_skinned_with_back_button(self):
        """The page wears the skin and has a Back button to the main page."""
        response = self.client.get("/optimizer", params={"event_id": EVENT})
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="skinned"', response.text)
        self.assertIn('action="/"', response.text)
        self.assertIn("Back", response.text)
        self.assertIn('hx-get="/optimizer/grid?event_id=reinvent2026"', response.text)

    def test_page_without_skin(self):
        """With "No skin" the page is plain and has no Winamp main window."""
        self.client.cookies.set(SKIN_COOKIE, "none")
        response = self.client.get("/optimizer", params={"event_id": EVENT})
        self.assertIn('class="plain"', response.text)
        self.assertNotIn("main-window", response.text)

    def test_fresh_grid_has_one_table_per_day(self):
        """Each day gets a table with a venue limit and must-attend boxes."""
        text = self.client.get("/optimizer/grid", params={"event_id": EVENT}).text
        self.assertEqual(text.count('class="calendar-day"'), 2)
        self.assertEqual(text.count('name="must_attend"'), 3)
        self.assertIn('<option value="2026-12-02:2" selected>', text)
        self.assertIn('value="no"', text)
        self.assertIn("Apply to favorites</button>", text)

    def test_optimize_keeps_the_best_sessions(self):
        """With 1 venue on 2 Dec, one session goes and is offered for removal."""
        response = self.client.post("/optimizer/optimize", data={
            "event_id": EVENT, "max_venues": ["2026-12-02:1"],
        })
        text = response.text
        self.assertIn("keeping 2 of 3 sessions", text)
        self.assertIn('name="remove_ids" value="s3"', text)
        self.assertNotIn('value="s3" aria-label', text)
        self.assertIn("Stays at Venetian", text)
        self.assertIn("1 session(s) will be deleted from your favorites", text)
        self.assertIn('<option value="2026-12-02:1" selected>', text)

    def test_refresh_keeps_choices(self):
        """Rebuilding after a change keeps the ticks and the optimized state."""
        response = self.client.post("/optimizer/grid", data={
            "event_id": EVENT, "optimized": "yes", "must_attend": ["s3"],
            "max_venues": ["2026-12-02:1"],
        })
        self.assertIn('name="remove_ids" value="s2"', response.text)
        self.assertIn('value="s3" checked', response.text)

    def test_apply_removes_only_dropped_favorites(self):
        """Apply deletes the listed favorites and asks the page to refresh."""
        response = self.client.post("/optimizer/apply", data={
            "event_id": EVENT, "remove_ids": ["s3"],
        })
        deletes = [r for r in self.transport.requests if r["method"] == "DELETE"]
        self.assertEqual([r["url"].rsplit("/", 1)[-1] for r in deletes], ["s3"])
        self.assertIn("Removed 1 session(s) from favorites.", response.text)
        self.assertEqual(response.headers["HX-Trigger"], "scheduleChanged")

    def test_book_sends_batches_of_ten(self):
        """Booking more than ten sessions splits them over several calls."""
        ids = [f"x{number}" for number in range(12)]
        self.transport.responses.extend([
            {"result": {"successful": ids[:10], "failed": []}},
            {"result": {"successful": ids[10:], "failed": []}},
        ])
        response = self.client.post("/optimizer/book", data={
            "event_id": EVENT, "book_ids": ids,
        })
        posts = [r for r in self.transport.requests if r["method"] == "POST"]
        self.assertEqual([len(r["json_body"]["sessionIds"]) for r in posts], [10, 2])
        self.assertIn("Booked 12 session(s).", response.text)

    def test_grid_recommends_a_backup(self):
        """The grid suggests backing up favorites, with a download link."""
        text = self.client.get("/optimizer/grid", params={"event_id": EVENT}).text
        self.assertIn("We recommend you back up your favorites first", text)
        self.assertIn('href="/optimizer/favorites.csv?event_id=reinvent2026" download',
                      text)

    def test_booked_sessions_are_locked(self):
        """A booked session shows ✅ instead of a must-attend box."""
        self.transport.schedule = {
            "reserved": ["s3"], "favorites": ["s1", "s2"], "personalTime": [],
        }
        text = self.client.get("/optimizer/grid", params={"event_id": EVENT}).text
        self.assertEqual(text.count('name="must_attend"'), 2)
        self.assertNotIn('name="must_attend" value="s3"', text)
        self.assertIn('title="Booked: always kept"', text)

    def test_booked_session_kept_even_if_unticked(self):
        """Optimizing with 1 venue keeps the booked s3 over the favorite s2."""
        self.transport.schedule = {
            "reserved": ["s3"], "favorites": ["s1", "s2"], "personalTime": [],
        }
        response = self.client.post("/optimizer/optimize", data={
            "event_id": EVENT, "max_venues": ["2026-12-02:1"],
        })
        self.assertIn('name="remove_ids" value="s2"', response.text)
        self.assertIn("Stays at MGM Grand", response.text)

    def test_favorites_csv_download(self):
        """The backup is a CSV attachment listing every favorite."""
        response = self.client.get("/optimizer/favorites.csv",
                                   params={"event_id": EVENT})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/csv"))
        self.assertIn('attachment; filename="reinvent2026-favorites-',
                      response.headers["content-disposition"])
        lines = response.text.splitlines()
        self.assertTrue(lines[0].startswith("session_id,code,title"))
        self.assertEqual([line.split(",")[0] for line in lines[1:]],
                         ["s1", "s2", "s3"])

    def test_favorites_csv_needs_sign_in(self):
        """Signed out, the backup explains why and doesn't call the API."""
        self.authenticator.signed_in = False
        response = self.client.get("/optimizer/favorites.csv",
                                   params={"event_id": EVENT})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Sign in first", response.text)
        self.assertEqual(self.transport.requests, [])

    def test_signed_out_grid_offers_sign_in(self):
        """Signed out, the grid asks to sign in and reloads once signed in."""
        self.authenticator.signed_in = False
        text = self.client.get("/optimizer/grid", params={"event_id": EVENT}).text
        self.assertIn('hx-post="/sign-in"', text)
        self.assertIn("scheduleChanged from:body", text)
        self.assertEqual(self.transport.requests, [])


class RebuildTests(WebAppTestCase):
    """Downloading and indexing an event from the page."""

    def test_rebuild_saves_data_and_reloads(self):
        """The job downloads, indexes and saves; status then reloads the page."""
        self.transport.responses.append(
            {"items": [make_session("n1", "New talk")], "totalCount": 1}
        )
        self.client.post("/rebuild", data={"event_id": "new-event"})
        job = self.services.jobs.get(rebuild_job_name("new-event"))
        job.wait(10)
        self.assertEqual(job.status, SUCCEEDED, job.message)
        self.assertTrue(self.catalog_path("new-event").exists())
        self.assertTrue(self.index_path("new-event").exists())

        status = self.client.get("/rebuild/status", params={"event_id": "new-event"})
        self.assertEqual(status.headers["HX-Refresh"], "true")
        self.assertIsNotNone(self.services.search_for("new-event"))

    def test_rebuild_failure_is_shown(self):
        """A failed download is reported and the page is not reloaded."""
        def not_signed_in():
            raise SignInError("You are not signed in.")

        self.transport.responses.append(ApiError("Sign in to continue", 401))
        self.authenticator.get_access_token = not_signed_in
        self.client.post("/rebuild", data={"event_id": "locked-event"})
        self.services.jobs.get(rebuild_job_name("locked-event")).wait(10)

        status = self.client.get("/rebuild/status", params={"event_id": "locked-event"})
        self.assertIn("You are not signed in.", status.text)
        self.assertNotIn("HX-Refresh", status.headers)


if __name__ == "__main__":
    unittest.main()
