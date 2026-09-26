"""Streamlit web UI for searching an event's sessions and managing favorites.

Run it with:
    streamlit run events_ui.py
"""

import webbrowser
from datetime import date

import streamlit as st

from aws_events import (
    Authenticator,
    EventsClient,
    EventsError,
    FastEmbedEmbeddings,
    HttpTransport,
    SessionCatalog,
    SessionFilter,
    SessionSearch,
    SessionVectorStore,
    TokenStore,
    default_catalog_path,
    default_index_path,
    session_date,
    session_start_time,
)
from aws_events.client import MAX_SESSIONS_PER_REQUEST
from aws_events.schedule import group_schedule_by_date


DEFAULT_EVENT_ID = "reinvent2026"
ALL = "All"
SEARCH_LIMIT = 10

RESERVED_ICON = "✅"
FAVORITE_ICON = "❤️"
LEGEND = f"{RESERVED_ICON} Reserved    {FAVORITE_ICON} Favorite"

# Tall rows give the title and abstract room to wrap onto several lines.
GRID_ROW_HEIGHT = 100
GRID_HEIGHT = 620

# Column widths in pixels, sized so everything up to the abstract fits on a
# laptop screen; venue, type and level are a scroll to the right.
GRID_COLUMNS = {
    "Mine": st.column_config.TextColumn(width=55),
    "When": st.column_config.TextColumn(width=105),
    "Code": st.column_config.TextColumn(width=95),
    "Title": st.column_config.TextColumn(width=210),
    "Abstract": st.column_config.TextColumn(width=405),
    "Venue": st.column_config.TextColumn(width=105),
    "Type": st.column_config.TextColumn(width=110),
    "Level": st.column_config.TextColumn(width=110),
}
EMPTY_SCHEDULE = {"reserved": [], "favorites": [], "personalTime": []}


# Pure helpers (no Streamlit calls, so they can be unit tested)

def format_day(day):
    """Return "YYYY-MM-DD" as a readable day such as "Wed 2 Dec"."""
    if not day:
        return "Date not set"
    parsed = date.fromisoformat(day)
    return f"{parsed:%a} {parsed.day} {parsed:%b}"


def format_when(session):
    """Return a session's day and start time, such as "Wed 2 Dec 13:30"."""
    when = format_day(session_date(session))
    start_time = session_start_time(session)
    if start_time:
        when = f"{when} {start_time}"
    return when


def status_icons(reserved, favorite):
    """Return the icons for a session's place on the schedule, or ""."""
    icons = []
    if reserved:
        icons.append(RESERVED_ICON)
    if favorite:
        icons.append(FAVORITE_ICON)
    return " ".join(icons)


def schedule_icons(schedule):
    """Return a dict mapping each scheduled session ID to its status icons."""
    reserved = set(schedule["reserved"])
    favorites = set(schedule["favorites"])
    icons = {}
    for session_id in reserved | favorites:
        icons[session_id] = status_icons(
            session_id in reserved, session_id in favorites
        )
    return icons


def session_rows(sessions, icons):
    """Return one grid row (a dict of column values) per session.

    Args:
        sessions: Session dicts to show, in display order.
        icons: Dict of session ID to status icons, from schedule_icons.
    """
    rows = []
    for session in sessions:
        rows.append({
            "Mine": icons.get(session["sessionId"], ""),
            "When": format_when(session),
            "Code": session.get("abbreviation", ""),
            "Title": session.get("title", ""),
            "Abstract": session.get("abstract", ""),
            "Venue": session.get("venue") or "",
            "Type": session.get("type", ""),
            "Level": session.get("level", ""),
        })
    return rows


def filter_from_choices(venue_choice, date_choice):
    """Build a SessionFilter from the dropdowns, where ALL means no filter."""
    venue = None if venue_choice == ALL else venue_choice
    day = None if date_choice == ALL else date_choice
    return SessionFilter(venue=venue, date=day)


# Cached resources, shared across reruns

@st.cache_resource
def get_client():
    """Return an EventsClient that never starts a sign-in by itself.

    Calls that need sign-in raise SignInError instead of opening a browser;
    the user signs in with the Sign in button.
    """
    transport = HttpTransport()
    authenticator = Authenticator(TokenStore(), transport, sign_in_when_needed=False)
    return EventsClient(transport, authenticator)


def is_signed_in():
    """Return True if a sign-in is saved (it may still turn out to be expired)."""
    return get_client().authenticator.has_saved_sign_in()


@st.cache_resource(show_spinner="Loading the embedding model...")
def get_embeddings():
    """Return the embedding model, loaded once per server."""
    return FastEmbedEmbeddings()


@st.cache_resource(show_spinner="Loading sessions and search index...")
def load_search(event_id):
    """Load the saved catalog and index for an event.

    Returns:
        A SessionSearch, or None if the data has not been downloaded yet.
    """
    try:
        catalog = SessionCatalog.load(default_catalog_path(event_id))
        vector_store = SessionVectorStore.load(
            default_index_path(event_id), get_embeddings()
        )
    except FileNotFoundError:
        return None
    return SessionSearch(catalog, vector_store)


# Actions

def download_and_rebuild(event_id):
    """Download the event's sessions, rebuild the search index, and reload."""
    with st.status("Updating session data...", expanded=True) as status:
        st.write("Downloading sessions...")
        try:
            catalog = SessionCatalog.download(get_client(), event_id)
        except EventsError as error:
            status.update(label="Could not download sessions.", state="error")
            st.error(f"{error}")
            return
        catalog.save(default_catalog_path(event_id))
        st.write(f"Saved {len(catalog)} sessions. Building the search index "
                 "(this can take a few minutes)...")
        vector_store = SessionVectorStore.build(catalog, get_embeddings())
        vector_store.save(default_index_path(event_id))
        status.update(label=f"Indexed {len(catalog)} sessions.", state="complete")
    load_search.clear()


def refresh_schedule(event_id):
    """Fetch the attendee's schedule into session state.

    On failure the schedule is empty and the error is kept in session state
    for the schedule panel to show; the Refresh button retries.
    """
    st.session_state.schedule_event_id = event_id
    st.session_state.schedule = EMPTY_SCHEDULE
    st.session_state.schedule_error = None
    try:
        with st.spinner("Loading your schedule..."):
            st.session_state.schedule = get_client().get_schedule(event_id)
    except EventsError as error:
        st.session_state.schedule_error = f"Could not load your schedule: {error}"


def sign_in(event_id):
    """Run the browser sign-in, showing the link on the page, then load the schedule."""
    link_area = st.empty()

    def open_and_show_link(url):
        link_area.markdown(
            f"If a sign-in tab didn't open, [open AWS Builder ID sign-in]({url})."
        )
        webbrowser.open(url)

    try:
        with st.spinner("Waiting for you to finish signing in (up to 5 minutes)..."):
            get_client().authenticator.sign_in(open_browser=open_and_show_link)
    except EventsError as error:
        st.session_state.schedule_error = f"Sign-in failed: {error}"
        return
    finally:
        link_area.empty()
    refresh_schedule(event_id)


def sign_out():
    """Forget the saved sign-in and clear the schedule."""
    get_client().authenticator.sign_out()
    st.session_state.schedule = EMPTY_SCHEDULE
    st.session_state.schedule_error = None


def add_to_favorites(event_id, session_ids):
    """Favorite the sessions, remember the outcome, and refresh the schedule."""
    result = get_client().add_favorites(event_id, session_ids)
    added = len(result["successful"])
    messages = [f"Added {added} session(s) to favorites."] if added else []
    for failure in result["failed"]:
        messages.append(f"Could not add {failure['sessionId']}: {failure['code']}")
    st.session_state.flash_messages = messages
    refresh_schedule(event_id)


# Page sections

def render_header():
    """Show the title, event picker and rebuild button; return the event ID."""
    st.title("AWS Events session planner")
    event_column, button_column = st.columns([3, 1], vertical_alignment="bottom")
    event_id = event_column.text_input("Event ID", value=DEFAULT_EVENT_ID)
    if button_column.button("Download data and rebuild index", width="stretch"):
        download_and_rebuild(event_id)
    return event_id


def render_search_panel(event_id, session_search):
    """Show filters, search box and results grid with an add-to-favorites button."""
    st.subheader("Find sessions")
    if session_search is None:
        st.info("No session data for this event yet. "
                "Click **Download data and rebuild index** to get it.")
        return

    catalog = session_search.catalog
    venue_column, date_column = st.columns(2)
    venue_choice = venue_column.selectbox("Venue", [ALL] + catalog.venues())
    date_choice = date_column.selectbox(
        "Date", [ALL] + catalog.dates(),
        format_func=lambda choice: choice if choice == ALL else format_day(choice),
    )
    query = st.text_input(
        "Search", placeholder="For example: cost optimization for EKS"
    )
    session_filter = filter_from_choices(venue_choice, date_choice)

    if query.strip():
        results = session_search.search(
            query, limit=SEARCH_LIMIT, session_filter=session_filter
        )
        sessions = [result.session for result in results]
        st.caption(f"Top {len(sessions)} matches")
    else:
        sessions = catalog.filter_sessions(session_filter)
        st.caption(f"{len(sessions)} sessions")

    icons = schedule_icons(st.session_state.get("schedule", EMPTY_SCHEDULE))
    # A new key whenever the rows change clears selections from the old rows.
    grid_key = f"grid-{event_id}-{query}-{venue_choice}-{date_choice}"
    grid = st.dataframe(
        session_rows(sessions, icons),
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key=grid_key,
        row_height=GRID_ROW_HEIGHT,
        height=GRID_HEIGHT,
        column_config=GRID_COLUMNS,
    )

    st.caption(LEGEND)

    selected_ids = [sessions[row]["sessionId"] for row in grid.selection.rows]
    too_many = len(selected_ids) > MAX_SESSIONS_PER_REQUEST
    if too_many:
        st.warning(f"Select at most {MAX_SESSIONS_PER_REQUEST} sessions at a time.")
    signed_in = is_signed_in()
    if st.button(f"Add {len(selected_ids)} selected to favorites",
                 disabled=not selected_ids or too_many or not signed_in,
                 type="primary"):
        add_to_favorites(event_id, selected_ids)
        st.rerun()
    if not signed_in:
        st.caption("Sign in (on the right) to add favorites.")


def render_schedule_panel(event_id, catalog):
    """Show reserved and favorite sessions grouped by day, or a Sign in button."""
    title_column, refresh_column, sign_out_column = st.columns(
        [2, 1, 1], vertical_alignment="bottom"
    )
    title_column.subheader("My schedule")

    if st.session_state.get("schedule_error"):
        st.error(st.session_state.schedule_error)

    if not is_signed_in():
        st.info("Sign in with your AWS Builder ID to see your schedule "
                "and add favorites.")
        if st.button("Sign in", type="primary"):
            sign_in(event_id)
            # Rerun so both panels show the signed-in state.
            st.rerun()
        return

    if refresh_column.button("Refresh", width="stretch"):
        refresh_schedule(event_id)
        # Rerun so the icons in the grid on the left update too.
        st.rerun()
    if sign_out_column.button("Sign out", width="stretch"):
        sign_out()
        st.rerun()

    schedule = st.session_state.get("schedule", EMPTY_SCHEDULE)
    groups = group_schedule_by_date(schedule, catalog)
    if not groups:
        st.write("No reserved or favorite sessions yet.")
        return

    for day, entries in groups:
        st.markdown(f"#### {format_day(day)}")
        for entry in entries:
            session = entry.session
            time = session_start_time(session) or "--:--"
            code = session.get("abbreviation", "")
            icons = status_icons(entry.reserved, entry.favorite)
            details = [f"{icons} {entry.status_label()}"]
            if session.get("venue"):
                details.insert(0, session["venue"])
            st.markdown(
                f"**{time}** · {code} {session['title']}  \n"
                f":gray[{' · '.join(details)}]"
            )


def main():
    """Lay out the page: header on top, search on the left, schedule on the right."""
    st.set_page_config(page_title="AWS Events session planner", layout="wide")
    try:
        event_id = render_header()
        new_event = st.session_state.get("schedule_event_id") != event_id
        if new_event and is_signed_in():
            refresh_schedule(event_id)
        session_search = load_search(event_id)
        catalog = session_search.catalog if session_search else None

        for message in st.session_state.pop("flash_messages", []):
            st.toast(message)

        search_column, schedule_column = st.columns([2, 1], gap="large")
        with search_column:
            render_search_panel(event_id, session_search)
        with schedule_column:
            render_schedule_panel(event_id, catalog)
    except EventsError as error:
        st.error(f"Error: {error}")


if __name__ == "__main__":
    main()
