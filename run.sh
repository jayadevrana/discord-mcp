#!/usr/bin/env bash
# Launch the Discord MCP server (stdio). Used by Claude Code / Claude Desktop.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# The `mcp` package requires Python >= 3.10. The system `python3` here is 3.9,
# so pick a newer interpreter (override with DISCORD_MCP_PYTHON=/path/to/python).
PYBIN="${DISCORD_MCP_PYTHON:-}"
if [ -z "$PYBIN" ]; then
  for c in python3.13 python3.12 python3.11 python3.10; do
    command -v "$c" >/dev/null 2>&1 && { PYBIN="$c"; break; }
  done
fi
if [ -z "$PYBIN" ]; then
  echo "Need Python >= 3.10 (the 'mcp' package requires it). Try: brew install python@3.12" >&2
  exit 1
fi

# This project lives on an exFAT volume (/Volumes/NO NAME) where macOS creates
# AppleDouble "._*.pth" companion files. Python's site.py reads every *.pth at
# startup and crashes on those binary files, poisoning any venv built here.
# So we build the venv on the INTERNAL disk instead; the code stays on the drive.
VENV="${DISCORD_MCP_VENV:-$HOME/.local/share/discord-mcp/venv}"

if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$(dirname "$VENV")"
  "$PYBIN" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
fi

cd "$HERE"
exec "$VENV/bin/python" server.py
