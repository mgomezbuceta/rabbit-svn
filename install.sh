#!/usr/bin/env bash
# Instala RabbitSVN para el usuario actual (sin sudo):
#  - crea .venv con PySide6 y keyring
#  - lanzador ~/.local/bin/rabbit-svn
#  - entrada de menú ~/.local/share/applications/rabbit-svn.desktop
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-/usr/bin/python3}"

command -v svn >/dev/null || { echo "Falta subversion: sudo apt install subversion"; exit 1; }
"$PY" -c "import venv, ensurepip" 2>/dev/null || { echo "Falta venv: sudo apt install python3-venv"; exit 1; }

echo "» Creando entorno virtual con $PY"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

chmod +x "$DIR/run.sh"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
ln -sf "$DIR/run.sh" "$HOME/.local/bin/rabbit-svn"

sed "s|@DIR@|$DIR|g" "$DIR/packaging/rabbit-svn.desktop.in" > "$HOME/.local/share/applications/rabbit-svn.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true

echo "✓ Instalado. Ejecuta 'rabbit-svn' o búscalo en el menú de aplicaciones."
