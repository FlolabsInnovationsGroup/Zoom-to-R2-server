"""Vercel entry point: the admin page, the MCP server and the daily sync cron, as one serverless function.

Vercel serves the ASGI `app` below; vercel.json routes every path here. Locally, run `python -m zoom_archive.web` from sync/
instead. The SQLite files live in R2 here (DB_IN_R2=1, see sync/zoom_archive/r2db.py).
"""
import logging
import os
import sys
from pathlib import Path

# Sync progress and warnings go to Vercel's function logs (never a URL: the MCP key is in the path).
logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s", force=True)
for noisy in ("urllib3", "botocore", "httpx", "mcp"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sync"))
os.environ.setdefault("DB_IN_R2", "1")
os.environ.setdefault("ZOOM_ARCHIVE_NO_DOTENV", "1")  # settings come from Vercel's environment variables

from zoom_archive.web import create_app  # noqa: E402

app = create_app()
