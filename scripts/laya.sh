#!/usr/bin/env bash
# Servidor local de Laya (motor gratuito). Uso:
#   bash scripts/laya.sh --install
#   bash scripts/laya.sh
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
venv="$root/.venv-laya"
py="$venv/bin/python"
serve="$venv/bin/laya-serve"
port="${LAYA_PORT:-8000}"

if [[ "${1:-}" == "--install" || "${1:-}" == "-Install" ]]; then
  python3 -m venv "$venv"
  "$py" -m pip install --upgrade pip
  "$py" -m pip install "laya[serve]"
  echo "Laya instalado en $venv (la primera ejecución descarga ~650 MB de modelo)."
  exit 0
fi

if [[ ! -x "$serve" ]]; then
  echo "No existe $serve. Ejecuta primero: bash scripts/laya.sh --install"
  exit 1
fi

export LAYA_MODELS=multilingual
export LAYA_HOST=127.0.0.1
export LAYA_PORT="$port"
echo "Laya escuchando en http://127.0.0.1:$port (Ctrl+C para parar)"
exec "$serve"
