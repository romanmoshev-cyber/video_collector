#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${APP_DIR}/venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQ_FILE="${APP_DIR}/requirements.txt"
REQ_STAMP="${VENV_DIR}/.requirements.sha256"
REQUIRED_MODULES=(aiogram telethon dotenv aiosqlite yt_dlp)

cd "${APP_DIR}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

CURRENT_REQ_HASH="$(sha256sum "${REQ_FILE}" | awk '{print $1}')"
INSTALLED_REQ_HASH=""
if [[ -f "${REQ_STAMP}" ]]; then
  INSTALLED_REQ_HASH="$(cat "${REQ_STAMP}")"
fi

MISSING_MODULES="$("${VENV_DIR}/bin/python" - "${REQUIRED_MODULES[@]}" <<'PY'
import importlib.util
import sys

missing = [module for module in sys.argv[1:] if importlib.util.find_spec(module) is None]
print(' '.join(missing))
PY
)"

if [[ "${CURRENT_REQ_HASH}" != "${INSTALLED_REQ_HASH}" || -n "${MISSING_MODULES}" ]]; then
  if [[ -n "${MISSING_MODULES}" ]]; then
    printf 'Missing Python modules in %s: %s\n' "${VENV_DIR}" "${MISSING_MODULES}" >&2
  fi
  "${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check -r "${REQ_FILE}"
  printf '%s\n' "${CURRENT_REQ_HASH}" > "${REQ_STAMP}"
fi

exec "${VENV_DIR}/bin/python" "${APP_DIR}/main.py"
