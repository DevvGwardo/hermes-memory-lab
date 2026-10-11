#!/bin/sh
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${MEMORY_SIM_PYTHON:-}
if [ -z "$PYTHON" ]; then
    for candidate in "$DIR/.venv/bin/python" \
        "$HOME/.hermes/hermes-agent/.venv/bin/python" \
        "$HOME"/.hermes/installs/*/test-environment/*/venv/bin/python; do
        if [ -x "$candidate" ]; then
            PYTHON=$candidate
            break
        fi
    done
fi
if [ ! -x "$PYTHON" ]; then
    printf '%s\n' "Set MEMORY_SIM_PYTHON to a Python with the installed Hermes test dependencies." >&2
    exit 2
fi
exec env PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B "$DIR/simulator.py" "$@"
