#!/usr/bin/env python3
"""Command-line client for the AWS Events API attendee flow.

Usage examples:
    python3 events_cli.py login
    python3 events_cli.py events
    python3 events_cli.py sessions reinvent2026 --no-abstracts
    python3 events_cli.py schedule reinvent2026
    python3 events_cli.py schedule reinvent2026 --details
    python3 events_cli.py book reinvent2026 SESSION_ID [SESSION_ID ...]
    python3 events_cli.py book-favorites reinvent2026
    python3 events_cli.py add-personal-time reinvent2026 --title Lunch \\
        --description "Team lunch" --start 2026-12-02T19:00:00 \\
        --end 2026-12-02T20:00:00

Session search:
    python3 events_cli.py download-sessions reinvent2026
    python3 events_cli.py build-index reinvent2026
    python3 events_cli.py search reinvent2026 "serverless event-driven design"
    python3 events_cli.py search reinvent2026 "EKS" --venue "MGM Grand" \
        --date 2026-12-02
"""

import argparse
import json
import sys
from pathlib import Path

from aws_events import (
    SEARCH_MODES,
    Authenticator,
    EventsClient,
    EventsError,
    FastEmbedEmbeddings,
    HttpTransport,
    PersonalTime,
    SessionCatalog,
    SessionFilter,
    SessionSearch,
    SessionVectorStore,
    TokenStore,
    default_catalog_path,
    default_index_path,
    open_url,
    session_venue,
)
from aws_events.booking import reserve_in_batches, session_ids_waiting_to_book


def open_browser_with_fallback(url):
    """Open the sign-in URL, printing it if no browser could be opened."""
    print("Opening AWS Builder ID sign-in in your browser...")
    if not open_url(url):
        print(f"Open this URL manually:\n{url}")


def build_parser():
    """Return the argument parser with one subcommand per API action."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", metavar="command", required=True)

    commands.add_parser("login", help="Sign in with AWS Builder ID")
    commands.add_parser("logout", help="Forget the saved sign-in")

    events = commands.add_parser("events", help="List events")
    events.add_argument(
        "--include-past", action="store_true", help="Also list events that have ended"
    )

    event = commands.add_parser("event", help="Show one event")
    add_event_id(event)

    sessions = commands.add_parser("sessions", help="List all sessions in an event")
    add_event_id(sessions)
    sessions.add_argument("--locale", help="Language, for example en-US")
    sessions.add_argument(
        "--no-abstracts", action="store_true", help="Leave out session abstracts"
    )

    session = commands.add_parser("session", help="Show one session")
    add_event_id(session)
    session.add_argument("session_id")
    session.add_argument("--locale", help="Language, for example en-US")

    schedule = commands.add_parser("schedule", help="Show your schedule")
    add_event_id(schedule)
    schedule.add_argument(
        "--details",
        action="store_true",
        help="Show full session details instead of IDs "
        "(one extra request per session)",
    )
    schedule.add_argument(
        "--locale", help="Language for session details, for example en-US"
    )

    # "book" is the everyday word; the API calls it a reservation. The
    # defaults make args.command the same whichever name is typed.
    reserve = commands.add_parser(
        "reserve", aliases=["book"], help="Book (reserve) one to ten sessions"
    )
    reserve.set_defaults(command="reserve")
    add_event_id(reserve)
    reserve.add_argument("session_ids", nargs="+", metavar="session_id")

    cancel = commands.add_parser(
        "cancel", aliases=["unbook"], help="Cancel a booking (reservation)"
    )
    cancel.set_defaults(command="cancel")
    add_event_id(cancel)
    cancel.add_argument("session_id")

    favorite = commands.add_parser("favorite", help="Favorite one to ten sessions")
    add_event_id(favorite)
    favorite.add_argument("session_ids", nargs="+", metavar="session_id")

    unfavorite = commands.add_parser("unfavorite", help="Remove a favorite")
    add_event_id(unfavorite)
    unfavorite.add_argument("session_id")

    book_favorites = commands.add_parser(
        "book-favorites",
        help="Book unbooked favorites (interactive sessions by default)",
    )
    add_event_id(book_favorites)
    add_catalog_path(book_favorites)
    book_favorites.add_argument(
        "--all",
        action="store_true",
        help="Include breakouts, not only chalk talks, workshops, and labs",
    )

    add_time = commands.add_parser(
        "add-personal-time", help="Add a personal time block"
    )
    add_event_id(add_time)
    add_personal_time_fields(add_time)

    update_time = commands.add_parser(
        "update-personal-time", help="Replace a personal time block"
    )
    add_event_id(update_time)
    update_time.add_argument("personal_time_id")
    add_personal_time_fields(update_time)

    delete_time = commands.add_parser(
        "delete-personal-time", help="Delete a personal time block"
    )
    add_event_id(delete_time)
    delete_time.add_argument("personal_time_id")

    download = commands.add_parser(
        "download-sessions", help="Save every session of an event to JSON"
    )
    add_event_id(download)
    add_catalog_path(download)
    download.add_argument("--locale", help="Language, for example en-US")

    build_index = commands.add_parser(
        "build-index", help="Embed the saved sessions for search"
    )
    add_event_id(build_index)
    add_catalog_path(build_index)
    add_index_path(build_index)

    search = commands.add_parser("search", help="Search the saved sessions")
    add_event_id(search)
    search.add_argument("query")
    search.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        default="hybrid",
        help="How to match the query (default: hybrid)",
    )
    search.add_argument(
        "--limit", type=int, default=10, help="Maximum number of results (default: 10)"
    )
    search.add_argument("--venue", help='Only this venue, for example "MGM Grand"')
    search.add_argument("--date", help="Only sessions on this day, as YYYY-MM-DD")
    add_catalog_path(search)
    add_index_path(search)

    return parser


def add_event_id(parser):
    """Add the event_id positional argument to a subcommand."""
    parser.add_argument("event_id", help="Event ID, for example reinvent2026")


def add_catalog_path(parser):
    """Add the --catalog option for the saved sessions file."""
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Sessions JSON file " "(default: <project>/data/EVENT-sessions.json)",
    )


def add_index_path(parser):
    """Add the --index option for the saved vector store file."""
    parser.add_argument(
        "--index",
        type=Path,
        help="Search index file " "(default: <project>/data/EVENT-vectors.json)",
    )


def add_personal_time_fields(parser):
    """Add the options that describe a personal time block."""
    parser.add_argument("--title", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument(
        "--start", required=True, help="UTC start, for example 2026-12-02T16:00:00"
    )
    parser.add_argument(
        "--end", required=True, help="UTC end, for example 2026-12-02T17:00:00"
    )
    parser.add_argument("--location")


def personal_time_from_args(args):
    """Build a PersonalTime from parsed command-line arguments."""
    return PersonalTime(
        title=args.title,
        description=args.description,
        start_date_time=args.start,
        end_date_time=args.end,
        location=args.location,
    )


def load_catalog(path):
    """Load a saved SessionCatalog, explaining how to create it if missing."""
    try:
        return SessionCatalog.load(path)
    except FileNotFoundError as error:
        raise EventsError(
            f"No sessions saved at {path}. Run download-sessions first."
        ) from error


def load_vector_store(path, embeddings):
    """Load a saved SessionVectorStore, explaining how to create it if missing."""
    try:
        return SessionVectorStore.load(path, embeddings)
    except FileNotFoundError as error:
        raise EventsError(
            f"No search index at {path}. Run build-index first."
        ) from error


def format_search_results(results):
    """Return search results as readable text, one session per block."""
    if not results:
        return "No matching sessions."
    blocks = []
    for number, result in enumerate(results, start=1):
        session = result.session
        session_time = session.get("sessionTime") or {}
        when = f"{session_time.get('date', '')} {session_time.get('time', '')}"
        details = [when.strip() or "Time not set"]
        venue = session_venue(session)
        if venue:
            details.append(venue)
        details.append(f"score {result.score:.3f}")
        details.append(f"id {session['sessionId']}")
        blocks.append(
            f"{number}. [{session.get('abbreviation', '')}] {session['title']}\n"
            f"   {' | '.join(details)}"
        )
    return "\n".join(blocks)


def run_command(args, client, authenticator, embeddings=None):
    """Run the chosen subcommand and return what should be printed.

    Args:
        args: Parsed command-line arguments.
        client: The EventsClient to call.
        authenticator: The Authenticator used by login and logout.
        embeddings: Embeddings for the search commands; defaults to
            FastEmbedEmbeddings. Tests pass a fake.

    Returns:
        A JSON-serializable result, or a plain message string.
    """
    command = args.command
    if embeddings is None:
        embeddings = FastEmbedEmbeddings()

    if command == "login":
        authenticator.get_access_token()
        return "Signed in."
    if command == "logout":
        authenticator.sign_out()
        return "Signed out."

    if command == "events":
        return client.list_events(include_past=args.include_past)
    if command == "event":
        return client.get_event(args.event_id)
    if command == "sessions":
        return client.list_sessions(
            args.event_id,
            locale=args.locale,
            include_abstracts=not args.no_abstracts,
        )
    if command == "session":
        return client.get_session(args.event_id, args.session_id, locale=args.locale)

    if command == "schedule":
        return client.get_schedule(
            args.event_id,
            include_session_details=args.details,
            locale=args.locale,
        )
    if command == "reserve":
        return client.reserve_sessions(args.event_id, args.session_ids)
    if command == "cancel":
        client.cancel_reservation(args.event_id, args.session_id)
        return "Reservation cancelled."
    if command == "favorite":
        return client.add_favorites(args.event_id, args.session_ids)
    if command == "unfavorite":
        client.remove_favorite(args.event_id, args.session_id)
        return "Favorite removed."
    if command == "book-favorites":
        schedule = client.get_schedule(args.event_id)
        catalog = None
        if not args.all:
            catalog = load_catalog(args.catalog or default_catalog_path(args.event_id))
        session_ids = session_ids_waiting_to_book(
            schedule, catalog=catalog, interactive_only=not args.all
        )
        if not session_ids:
            return "No unbooked favorites to reserve."
        return reserve_in_batches(client, args.event_id, session_ids)

    if command == "add-personal-time":
        client.create_personal_time(args.event_id, personal_time_from_args(args))
        return "Personal time added."
    if command == "update-personal-time":
        client.update_personal_time(
            args.event_id, args.personal_time_id, personal_time_from_args(args)
        )
        return "Personal time updated."
    if command == "delete-personal-time":
        client.delete_personal_time(args.event_id, args.personal_time_id)
        return "Personal time deleted."

    if command == "download-sessions":
        catalog = SessionCatalog.download(client, args.event_id, locale=args.locale)
        catalog_path = args.catalog or default_catalog_path(args.event_id)
        catalog.save(catalog_path)
        return f"Saved {len(catalog)} sessions to {catalog_path}."
    if command == "build-index":
        catalog = load_catalog(args.catalog or default_catalog_path(args.event_id))
        vector_store = SessionVectorStore.build(catalog, embeddings)
        index_path = args.index or default_index_path(args.event_id)
        vector_store.save(index_path)
        return f"Indexed {len(catalog)} sessions to {index_path}."
    if command == "search":
        catalog = load_catalog(args.catalog or default_catalog_path(args.event_id))
        vector_store = load_vector_store(
            args.index or default_index_path(args.event_id), embeddings
        )
        session_search = SessionSearch(catalog, vector_store)
        session_filter = SessionFilter(venue=args.venue, date=args.date)
        results = session_search.search(
            args.query, limit=args.limit, mode=args.mode, session_filter=session_filter
        )
        return format_search_results(results)

    raise ValueError(f"Unknown command: {command}")


def print_result(result):
    """Print a message as-is, or anything else as indented JSON."""
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2))


def main(argv=None):
    """Parse arguments, run the command, and return the process exit code."""
    args = build_parser().parse_args(argv)

    transport = HttpTransport()
    authenticator = Authenticator(
        TokenStore(), transport, open_browser=open_browser_with_fallback
    )
    client = EventsClient(transport, authenticator)

    try:
        result = run_command(args, client, authenticator)
    except (EventsError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
