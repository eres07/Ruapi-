#!/bin/sh
# Start ruapi from its own directory, whatever cwd the host uses.
# Prefer the project's venv (where the build installed dependencies).
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python main.py
fi
if [ -x /app/.venv/bin/python ]; then
  exec /app/.venv/bin/python main.py
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 main.py
else
  exec python main.py
fi
