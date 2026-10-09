"""Búsqueda, descarga verificada e instalación de nuevas versiones desde GitHub Releases.

Seguridad:
- Solo se consulta la API de GitHub del repositorio oficial y solo se descargan ficheros
  de sus releases (se comprueba el host final tras las redirecciones).
- TLS siempre verificado (contexto por defecto de Python con los certificados del sistema).
- El .deb se comprueba contra SHA256SUMS.txt de la release y contra el digest que publica
  la API de GitHub (si existe); además se valida que el paquete sea rabbit-svn con la versión
  anunciada. Si algo no cuadra, no se instala.
- La instalación usa pkexec + apt-get (pide la contraseña con el diálogo gráfico del sistema).
- No se envía ningún dato del usuario: solo una petición GET con User-Agent "RabbitSVN/<versión>".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import __version__

REPO = "mgomezbuceta/rabbit-svn"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
ALLOWED_DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_DEB_BYTES = 200 * 1024 * 1024
TIMEOUT = 15
INSTALL_PREFIX = "/opt/rabbit-svn"


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    name: str
    notes: str
    page_url: str
    deb_url: str = ""
    deb_size: int = 0
    deb_digest: str = ""      # "sha256:…" según la API, si GitHub lo publica
    sums_url: str = ""


def parse_version(text: str) -> tuple:
    """'v0.1.10' → (0, 1, 10). Ignora sufijos (-rc1, +build)."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (text or "").strip())
    if not m:
        raise UpdateError(f"Versión no reconocida: {text!r}")
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(remote: str, local: str = __version__) -> bool:
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def installed_from_deb() -> bool:
    """True si esta copia es la del paquete .deb (y por tanto se puede actualizar sola)."""
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    if not here.startswith(INSTALL_PREFIX + os.sep):
        return False
    try:
        out = subprocess.run(["dpkg-query", "-W", "-f=${Status}", "rabbit-svn"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and "install ok installed" in out.stdout


def _check_url(url: str):
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "") not in ALLOWED_DOWNLOAD_HOSTS | {"api.github.com"}:
        raise UpdateError(f"URL de descarga no permitida: {url}")


def _open(url: str, accept: str):
    _check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": f"RabbitSVN/{__version__}", "Accept": accept})
    resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context())
    _check_url(resp.geturl())   # tras redirecciones seguimos en GitHub
    return resp


def release_from_json(data: dict) -> Release:
    tag = data.get("tag_name") or ""
    version = ".".join(str(p) for p in parse_version(tag))
    rel = Release(version=version, tag=tag, name=data.get("name") or tag, notes=data.get("body") or "",
                  page_url=data.get("html_url") or RELEASES_PAGE)
    expected = f"rabbit-svn_{version}_amd64.deb"
    for asset in data.get("assets") or []:
        name = asset.get("name")
        if name == expected:
            rel.deb_url = asset.get("browser_download_url") or ""
            rel.deb_size = int(asset.get("size") or 0)
            rel.deb_digest = asset.get("digest") or ""
        elif name == "SHA256SUMS.txt":
            rel.sums_url = asset.get("browser_download_url") or ""
    return rel


def fetch_latest() -> Release:
    try:
        with _open(API_LATEST, "application/vnd.github+json") as resp:
            data = json.loads(resp.read(2 * 1024 * 1024).decode("utf-8"))
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001  (red, JSON…)
        raise UpdateError(f"No se pudo consultar GitHub: {exc}") from None
    if data.get("draft") or data.get("prerelease"):
        raise UpdateError("La última release no es una versión publicada.")
    return release_from_json(data)


def expected_sha256(sums_text: str, filename: str) -> str:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise UpdateError(f"SHA256SUMS.txt no contiene {filename}.")


def download(rel: Release, dest_dir: str, progress: Optional[Callable[[int, int], None]] = None) -> str:
    """Descarga el .deb, lo verifica y devuelve su ruta. Lanza UpdateError si algo no cuadra."""
    if not rel.deb_url or not rel.sums_url:
        raise UpdateError("La release no incluye el paquete .deb o SHA256SUMS.txt.")
    filename = f"rabbit-svn_{rel.version}_amd64.deb"
    with _open(rel.sums_url, "application/octet-stream") as resp:
        want = expected_sha256(resp.read(64 * 1024).decode("utf-8", "replace"), filename)
    if rel.deb_digest and rel.deb_digest.lower() != f"sha256:{want}":
        raise UpdateError("El digest publicado por GitHub no coincide con SHA256SUMS.txt.")

    path = os.path.join(dest_dir, filename)
    h = hashlib.sha256()
    done = 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh, _open(rel.deb_url, "application/octet-stream") as resp:
            total = int(resp.headers.get("Content-Length") or rel.deb_size or 0)
            if total > MAX_DEB_BYTES:
                raise UpdateError("El paquete es demasiado grande.")
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_DEB_BYTES:
                    raise UpdateError("El paquete es demasiado grande.")
                h.update(chunk)
                fh.write(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    if h.hexdigest() != want:
        os.unlink(path)
        raise UpdateError("La suma SHA-256 del paquete descargado no coincide. No se instalará.")
    try:
        verify_deb(path, rel.version)
    except UpdateError:
        os.unlink(path)
        raise
    return path


def verify_deb(path: str, version: str):
    """Comprueba que el fichero es el paquete rabbit-svn de la versión esperada."""
    try:
        out = subprocess.run(["dpkg-deb", "-f", path, "Package", "Version"], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"No se pudo leer el paquete: {exc}") from None
    fields = dict(l.split(": ", 1) for l in out.stdout.splitlines() if ": " in l)
    if out.returncode != 0 or fields.get("Package") != "rabbit-svn" or fields.get("Version") != version:
        raise UpdateError("El paquete descargado no es rabbit-svn " + version + ".")


def install_command(deb_path: str) -> list:
    """Orden para instalar el .deb con apt (resuelve dependencias). pkexec pide la contraseña."""
    apt = shutil.which("apt-get") or "/usr/bin/apt-get"
    cmd = [apt, "install", "-y", "--no-remove", os.path.abspath(deb_path)]
    if os.geteuid() == 0:
        return cmd
    pkexec = shutil.which("pkexec")
    if not pkexec:
        raise UpdateError("No se encuentra pkexec para pedir permisos de administrador. Instala el paquete "
                          f"a mano: sudo apt install {deb_path}")
    return [pkexec] + cmd
