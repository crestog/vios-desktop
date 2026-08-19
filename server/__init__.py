"""
server — the HTTP surface, as one application.

`python -m server` runs it bare on `atlas.config.PORT`, which is useful for
`npm run dev` to proxy against and for reading `/api/docs`. The real entry point
is `python -m desktop`, which picks a free port and opens a window on it.
"""

from .app import app, create_app          # noqa: F401

__all__ = ["app", "create_app"]
