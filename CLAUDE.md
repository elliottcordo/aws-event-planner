# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

A small set of simple python classes (to be done) for the AWS Events API attendee flow , that are then used by a CLI client (`events_cli.py`, to be refactored) and a simple Web UI (to be done).

It uses only the Python standard library and the following third party dependencies if necessary:
* requests
* boto3
* pylint
* langchain-core, fastembed, rank-bm25 (session search; see `requirements.txt`)
* fastapi, uvicorn, jinja2, python-multipart (web UI); httpx (tests only)
* pywebview (desktop window, `events_desktop.py`)
* backoff (exponential retry in `book_when_open.py`)

Do not add additonal third-party dependencies without asking first.
The web UI's only JavaScript library is HTMX 2.0.11, vendored in `web/static/htmx.min.js`;
don't add a Node toolchain or build step.

## Environment

- The project uses a virtual environment in `venv/`. Run Python as
  `venv/bin/python` (or activate it first); do not install into the system Python.
- Install dependencies with `venv/bin/pip install -r requirements.txt`, and add any
  newly approved dependency to `requirements.txt`.

## Code Style

- **Simple, readable code is a must.** Prefer the plain, obvious solution over a
  clever one. If a reader has to pause to work out what a line does, rewrite it.
- No one-liner tricks, nested comprehensions, or dense chained expressions when
  a few clear lines would do.
- Keep functions small and focused on one job. Avoid unnecessary abstraction,
  classes, or indirection.
- Useful unit tests are mandatory, build functions and classes that enable  testability.
- Use descriptive names for variables, functions, and constants. No cryptic
  abbreviations.
- Follow **PEP 8**: 4-space indentation, `snake_case` for functions and
  variables, `UPPER_CASE` for constants, `PascalCase` for classes, and lines
  of 88 characters or fewer.
- Group imports as standard library, then third-party, then local, with a
  blank line between groups.
- Handle errors explicitly with clear messages. Never use a bare `except:`.

## Docstrings

- **Every module, class, and function gets a docstring**, including small
  helpers.
- Follow PEP 257: a one-line summary in the imperative mood ("Return...",
  "Fetch..."), then a blank line and more detail if needed.
- Document arguments, return values, and raised exceptions when they are not
  obvious from the signature.

```python
def fetch_session(event_id, session_id):
    """Fetch a single session from the Events API.

    Args:
        event_id: The event identifier, e.g. "reinvent2026".
        session_id: The session identifier.

    Returns:
        The session as a dict.

    Raises:
        RuntimeError: If the API request fails.
    """
```

## Comments

- Comment *why*, not *what*. The code should explain what it does.
- Keep comments short and up to date.

## Before Finishing

- Check formatting and style (`venv/bin/python -m pylint aws_events events_cli.py`
  if pylint is installed).
- Run the tests: `venv/bin/python -m unittest discover -s tests -t .`
- Make sure the script still runs: `venv/bin/python events_cli.py --help`.
- Web UI changes: keep `tests/test_web_app.py` and `tests/test_skins.py` passing, and
  check the page in a browser (`venv/bin/python events_web.py`) with a few skins and
  with "No skin".
- Keep `README.md` in step with CLI changes.
