#!/usr/bin/env bash
# Live Relay launcher for Linux and macOS. Mirrors start_live_relay.bat.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUTF8=1

if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    echo "Missing .env.example. Re-extract the complete project folder." >&2
    exit 1
  fi
  cp .env.example .env
  chmod 600 .env
  echo "Created the local settings file. Configure it in the dashboard after it opens."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required. Install Python and try again." >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

if ! ".venv/bin/python" -c "import live_relay, yt_dlp, curl_cffi, imageio_ffmpeg, dotenv, requests, googleapiclient, google_auth_oauthlib; raise SystemExit(tuple(map(int, yt_dlp.version.__version__.split('.')[:3])) < (2026, 10, 21))" >/dev/null 2>&1; then
  echo "Installing project dependencies for the first time or repairing the environment..."
  ".venv/bin/python" -m pip install --disable-pip-version-check -e .
fi

".venv/bin/python" -m live_relay || echo "The dashboard stopped with an error. Check live-relay.log in your configuration folder."
