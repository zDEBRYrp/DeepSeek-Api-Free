#!/usr/bin/env bash
set -euo pipefail

# Always run from the OwnApi directory, regardless of the current directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

OWNAPI_HOST="${OWNAPI_HOST:-127.0.0.1}"
OWNAPI_PORT="${OWNAPI_PORT:-8000}"

if [[ -x ".venv/bin/python" ]]; then
    OWNAPI_PYTHON="$SCRIPT_DIR/.venv/bin/python"
elif [[ -f ".venv/Scripts/python.exe" ]]; then
    # Windows virtualenv from Git Bash.
    OWNAPI_PYTHON="$SCRIPT_DIR/.venv/Scripts/python.exe"
else
    printf '%s\n' \
        "[ERROR] Python environment was not found in $SCRIPT_DIR/.venv" \
        "" \
        "Create it first:" \
        "  python -m venv .venv" \
        "  .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

while true; do
    printf '%s\n' \
        "Browser mode:" \
        "  [1] Headless - browser runs hidden (default)" \
        "  [2] Visible  - open the browser window"
    read -r -p "Select 1 or 2 [1]: " OWNAPI_MODE || OWNAPI_MODE=""
    OWNAPI_MODE="${OWNAPI_MODE:-1}"
    case "$OWNAPI_MODE" in
        1)
            export HEADLESS=true
            OWNAPI_BROWSER_LABEL="headless"
            break
            ;;
        2)
            export HEADLESS=false
            OWNAPI_BROWSER_LABEL="visible"
            break
            ;;
        *)
            printf 'Invalid choice. Enter 1, 2, or press Enter for headless.\n\n' >&2
            ;;
    esac
done

printf '\n'
printf '%s\n' \
    "============================================================" \
    " OwnApi" \
    " Browser: ${OWNAPI_BROWSER_LABEL}" \
    " API:    http://${OWNAPI_HOST}:${OWNAPI_PORT}" \
    " Health: http://${OWNAPI_HOST}:${OWNAPI_PORT}/healthz" \
    " UI:     http://${OWNAPI_HOST}:${OWNAPI_PORT}/ui" \
    " Stop:   Ctrl+C" \
    "============================================================" \
    ""

exec "$OWNAPI_PYTHON" -m uvicorn app.main:app \
    --host "$OWNAPI_HOST" \
    --port "$OWNAPI_PORT" \
    "$@"
