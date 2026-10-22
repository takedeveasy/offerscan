#!/usr/bin/env bash
# Instala radar en este directorio (macOS, Linux y Git Bash en Windows).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if command -v python3 >/dev/null 2>&1; then
  py=python3
elif command -v python >/dev/null 2>&1; then
  py=python
else
  echo "No encuentro Python. Instálalo desde https://www.python.org/downloads/ (3.10 o superior, marca 'Add Python to PATH')."
  exit 1
fi

ver="$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"
"$py" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" || {
  echo "Hace falta Python 3.10 o superior. Tienes $ver."
  exit 1
}

if [[ ! -d .venv ]]; then
  echo "Creando entorno .venv con $py ($ver)..."
  "$py" -m venv .venv
fi

venv_py="$root/.venv/bin/python"
[[ -x "$venv_py" ]] || venv_py="$root/.venv/Scripts/python.exe"

"$venv_py" -m pip install --upgrade pip
"$venv_py" -m pip install -e ".[ui]"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Creado .env (no hace falta rellenarlo para el uso básico)."
fi

echo "Instalando el navegador para los portales que bloquean (InfoJobs, Indeed, Glassdoor)..."
offerscan_bin="$root/.venv/bin/offerscan"
[[ -x "$offerscan_bin" ]] || offerscan_bin="$root/.venv/Scripts/offerscan.exe"
browser_args=""
# Linux needs system libraries for Chromium; they can only be installed as root.
if [[ "$(uname -s)" == "Linux" && "$(id -u)" == "0" ]]; then
  browser_args="--with-deps"
fi
browser_ok=1
# shellcheck disable=SC2086  # empty or a single flag
"$offerscan_bin" install-browser $browser_args || browser_ok=0

echo
if [[ "$browser_ok" == "0" ]]; then
  echo "AVISO: el navegador no quedó listo. El resto funciona; para InfoJobs, Indeed y Glassdoor"
  echo "repite más tarde (con el entorno activado): offerscan install-browser"
  if [[ "$(uname -s)" == "Linux" ]]; then
    echo "En Linux, si faltan librerías: sudo .venv/bin/python -m patchright install-deps chromium"
  fi
  echo
fi
echo "Listo. Arranca la interfaz con:"
if [[ -x "$root/.venv/bin/offerscan" ]]; then
  echo "  source .venv/bin/activate"
  echo "  offerscan ui"
else
  echo "  .venv\\Scripts\\Activate.ps1"
  echo "  offerscan ui"
fi
