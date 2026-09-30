"""FastAPI web app: search sessions, manage favorites, and wear a Winamp skin.

Pages are rendered on the server with Jinja2; HTMX swaps in the parts that
change (search results, schedule, job status).

Run it with:
    python3 events_web.py
"""

import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from aws_events import EventsError, SessionFilter, SignInError
from aws_events.client import MAX_SESSIONS_PER_REQUEST
from aws_events.schedule import group_schedule_by_date
from web import views
from web.jobs import SUCCEEDED
from web.services import SIGN_IN_JOB, Services, rebuild_job_name
from web.skins import SPRITE_SHEETS, SkinError


WEB_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_EVENT_ID = "reinvent2026"
# Event IDs end up in file names, so only plain characters are accepted.
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SEARCH_LIMIT = 10
PAGE_SIZE = 100
SKIN_COOKIE = "skin"
NO_SKIN = "none"
ONE_YEAR_SECONDS = 365 * 24 * 60 * 60
# HTMX listens for this event to reload the schedule and results.
SCHEDULE_CHANGED = {"HX-Trigger": "scheduleChanged"}


def check_event_id(event_id):
    """Return the event ID if it is well formed, else reject the request."""
    if not EVENT_ID_PATTERN.match(event_id):
        raise HTTPException(status_code=400, detail="Invalid event ID")
    return event_id


def create_app(services):
    """Build the FastAPI app around a Services object."""
    app = FastAPI(title="AWS Event AMP")
    app.mount("/static", StaticFiles(directory=WEB_DIRECTORY / "static"), name="static")
    templates = Jinja2Templates(directory=WEB_DIRECTORY / "templates")
    templates.env.globals["RESERVED_ICON"] = views.RESERVED_ICON
    templates.env.globals["FAVORITE_ICON"] = views.FAVORITE_ICON

    def render(request, template, **context):
        """Render a template with the request and the given values."""
        return templates.TemplateResponse(request, template, context)

    def chosen_skin_id(request):
        """Return the skin picked in this browser, or the default skin."""
        skin_id = request.cookies.get(SKIN_COOKIE, services.skins.default_skin_id)
        if skin_id != NO_SKIN and services.skins.get(skin_id) is None:
            return services.skins.default_skin_id
        return skin_id

    # Pages

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, event_id: str = DEFAULT_EVENT_ID):
        """Show the whole page."""
        check_event_id(event_id)
        skin_id = chosen_skin_id(request)
        skin_style = None
        skin_problem = None
        if skin_id != NO_SKIN:
            try:
                skin_style = services.skins.playlist_style(skin_id)
            except SkinError as error:
                skin_problem = f"Could not load the skin, so it is off: {error}"
                skin_id = NO_SKIN

        search = services.search_for(event_id)
        catalog = search.catalog if search else None
        return render(
            request, "index.html",
            event_id=event_id,
            catalog=catalog,
            venues=catalog.venues() if catalog else [],
            dates=catalog.dates() if catalog else [],
            format_day=views.format_day,
            skins=services.skins.skins(),
            skin_id=skin_id,
            no_skin=NO_SKIN,
            skin_style=skin_style,
            skin_problem=skin_problem,
            legend=legend(),
            rebuild_job=services.jobs.get(rebuild_job_name(event_id)),
        )

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions(request: Request, event_id: str, venue: str = "", date: str = "",
                 q: str = "", offset: int = 0):
        """Return the results table, or the next page of rows when offset > 0."""
        check_event_id(event_id)
        search = services.search_for(event_id)
        if search is None:
            return render(request, "_no_data.html")

        session_filter = SessionFilter(venue=venue or None, date=date or None)
        if q.strip():
            results = search.search(
                q, limit=SEARCH_LIMIT, session_filter=session_filter
            )
            matching = [result.session for result in results]
            page = matching
            summary = f"Top {len(matching)} matches"
            next_offset = None
        else:
            matching = search.catalog.filter_sessions(session_filter)
            page = matching[offset:offset + PAGE_SIZE]
            summary = f"{len(matching)} sessions"
            next_offset = offset + PAGE_SIZE
            if next_offset >= len(matching):
                next_offset = None

        more_url = None
        if next_offset is not None:
            more_url = "/sessions?" + urlencode({
                "event_id": event_id, "venue": venue, "date": date, "q": q,
                "offset": next_offset,
            })
        statuses = views.schedule_status(current_schedule(event_id))
        rows = [views.session_row(session, statuses) for session in page]
        template = "_result_rows.html" if offset else "_results.html"
        return render(
            request, template,
            event_id=event_id,
            rows=rows,
            summary=summary,
            next_offset=next_offset,
            more_url=more_url,
            legend=legend(),
            signed_in=services.is_signed_in(),
            max_selection=MAX_SESSIONS_PER_REQUEST,
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule(request: Request, event_id: str):
        """Return the schedule panel, or a Sign in prompt."""
        check_event_id(event_id)
        if not services.is_signed_in():
            return render(request, "_signed_out.html",
                          sign_in_job=services.jobs.get(SIGN_IN_JOB))
        try:
            schedule_data = services.client.get_schedule(event_id)
        except SignInError as error:
            return render(request, "_signed_out.html", problem=str(error),
                          sign_in_job=services.jobs.get(SIGN_IN_JOB))
        except EventsError as error:
            return render(request, "_schedule.html", event_id=event_id, groups=[],
                          problem=f"Could not load your schedule: {error}")

        search = services.search_for(event_id)
        catalog = search.catalog if search else None
        groups = []
        for day, entries in group_schedule_by_date(schedule_data, catalog):
            groups.append((views.format_day(day), [
                {
                    "session": entry.session,
                    "reserved": entry.reserved,
                    "favorite": entry.favorite,
                }
                for entry in entries
            ]))
        return render(request, "_schedule.html", event_id=event_id, groups=groups,
                      problem=None, max_selection=MAX_SESSIONS_PER_REQUEST)

    # Actions

    @app.post("/favorites", response_class=HTMLResponse)
    def add_favorites(request: Request, event_id: str = Form(...),
                      session_ids: list[str] = Form(default=[])):
        """Add the ticked sessions to favorites."""
        return run_bulk_action(request, event_id, session_ids,
                               services.client.add_favorites,
                               "Added {count} session(s) to favorites.")

    @app.post("/favorites/remove", response_class=HTMLResponse)
    def remove_favorites(request: Request, event_id: str = Form(...),
                         session_ids: list[str] = Form(default=[])):
        """Remove the ticked sessions from favorites."""
        return run_bulk_action(request, event_id, session_ids,
                               services.remove_favorites,
                               "Removed {count} session(s) from favorites.")

    @app.post("/reservations", response_class=HTMLResponse)
    def book_sessions(request: Request, event_id: str = Form(...),
                      session_ids: list[str] = Form(default=[])):
        """Book (reserve) the ticked sessions."""
        return run_bulk_action(request, event_id, session_ids,
                               services.client.reserve_sessions,
                               "Booked {count} session(s).")

    @app.post("/sign-in", response_class=HTMLResponse)
    def sign_in(request: Request):
        """Start the browser sign-in and show its progress."""
        job = services.start_sign_in()
        return render(request, "_sign_in_status.html", job=job)

    @app.get("/sign-in/status", response_class=HTMLResponse)
    def sign_in_status(request: Request):
        """Show sign-in progress; once signed in, refresh the schedule.

        A failed sign-in stays on screen, with a Try again button.
        """
        job = services.jobs.get(SIGN_IN_JOB)
        response = render(request, "_sign_in_status.html", job=job)
        if job is not None and job.status == SUCCEEDED:
            response.headers.update(SCHEDULE_CHANGED)
        return response

    @app.post("/sign-out", response_class=HTMLResponse)
    def sign_out():
        """Forget the saved sign-in and refresh the schedule."""
        services.authenticator.sign_out()
        return HTMLResponse("", headers=SCHEDULE_CHANGED)

    @app.post("/rebuild", response_class=HTMLResponse)
    def rebuild(request: Request, event_id: str = Form(...)):
        """Start downloading and indexing the event's sessions."""
        check_event_id(event_id)
        job = services.start_rebuild(event_id)
        return render(request, "_rebuild_status.html", job=job, event_id=event_id)

    @app.get("/rebuild/status", response_class=HTMLResponse)
    def rebuild_status(request: Request, event_id: str):
        """Show rebuild progress; reload the page once it succeeds."""
        check_event_id(event_id)
        job = services.jobs.get(rebuild_job_name(event_id))
        response = render(request, "_rebuild_status.html", job=job, event_id=event_id)
        if job is not None and job.status == SUCCEEDED:
            # New venues and dates may have appeared, so reload everything.
            response.headers["HX-Refresh"] = "true"
        return response

    @app.post("/skin")
    def choose_skin(skin_id: str = Form(...)):
        """Remember the chosen skin in a cookie and reload the page."""
        if skin_id != NO_SKIN and services.skins.get(skin_id) is None:
            raise HTTPException(status_code=400, detail="Unknown skin")
        response = Response(headers={"HX-Refresh": "true"})
        response.set_cookie(SKIN_COOKIE, skin_id, max_age=ONE_YEAR_SECONDS,
                            samesite="lax")
        return response

    @app.get("/skins/{skin_id}/{sheet}")
    def sprite_sheet(skin_id: str, sheet: str):
        """Serve one of a skin's sprite sheets as a BMP image."""
        if sheet not in SPRITE_SHEETS or services.skins.get(skin_id) is None:
            raise HTTPException(status_code=404)
        try:
            data = services.skins.sprite_sheet(skin_id, sheet)
        except SkinError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        # Skins never change, so browsers may keep them for a day.
        return Response(data, media_type="image/bmp",
                        headers={"Cache-Control": "public, max-age=86400"})

    # Helpers that need the services

    def run_bulk_action(request, event_id, session_ids, action, done_message):
        """Apply an action to the ticked sessions and report what happened.

        Args:
            request: The incoming request.
            event_id: The event the sessions belong to.
            session_ids: The ticked session IDs.
            action: Function taking (event_id, session_ids) and returning a
                dict with "successful" and "failed" lists.
            done_message: Words for the successes, with {count}.

        Returns:
            A message for the page. When anything changed, the response also
            tells the page to reload the schedule and results.
        """
        check_event_id(event_id)
        if not session_ids:
            return render(request, "_message.html", kind="warning",
                          message="Tick at least one session first.")
        try:
            result = action(event_id, session_ids)
        except (EventsError, ValueError) as error:
            return render(request, "_message.html", kind="error", message=str(error))

        search = services.search_for(event_id)
        catalog = search.catalog if search else None
        kind, message = views.describe_bulk_result(result, done_message, catalog)
        response = render(request, "_message.html", kind=kind, message=message)
        if result["successful"]:
            response.headers.update(SCHEDULE_CHANGED)
        return response

    def current_schedule(event_id):
        """Return the schedule for the status icons, or an empty one on error."""
        empty = {"reserved": [], "favorites": [], "personalTime": []}
        if not services.is_signed_in():
            return empty
        try:
            return services.client.get_schedule(event_id)
        except EventsError:
            return empty

    return app


def legend():
    """Return the text that explains the schedule icons."""
    return f"{views.RESERVED_ICON} Reserved    {views.FAVORITE_ICON} Favorite"
