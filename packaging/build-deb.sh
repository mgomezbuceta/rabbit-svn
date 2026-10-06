#!/usr/bin/env bash
# Genera el paquete .deb de RabbitSVN en instaladores/.
#
#   packaging/build-deb.sh
#
# El paquete instala la app en /opt/rabbit-svn con su propia copia de PySide6
# (Ubuntu/Debian no empaquetan PySide6). keyring, Subversion y las librerías
# del sistema gráfico se toman de los paquetes de la distribución.
# No necesita root: usa fakeroot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'import re,sys; print(re.search(r"__version__ = \"([^\"]+)\"", open("rabbitsvn/__init__.py").read()).group(1))')"
ARCH="amd64"
PKG="rabbit-svn_${VERSION}_${ARCH}"
BUILD="$ROOT/build/deb"
STAGE="$BUILD/$PKG"
OUT="$ROOT/instaladores"
PYSIDE_VERSION="$(grep -E '^PySide6==' requirements.txt | cut -d= -f3)"

for tool in dpkg-deb fakeroot python3; do
    command -v "$tool" >/dev/null || { echo "Falta $tool (sudo apt install dpkg-dev fakeroot python3-pip)"; exit 1; }
done

echo "» RabbitSVN $VERSION ($ARCH), PySide6 $PYSIDE_VERSION"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/rabbit-svn" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" "$STAGE/usr/share/doc/rabbit-svn" "$OUT"

# ---------------------------------------------------------------- aplicación
cp -r rabbitsvn "$STAGE/opt/rabbit-svn/"
find "$STAGE/opt/rabbit-svn" -name __pycache__ -prune -exec rm -rf {} +
cat > "$STAGE/opt/rabbit-svn/launcher.py" <<'EOF'
"""Lanzador del paquete: usa solo el código y las librerías de /opt/rabbit-svn
y del sistema (python3 -s -E ignora PYTHONPATH y los paquetes del usuario)."""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [BASE, os.path.join(BASE, "lib")]

from rabbitsvn.__main__ import main  # noqa: E402

main()
EOF

# ---------------------------------------------------------------- PySide6
echo "» Descargando PySide6-Essentials $PYSIDE_VERSION"
WHEELS="$BUILD/wheels"
mkdir -p "$WHEELS"
python3 -m pip download --quiet --no-deps --only-binary=:all: --dest "$WHEELS" \
    --platform manylinux_2_34_x86_64 --python-version 3.10 --implementation cp \
    "PySide6-Essentials==$PYSIDE_VERSION" "shiboken6==$PYSIDE_VERSION"
LIB="$STAGE/opt/rabbit-svn/lib"
mkdir -p "$LIB"
for whl in "$WHEELS"/*.whl; do
    python3 -m zipfile -e "$whl" "$LIB"
done
rm -rf "$LIB"/*.dist-info
find "$LIB" -name __pycache__ -type d -prune -exec rm -rf {} +

echo "» Reduciendo PySide6 a lo que usa la app"
python3 "$ROOT/packaging/prune_pyside.py" "$LIB"

# ---------------------------------------------------------------- integración con el escritorio
cat > "$STAGE/usr/bin/rabbit-svn" <<'EOF'
#!/bin/sh
# RabbitSVN: cliente gráfico de Subversion
if [ ! -t 2 ]; then
    LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/rabbit-svn"
    (umask 077; mkdir -p "$LOG_DIR" && touch "$LOG_DIR/arranque.log") 2>/dev/null && \
        exec 2>>"$LOG_DIR/arranque.log"
fi
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -s -E /opt/rabbit-svn/launcher.py "$@"
EOF

cat > "$STAGE/usr/share/applications/rabbit-svn.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=RabbitSVN
GenericName=Cliente de Subversion
Comment=Cliente gráfico de Subversion inspirado en RabbitVCS
Exec=rabbit-svn %f
Icon=rabbit-svn
Terminal=false
Categories=Development;RevisionControl;
Keywords=svn;subversion;vcs;commit;
StartupWMClass=rabbit-svn
EOF

cp packaging/rabbit-svn.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/rabbit-svn.svg"
cp assets/icon.png "$STAGE/usr/share/icons/hicolor/256x256/apps/rabbit-svn.png"

# ---------------------------------------------------------------- documentación
cat > "$STAGE/usr/share/doc/rabbit-svn/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: rabbit-svn
Source: https://github.com/mgomezbuceta/rabbit-svn

Files: *
Copyright: $(date +%Y) Marcos Gómez Buceta
License: Apache-2.0
 On Debian systems, the full text of the Apache License 2.0 can be found in
 /usr/share/common-licenses/Apache-2.0.

Files: opt/rabbit-svn/lib/*
Copyright: The Qt Company Ltd. and other contributors
License: LGPL-3.0
 PySide6 and Qt are distributed under the GNU Lesser General Public License v3.
 On Debian systems, see /usr/share/common-licenses/LGPL-3.
EOF
printf 'rabbit-svn (%s) stable; urgency=medium\n\n  * Versión %s. Ver https://github.com/mgomezbuceta/rabbit-svn/releases\n\n -- Marcos Gómez Buceta <mgomezbuceta@gmail.com>  %s\n' \
    "$VERSION" "$VERSION" "$(date -R)" | gzip -9n > "$STAGE/usr/share/doc/rabbit-svn/changelog.gz"

# ---------------------------------------------------------------- control y scripts
INSTALLED_KB="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: rabbit-svn
Version: $VERSION
Section: vcs
Priority: optional
Architecture: $ARCH
Maintainer: Marcos Gómez Buceta <mgomezbuceta@gmail.com>
Installed-Size: $INSTALLED_KB
Depends: python3 (>= 3.10), python3-keyring, python3-secretstorage, subversion (>= 1.10), libc6 (>= 2.34), libegl1, libgl1, libfontconfig1, libfreetype6, libdbus-1-3, libglib2.0-0t64 | libglib2.0-0, libxkbcommon0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libxcb-shape0, libxcb-xkb1, libxcb-render-util0, libxcb-image0, libx11-xcb1, libwayland-client0, libwayland-cursor0
Recommends: meld, gnome-keyring | kwalletmanager
Homepage: https://github.com/mgomezbuceta/rabbit-svn
Description: cliente gráfico de Subversion inspirado en RabbitVCS
 RabbitSVN reúne las funciones SVN de RabbitVCS en una aplicación de escritorio
 independiente: proyectos con credenciales en el llavero del sistema, estado de
 la working copy, commit, log, diff, annotate, ramas, switch, merge, navegador
 del repositorio, propiedades, bloqueos, conflictos, parches, import, export,
 relocate y cleanup. Usa el cliente svn oficial.
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    # Precompila el código para el Python del sistema (arranque más rápido)
    python3 -m compileall -q /opt/rabbit-svn/rabbitsvn /opt/rabbit-svn/lib >/dev/null 2>&1 || true
fi
exit 0
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /opt/rabbit-svn -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
EOF

# ---------------------------------------------------------------- permisos y empaquetado
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/usr/bin/rabbit-svn" "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

DEB="$OUT/$PKG.deb"
fakeroot dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
( cd "$OUT" && sha256sum "$(basename "$DEB")" > SHA256SUMS.txt )
echo "✓ $DEB ($(du -h "$DEB" | cut -f1))"
cat "$OUT/SHA256SUMS.txt"
