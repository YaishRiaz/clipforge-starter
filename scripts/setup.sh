#!/usr/bin/env bash
# One-time setup: virtualenv, dependencies, .env and the database.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null || { echo "Python 3.11+ is required"; exit 1; }
command -v ffmpeg  >/dev/null || { echo "ffmpeg is required and must be on PATH"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ is required"; exit 1; }

[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Fill it in (docs/SETUP.md). It is git-ignored; never commit it."
fi

.venv/bin/python -m clipforge init-db
echo "Done. Next: edit .env, then see docs/SETUP.md"
