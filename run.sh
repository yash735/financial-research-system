#!/usr/bin/env bash
# =============================================================================
# Start the Financial Research System locally.
#
#   ./run.sh                  formatter service + web app
#   ./run.sh --no-formatter   web app only, exercising the in-process fallback
#   ./run.sh --fresh          clear the OCR cache first
#   ./run.sh --port 9000      serve the web app on a different port
#
# Ctrl-C stops everything.
#
# NOTE ON `python -m`: every command here goes through `python -m <module>`
# rather than the venv's console scripts (adk, uvicorn, ...). Those scripts hard
# code an absolute interpreter path in their shebang, so they break the moment a
# virtualenv is moved — and a shebang cannot express a path containing a space
# at all. `python -m` is immune to both.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WITH_FORMATTER=1
FRESH=0
PORT_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-formatter) WITH_FORMATTER=0; shift ;;
    --fresh)        FRESH=1; shift ;;
    --port)         PORT_OVERRIDE="${2:-}"; shift 2 ;;
    -h|--help)      sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# --- interpreter ------------------------------------------------------------
if [ -x "$SCRIPT_DIR/adk_env/bin/python" ]; then
  PY="$SCRIPT_DIR/adk_env/bin/python"
elif [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
  PY="$SCRIPT_DIR/.venv/bin/python"
elif [ -n "${VIRTUAL_ENV:-}" ]; then
  PY="$VIRTUAL_ENV/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

if [ -z "$PY" ] || ! "$PY" -c "import google.adk" >/dev/null 2>&1; then
  echo "ERROR: no Python environment with google-adk installed." >&2
  echo "       python -m venv adk_env && ./adk_env/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

# --- configuration ----------------------------------------------------------
if [ ! -f .env ]; then
  echo "ERROR: no .env found. Start from the template:" >&2
  echo "       cp .env.example .env    # then fill in the REQUIRED values" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

WEBAPP_HOST="${WEBAPP_HOST:-127.0.0.1}"
WEBAPP_PORT="${PORT_OVERRIDE:-${WEBAPP_PORT:-8000}}"
A2A_PORT="${A2A_PORT:-8001}"
A2A_HOST="${A2A_HOST:-localhost}"
export WEBAPP_PORT

if [ "$FRESH" = "1" ]; then
  rm -rf "$SCRIPT_DIR/.cache/ocr"
  echo "==> Cleared the OCR cache."
fi

# --- shutdown ---------------------------------------------------------------
FORMATTER_PID=""
cleanup() {
  echo
  echo "==> Stopping…"
  [ -n "$FORMATTER_PID" ] && kill "$FORMATTER_PID" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM

mkdir -p "$SCRIPT_DIR/.run"

# --- formatter A2A service --------------------------------------------------
if [ "$WITH_FORMATTER" = "1" ]; then
  if lsof -nP -iTCP:"$A2A_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "==> Formatter already listening on :$A2A_PORT — reusing it."
  else
    echo "==> Starting the formatter A2A service on :${A2A_PORT}…"
    "$PY" -m uvicorn formatter_agent.agent:a2a_app \
      --host "$A2A_HOST" --port "$A2A_PORT" \
      > "$SCRIPT_DIR/.run/formatter.log" 2>&1 &
    FORMATTER_PID=$!

    # The coordinator dials the agent card, so wait until it actually resolves
    # rather than racing it. If it never comes up the app degrades to the
    # in-process formatter instead of failing.
    for _ in $(seq 1 30); do
      curl -sf -m 2 "http://${A2A_HOST}:${A2A_PORT}/.well-known/agent-card.json" \
        >/dev/null 2>&1 && break
      sleep 1
    done
    if curl -sf -m 2 "http://${A2A_HOST}:${A2A_PORT}/.well-known/agent-card.json" >/dev/null 2>&1; then
      echo "    agent card is up."
    else
      echo "    WARNING: the formatter did not come up — see .run/formatter.log."
      echo "    The app will use its in-process fallback."
    fi
  fi
else
  echo "==> Skipping the formatter (--no-formatter): exercising the in-process fallback."
fi

# --- web app ----------------------------------------------------------------
echo "==> Starting the web app…"
echo
echo "    ┌─────────────────────────────────────────────┐"
printf "    │  http://%s:%-*s│\n" "$WEBAPP_HOST" $((36 - ${#WEBAPP_HOST})) "$WEBAPP_PORT  "
echo "    └─────────────────────────────────────────────┘"
echo

"$PY" -m uvicorn webapp.main:app --host "$WEBAPP_HOST" --port "$WEBAPP_PORT"
cleanup
