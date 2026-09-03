#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Single definition of how this project's dependencies get installed.
#
# requirements.txt pins websockets above the caps google-adk and google-genai
# declare (see overrides.txt for why), so neither `pip install -r
# requirements.txt` nor `uv pip install -r requirements.txt` can resolve it.
#
# uv has a first-class flag for this. pip does not -- --constraint can only
# tighten an upper bound, never loosen one -- so the pip path installs
# everything else first and then forces the pin, which leaves the same result.
#
# Usage: ./scripts/install_deps.sh [--dev] [extra pip/uv args...]

REQ_FILE="requirements.txt"
OVERRIDES="overrides.txt"

WANT_DEV=false
if [ "${1:-}" = "--dev" ]; then
    WANT_DEV=true
    shift
fi

# The single package the override applies to, read from overrides.txt rather
# than repeated here, so there is one source of truth.
OVERRIDE_PIN=$(grep -E '^[a-zA-Z]' "$OVERRIDES" | head -n1)

if command -v uv >/dev/null 2>&1; then
    # This project installs into the ambient interpreter, not a venv, and uv
    # refuses to do that without --system. Passing it unconditionally would be
    # wrong inside an activated venv, so key off VIRTUAL_ENV.
    UV_TARGET=()
    if [ -z "${VIRTUAL_ENV:-}" ]; then
        UV_TARGET=(--system)
    fi

    echo "Installing with uv (--override ${OVERRIDES})..."
    uv pip install "${UV_TARGET[@]}" --override "$OVERRIDES" -r "$REQ_FILE" "$@"
    if [ "$WANT_DEV" = true ]; then
        uv pip install "${UV_TARGET[@]}" --override "$OVERRIDES" -r requirements-dev.txt "$@"
    fi
else
    echo "Installing with pip (two-step; pip has no --override)..."
    # Everything except the overridden package, so its transitive deps all land.
    grep -v '^websockets' "$REQ_FILE" | pip install -r /dev/stdin "$@"
    # Then force the pin, skipping deps so pip does not re-resolve the cap.
    pip install --no-deps "$OVERRIDE_PIN" "$@"
    if [ "$WANT_DEV" = true ]; then
        grep -v '^websockets' requirements-dev.txt | pip install -r /dev/stdin "$@"
    fi
fi

echo
echo "Installed: $(python3 -c 'import websockets; print("websockets", websockets.__version__)')"
echo "pip check will report the deliberate websockets conflict; that is expected."
