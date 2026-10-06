#!/usr/bin/env bash
# Lanza RabbitSVN usando el entorno virtual creado por install.sh.
# Funciona desde cualquier directorio (menú de aplicaciones, terminal, enlace en ~/.local/bin).
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"

# Si no hay terminal (lanzado desde el menú), los errores de arranque van a un fichero
if [ ! -t 2 ]; then
    LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rabbit-svn"
    (umask 077; mkdir -p "$LOG_DIR"; touch "$LOG_DIR/arranque.log")
    exec 2>>"$LOG_DIR/arranque.log"
fi

if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "RabbitSVN: falta el entorno virtual. Ejecuta $DIR/install.sh" >&2
    command -v notify-send >/dev/null && notify-send "RabbitSVN" "Falta el entorno virtual: ejecuta install.sh"
    exit 1
fi
exec "$DIR/.venv/bin/python" -m rabbitsvn "$@"
