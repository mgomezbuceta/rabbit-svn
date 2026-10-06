"""Pruebas de las medidas de seguridad y robustez."""
import json
import os
import stat

import pytest

from rabbitsvn.svn.client import (SvnClient, ValidationError, strip_url_credentials, validate_revision,
                                  validate_url)


def test_url_con_contrasena_rechazada():
    with pytest.raises(ValidationError):
        validate_url("https://pepe:secreto@svn.example.com/repo")
    assert validate_url("https://pepe@svn.example.com/repo")


@pytest.mark.parametrize("url", ["ftp://x/y", "javascript:alert(1)", "-r", "ext::sh -c id", "", "https:///sinhost"])
def test_url_esquema_no_permitido(url):
    with pytest.raises(ValidationError):
        validate_url(url)


def test_strip_credentials():
    assert strip_url_credentials("https://a:b@h:8443/r") == "https://h:8443/r"
    assert strip_url_credentials("svn://h/r") == "svn://h/r"


@pytest.mark.parametrize("rev", ["HEAD", "12", "{2024-01-31}", "{2024-01-31 10:00}", "5:HEAD"])
def test_revisiones_validas(rev):
    assert validate_revision(rev) == rev


@pytest.mark.parametrize("rev", ["--config-option=x", "1;rm", "{x}", "-5", "HEAD HEAD"])
def test_revisiones_invalidas(rev):
    with pytest.raises(ValidationError):
        validate_revision(rev)


def test_rutas_tras_separador():
    """Una ruta que empieza por '-' nunca se interpreta como opción."""
    c = SvnClient()
    for cmd in (c.cmd_update(["-evil"]), c.cmd_cleanup("--remove-unversioned"),
                c.cmd_export("--force", "/tmp/x"), c.cmd_commit(["--x"], "m")):
        sep = cmd.argv.index("--") if "--" in cmd.argv else None
        assert sep is not None
        assert all(not a.startswith("-") or i <= sep or a in ("--",) for i, a in enumerate(cmd.argv[:sep + 1]))
        cmd.cleanup()


def test_contrasena_no_en_argv():
    from rabbitsvn.svn.client import Credentials
    import secrets
    clave = secrets.token_urlsafe(16)  # valor aleatorio: el test no contiene ninguna credencial
    c = SvnClient(Credentials(username="u", password=clave))
    cmd = c.cmd_update(["/tmp/wc"])
    assert clave not in " ".join(cmd.argv)
    assert "--password-from-stdin" in cmd.argv and cmd.stdin == clave + "\n"


def test_mensaje_no_en_argv():
    c = SvnClient()
    cmd = c.cmd_copy("^/trunk", "svn://h/r/branches/b", "mensaje privado")
    assert "mensaje privado" not in cmd.argv
    tmp = cmd.argv[cmd.argv.index("-F") + 1]
    assert stat.S_IMODE(os.stat(tmp).st_mode) == 0o600
    cmd.cleanup()
    assert not os.path.exists(tmp)


@pytest.mark.parametrize("spec", [["1-"], ["a"], ["5-3"], ["0"], ["1;2"]])
def test_merge_rangos_invalidos(spec):
    with pytest.raises(ValidationError):
        SvnClient().cmd_merge("/wc", "svn://h/r/trunk", spec)


def test_config_corrupta_y_permisos(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import rabbitsvn.config as cfgmod
    importlib.reload(cfgmod)
    os.makedirs(cfgmod.CONFIG_DIR)
    with open(cfgmod.CONFIG_FILE, "w") as fh:
        fh.write("{roto")
    cfg = cfgmod.Config()
    assert cfg.load_warning
    assert any(n.startswith("config.json.corrupto-") for n in os.listdir(cfgmod.CONFIG_DIR))
    cfg.save_project(cfgmod.Project(name="p", wc_path="/tmp"))
    assert stat.S_IMODE(os.stat(cfgmod.CONFIG_FILE).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cfgmod.CONFIG_DIR).st_mode) == 0o700
    data = json.load(open(cfgmod.CONFIG_FILE))
    assert "password" not in json.dumps(data).lower().replace("remember_password", "")


def test_dos_instancias_no_pierden_proyectos(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import rabbitsvn.config as cfgmod
    importlib.reload(cfgmod)
    a, b = cfgmod.Config(), cfgmod.Config()
    a.save_project(cfgmod.Project(name="A", wc_path="/a"))
    b.save_project(cfgmod.Project(name="B", wc_path="/b"))
    a.save()  # p. ej. al cerrar la ventana A
    names = {p.name for p in cfgmod.Config().projects}
    assert names == {"A", "B"}


def test_proyecto_tolerante():
    from rabbitsvn.config import Project
    p = Project.from_dict({"name": 3, "wc_path": "/x", "trust_failures": ["other", "malo"], "id": "abc123"})
    assert p.name == "x" and p.trust_failures == ["other"]
    assert Project.from_dict("basura") is None
    assert Project.from_dict({"id": "../../etc"}) is None


def test_safe_filename():
    pytest.importorskip("PySide6")
    from rabbitsvn.ui.common import safe_filename
    assert safe_filename("..%2F..%2Fetc%2Fpasswd") == "passwd"
    assert safe_filename("..") == "fichero"
    assert safe_filename("a\\b") == "b"
