"""Vercel ASGI entrypoint for the FireViewer API.

Vercel runs ``api/index.py`` with the ``api`` directory on ``sys.path``.  The
application itself follows the standard ``src/`` layout, so make that source
root explicit before importing the ASGI application.  Keeping this adjustment
at the deployment boundary preserves the normal local/package import contract.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

app = import_module("fire_viewer.main").app

__all__ = ["app"]
