#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PYTHON_BIN="${BACKLOGTALK_PYTHON:-python3}"
if [[ "$(uname -s)" == Darwin ]]; then
  mac_major=$(sw_vers -productVersion | cut -d. -f1)
  if [[ "$mac_major" =~ ^[0-9]+$ ]] && (( mac_major < 12 )); then
    echo 'Se requiere macOS 12 o superior.' >&2
    exit 1
  fi
  printf 'Para macOS se recomienda Python 3.12; para elegirlo: BACKLOGTALK_PYTHON=python3.12 ./scripts/install.sh\n'
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 12), "Se necesita Python 3.12 o superior"'
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -I scripts/bootstrap_pip.py
.venv/bin/python -m pip install -r requirements.txt
printf '\nInstalación Python terminada. Ejecutá ./scripts/run.sh\n'
printf 'También necesitás Cursor CLI, TWG CLI y Ollama (si usás el modelo local).\n'
