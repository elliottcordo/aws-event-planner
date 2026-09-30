#!/usr/bin/env python3
"""Start AWS Event AMP, the web app, at http://127.0.0.1:8000.

Usage:
    python3 events_web.py [--port 8000]
"""

import argparse

import uvicorn

from web.app import create_app
from web.services import Services


def main():
    """Parse arguments and run the web server until stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app = create_app(Services.create_default())
    # Only this machine can reach the app: it holds your AWS Builder ID sign-in.
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
