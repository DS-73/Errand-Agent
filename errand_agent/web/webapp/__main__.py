"""Run the web app with the built frontend.

    python -m webapp
"""
from __future__ import annotations

from errand_agent.web.webapp.backend.api import main

if __name__ == "__main__":
    raise SystemExit(main())
