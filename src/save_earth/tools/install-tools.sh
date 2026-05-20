#!/usr/bin/env bash
# Install Python dependencies required by the save-earth tool set.
#
# Currently: just ``requests`` for HTTP fetching from the upstream
# data sources (OpenLitterMap, EPA ArcGIS REST endpoints). The map
# renderer writes MapLibre HTML directly; MapLibre is pulled from a
# CDN at render time, so no static asset install is needed.
#
# Installs into ${REPO_ROOT}/.venv via ``python -m pip``. Idempotent —
# re-running only installs what's missing. Uses ``python -m pip``
# instead of ``.venv/bin/pip`` because the latter's shebang is
# hardcoded with the interpreter path at venv-creation time and
# breaks if the repo is renamed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python3"

if [ -t 1 ]; then
    BOLD=$'\033[1m'
    GREEN=$'\033[32m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD="" GREEN="" RED="" RESET=""
fi

log()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '%s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
fail() { printf '%s[fail]%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

if [ ! -x "$PY" ]; then
    fail ".venv not found at ${PY}. Create it first: python3.13 -m venv ${REPO_ROOT}/.venv"
fi

REQUIREMENTS=(
    "requests:requests"
)

MISSING=()
for spec in "${REQUIREMENTS[@]}"; do
    pkg="${spec%%:*}"
    mod="${spec##*:}"
    if "$PY" -c "import ${mod}" 2>/dev/null; then
        ok "${pkg} already installed"
    else
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    log "installing: ${MISSING[*]}"
    "$PY" -m pip install --disable-pip-version-check "${MISSING[@]}"
    for spec in "${REQUIREMENTS[@]}"; do
        pkg="${spec%%:*}"
        mod="${spec##*:}"
        if "$PY" -c "import ${mod}" 2>/dev/null; then
            ok "${pkg} installed"
        else
            fail "${pkg} still not importable — check the pip output"
        fi
    done
fi

log "save-earth tool dependencies ready"
