"""Vercel serverless entrypoint for ruapi (FastAPI via Mangum).

Vercel runs this as a Python serverless function. The FastAPI app lives
in main.py at the repo root, so add the root to sys.path first.
Note: Vercel's filesystem is ephemeral per invocation, so the SQLite
credits DB does not reliably persist between calls on Hobby — keys and
balances are demo-only here. The API logic is unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mangum import Mangum  # noqa: E402
from main import app  # noqa: E402

handler = Mangum(app, lifespan="off")
