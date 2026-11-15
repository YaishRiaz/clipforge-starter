# One-time setup: virtualenv, dependencies, .env and the database.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.11+ is required" }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "ffmpeg is required and must be on PATH" }
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is required" }

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example. Fill it in (docs/SETUP.md). It is git-ignored; never commit it."
}

.\.venv\Scripts\python.exe -m clipforge init-db
Write-Host "Done. Next: edit .env, then see docs/SETUP.md"
