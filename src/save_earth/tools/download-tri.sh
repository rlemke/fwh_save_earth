#!/usr/bin/env bash
# Thin shell wrapper. See `download-tri.sh --help` and the module docstring for details.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

if [ -f "${REPO_ROOT}/scripts/_env.sh" ]; then
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/scripts/_env.sh"
fi

PY="${REPO_ROOT}/.venv/bin/python3"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

exec "$PY" "${SCRIPT_DIR}/download_tri.py" "$@"
