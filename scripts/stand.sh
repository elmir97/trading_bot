#!/usr/bin/env bash
set -euo pipefail
if [[ -z ${STAND_PYTHON:-} ]]; then
    echo 'Set STAND_PYTHON to the Python 3.12 virtualenv executable.' >&2
    exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
exec "$STAND_PYTHON" -m scripts.stand "$@"
