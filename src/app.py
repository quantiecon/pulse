"""Vercel FastAPI entrypoint. Local runs still use `pulse serve`."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from berkeleypulse.app import create_app

app = create_app()
