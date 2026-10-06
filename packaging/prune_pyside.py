"""Elimina de PySide6 todo lo que RabbitSVN no usa (Qt Quick, QML, Designer, herramientas…).

Uso: python3 prune_pyside.py <directorio_lib>

Conserva los módulos Python que importa la app, los plugins de plataforma
necesarios (X11, Wayland, temas, formatos de imagen…) y, de las librerías de
Qt, solo las que esos ficheros necesitan (cierre transitivo de DT_NEEDED).
"""
import os
import shutil
import struct
import sys

KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtSvg", "QtDBus"}
KEEP_PLUGIN_DIRS = {"platforms", "platformthemes", "platforminputcontexts", "imageformats", "iconengines",
                    "xcbglintegrations", "wayland-decoration-client", "wayland-graphics-integration-client",
                    "wayland-shell-integration", "styles"}
DROP_PLATFORMS = {"libqeglfs.so", "libqlinuxfb.so", "libqminimalegl.so", "libqvnc.so", "libqvkkhrdisplay.so"}


def needed(path):
    """Lee las entradas DT_NEEDED de un ELF de 64 bits little-endian."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"\x7fELF" or data[4] != 2:
        return []
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    out = []
    for sh in sections:
        if sh[1] != 6:  # SHT_DYNAMIC
            continue
        strtab = sections[sh[6]]
        str_off = strtab[4]
        for off in range(sh[4], sh[4] + sh[5], 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            if tag == 1:  # DT_NEEDED
                end = data.index(b"\0", str_off + val)
                out.append(data[str_off + val:end].decode())
    return out


def main(lib):
    pyside = os.path.join(lib, "PySide6")
    qt = os.path.join(pyside, "Qt")
    qtlib = os.path.join(qt, "lib")
    before = du(lib)

    # 1. Módulos Python y binarios de herramientas
    for name in os.listdir(pyside):
        path = os.path.join(pyside, name)
        if name.endswith(".abi3.so"):
            if name.split(".")[0] not in KEEP_MODULES:
                os.remove(path)
        elif name.startswith("libpyside6qml"):
            os.remove(path)  # solo lo usa el módulo QtQml
        elif name.endswith(".pyi") or name in ("include", "typesystems", "glue", "scripts", "doc", "examples"):
            rm(path)
        elif os.path.isfile(path) and "." not in name:
            os.remove(path)  # ejecutables de herramientas: designer, assistant, linguist, qmlls, uic…

    # 2. Directorios de Qt que no se usan
    for name in ("qml", "libexec", "metatypes", "modules", "resources", "mkspecs", "translations_unused"):
        rm(os.path.join(qt, name))
    trans = os.path.join(qt, "translations")
    if os.path.isdir(trans):
        for f in os.listdir(trans):
            if not (f.startswith("qtbase_") and ("_es" in f or "_en" in f or "_gl" in f or "_ca" in f)):
                rm(os.path.join(trans, f))

    # 3. Plugins
    plugins = os.path.join(qt, "plugins")
    for d in os.listdir(plugins):
        if d not in KEEP_PLUGIN_DIRS:
            rm(os.path.join(plugins, d))
    for f in DROP_PLATFORMS:
        rm(os.path.join(plugins, "platforms", f))
    gic = os.path.join(plugins, "wayland-graphics-integration-client")
    if os.path.isdir(gic):
        for f in os.listdir(gic):
            if f.endswith("-server.so"):   # integración de buffers del compositor, no del cliente
                rm(os.path.join(gic, f))

    # 4 y 5. Librerías: cierre transitivo desde módulos y plugins conservados. Se repite hasta
    # estabilizar porque al quitar un plugin roto (p. ej. el teclado virtual, que necesita Qt
    # Quick) pueden sobrar más librerías.
    while True:
        available = set(os.listdir(qtlib))
        for dirpath, _dirs, files in os.walk(plugins):
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith(".so") and any(d.startswith("libQt6") and d not in available for d in needed(p)):
                    os.remove(p)
        roots = [os.path.join(pyside, f) for f in os.listdir(pyside) if ".so" in f]
        roots += [os.path.join(lib, "shiboken6", f) for f in os.listdir(os.path.join(lib, "shiboken6"))
                  if ".so" in f]
        for dirpath, _dirs, files in os.walk(plugins):
            roots += [os.path.join(dirpath, f) for f in files if f.endswith(".so")]
        keep, queue = set(), list(roots)
        while queue:
            for dep in needed(queue.pop()):
                if dep in available and dep not in keep:
                    keep.add(dep)
                    queue.append(os.path.join(qtlib, dep))
        unused = available - keep
        if not unused:
            break
        for f in unused:
            rm(os.path.join(qtlib, f))
    remaining = set(os.listdir(qtlib))
    print(f"  PySide6: {before // 1048576} MB → {du(lib) // 1048576} MB ({len(remaining)} librerías Qt)")


def rm(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.remove(path)


def du(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


if __name__ == "__main__":
    main(sys.argv[1])
