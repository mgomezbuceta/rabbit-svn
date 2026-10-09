"""Actualizaciones: comparación de versiones, selección de ficheros y verificación de la descarga."""
import hashlib
import io
import os
import shutil
import subprocess

import pytest

from rabbitsvn import updater
from rabbitsvn.updater import Release, UpdateError


@pytest.mark.parametrize("remote,local,newer", [
    ("v0.1.2", "0.1.1", True), ("0.2.0", "0.1.9", True), ("v0.1.10", "0.1.9", True),
    ("v0.1.1", "0.1.1", False), ("v0.1.0", "0.1.1", False), ("v1.0", "0.9.9", True), ("v0.2", "0.2.0", False),
])
def test_is_newer(remote, local, newer):
    assert updater.is_newer(remote, local) is newer


def test_version_invalida():
    with pytest.raises(UpdateError):
        updater.parse_version("latest")


def test_release_from_json():
    data = {"tag_name": "v0.3.0", "name": "RabbitSVN 0.3.0", "body": "notas", "html_url": "https://github.com/x",
            "assets": [{"name": "rabbit-svn_0.3.0_amd64.deb", "size": 10, "digest": "sha256:ab",
                        "browser_download_url": "https://github.com/a.deb"},
                       {"name": "SHA256SUMS.txt", "browser_download_url": "https://github.com/s.txt"},
                       {"name": "rabbit-svn_0.2.0_amd64.deb", "browser_download_url": "https://github.com/old.deb"}]}
    rel = updater.release_from_json(data)
    assert (rel.version, rel.deb_url, rel.sums_url, rel.deb_digest) == \
        ("0.3.0", "https://github.com/a.deb", "https://github.com/s.txt", "sha256:ab")


def test_expected_sha256():
    h = "a" * 64
    assert updater.expected_sha256(f"{h}  rabbit-svn_1_amd64.deb\n", "rabbit-svn_1_amd64.deb") == h
    with pytest.raises(UpdateError):
        updater.expected_sha256(f"{h}  otro.deb\n", "rabbit-svn_1_amd64.deb")


@pytest.mark.parametrize("url", ["http://github.com/a", "https://evil.example/a", "file:///etc/passwd",
                                 "https://github.com.evil.example/a"])
def test_urls_no_permitidas(url):
    with pytest.raises(UpdateError):
        updater._check_url(url)


class _Resp(io.BytesIO):
    def __init__(self, data, url="https://github.com/x"):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}
        self._url = url

    def geturl(self):
        return self._url


def _fake_open(files):
    def _open(url, accept):
        updater._check_url(url)
        return _Resp(files[url])
    return _open


def _build_deb(tmp_path, version):
    root = tmp_path / "pkg"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN" / "control").write_text(
        f"Package: rabbit-svn\nVersion: {version}\nArchitecture: amd64\nMaintainer: t <t@t>\nDescription: t\n")
    deb = tmp_path / "in.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)], check=True,
                   stdout=subprocess.DEVNULL)
    return deb.read_bytes()


needs_dpkg = pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="dpkg-deb no disponible")


@needs_dpkg
def test_descarga_verificada(tmp_path, monkeypatch):
    data = _build_deb(tmp_path, "9.9.9")
    sha = hashlib.sha256(data).hexdigest()
    rel = Release("9.9.9", "v9.9.9", "", "", "", deb_url="https://github.com/d.deb",
                  sums_url="https://github.com/s.txt", deb_digest=f"sha256:{sha}")
    monkeypatch.setattr(updater, "_open", _fake_open({
        rel.deb_url: data, rel.sums_url: f"{sha}  rabbit-svn_9.9.9_amd64.deb\n".encode()}))
    out = tmp_path / "out"
    out.mkdir()
    path = updater.download(rel, str(out))
    assert open(path, "rb").read() == data


@needs_dpkg
@pytest.mark.parametrize("fallo", ["suma", "digest", "version"])
def test_descarga_rechazada(tmp_path, monkeypatch, fallo):
    data = _build_deb(tmp_path, "9.9.8" if fallo == "version" else "9.9.9")
    sha = hashlib.sha256(data).hexdigest()
    sums_sha = "0" * 64 if fallo == "suma" else sha
    digest = "sha256:" + ("1" * 64 if fallo == "digest" else sha)
    rel = Release("9.9.9", "v9.9.9", "", "", "", deb_url="https://github.com/d.deb",
                  sums_url="https://github.com/s.txt", deb_digest=digest)
    monkeypatch.setattr(updater, "_open", _fake_open({
        rel.deb_url: data, rel.sums_url: f"{sums_sha}  rabbit-svn_9.9.9_amd64.deb\n".encode()}))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UpdateError):
        updater.download(rel, str(out))
    assert os.listdir(out) == []          # no queda nada a medio descargar


def test_no_es_instalacion_deb():
    assert updater.installed_from_deb() is False   # los tests corren desde el código fuente
