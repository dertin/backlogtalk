#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PATH="$PATH:$HOME/.local/bin"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Falta instalar dependencias: ./scripts/install.sh' >&2
  exit 1
fi
if [[ "$(uname -s)" == Linux && -d .venv/portaudio ]]; then
  export LD_LIBRARY_PATH="$PWD/.venv/portaudio${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec .venv/bin/python -m backlogtalk.qt_app "$@"
