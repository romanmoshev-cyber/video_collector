#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQ_FILE="${APP_DIR}/requirements.txt"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"

cd "${APP_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

CURRENT_REQ_HASH="$(sha256sum "${REQ_FILE}" | awk '{print $1}')"
INSTALLED_REQ_HASH=""
if [[ -f "${REQ_STAMP}" ]]; then
  INSTALLED_REQ_HASH="$(cat "${REQ_STAMP}")"
fi

if [[ "${CURRENT_REQ_HASH}" != "${INSTALLED_REQ_HASH}" ]]; then
  if [[ -z "${INSTALLED_REQ_HASH}" ]] && "${VENV_DIR}/bin/python" - <<'PY'
import importlib.util
import sys

modules = ('aiogram', 'telethon', 'dotenv', 'aiosqlite', 'yt_dlp')
missing = [module for module in modules if importlib.util.find_spec(module) is None]
if missing:
    print('Missing Python modules:', ', '.join(missing), file=sys.stderr)
    sys.exit(1)
PY
  then
    printf '%s\n' "${CURRENT_REQ_HASH}" > "${REQ_STAMP}"
  else
    "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check -r "${REQ_FILE}"
    printf '%s\n' "${CURRENT_REQ_HASH}" > "${REQ_STAMP}"
  fi
fi

exec "${VENV_DIR}/bin/python" "${APP_DIR}/main.py"
