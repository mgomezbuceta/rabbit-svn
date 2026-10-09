"""Credenciales compartidas por servidor (0.3.0)."""
import importlib
import secrets

import pytest


@pytest.fixture()
def cfgmod(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import rabbitsvn.config as mod
    importlib.reload(mod)
    # Nunca tocar el llavero real del usuario desde los tests: contraseñas solo en memoria
    monkeypatch.setattr(mod, "SAFE_KEYRING_MODULES", ("__ninguno__",))
    return mod


@pytest.mark.parametrize("url,server", [
    ("https://Svn.Example.com/repo/trunk", "https://svn.example.com"),
    ("https://svn.example.com:443/r", "https://svn.example.com"),
    ("https://svn.example.com:8443/r", "https://svn.example.com:8443"),
    ("http://svn.example.com/r", "http://svn.example.com"),
    ("svn://10.196.4.65/sicedar", "svn://10.196.4.65"),
    ("svn+ssh://host/r", "svn+ssh://host"),
    ("file:///srv/repo", ""),
    ("", ""),
])
def test_server_of(cfgmod, url, server):
    assert cfgmod.server_of(url) == server


def test_solo_se_ofrecen_credenciales_del_mismo_servidor(cfgmod):
    cfg = cfgmod.Config()
    a = cfgmod.Account(server="https://svn.example.com", username="ana")
    b = cfgmod.Account(server="https://svn.example.com:8443", username="ana")
    c = cfgmod.Account(server="http://svn.example.com", username="ana")
    for acc in (a, b, c):
        cfg.save_account(acc)
    assert [x.id for x in cfg.accounts_for_server("https://svn.example.com")] == [a.id]
    assert cfg.accounts_for_server("") == []


def test_credencial_compartida_entre_proyectos(cfgmod):
    cfg = cfgmod.Config()
    clave = secrets.token_urlsafe(12)
    acc = cfgmod.Account(server="https://svn.example.com", username="ana")
    cfg.save_account(acc, clave)
    p1 = cfgmod.Project(name="uno", url="https://svn.example.com/uno", account_id=acc.id)
    p2 = cfgmod.Project(name="dos", url="https://svn.example.com/dos", account_id=acc.id)
    cfg.save_project(p1)
    cfg.save_project(p2)
    assert cfg.client(p1).creds.password == clave == cfg.client(p2).creds.password
    nueva = secrets.token_urlsafe(12)
    cfg.save_account(acc, nueva)   # cambiar la contraseña una vez…
    assert cfg.client(p1).creds.password == nueva == cfg.client(p2).creds.password   # …vale para todos
    assert {p.name for p in cfg.projects_using(acc.id)} == {"uno", "dos"}


def test_borrar_credencial_desvincula_proyectos(cfgmod):
    cfg = cfgmod.Config()
    acc = cfgmod.Account(server="https://svn.example.com", username="ana")
    cfg.save_account(acc, "x")
    cfg.save_project(cfgmod.Project(name="uno", url="https://svn.example.com/uno", account_id=acc.id))
    cfg.remove_account(acc.id)
    p = cfg.projects[0]
    assert p.account_id == "" and p.username == "" and cfg.accounts == []
    assert cfg.passwords.get(acc.key) == ""


def test_migracion_contrasenas_iguales(cfgmod):
    cfg = cfgmod.Config()
    clave = secrets.token_urlsafe(12)
    ps = [cfgmod.Project(name=n, url=f"https://svn.example.com/{n}", username="ana", remember_password=True)
          for n in ("uno", "dos")]
    otro = cfgmod.Project(name="tres", url="https://otro.example.com/r", username="ana")
    for p in ps + [otro]:
        cfg.save_project(p)
        cfg.passwords.set(p.id, clave, True)
    assert cfg.migrate_credentials() == []
    assert len(cfg.accounts) == 2                      # una por servidor
    for p in cfg.projects:
        assert p.account_id
        assert cfg.client(p).creds.password == clave
        assert cfg.passwords.get(p.id) == ""           # ya no quedan contraseñas por proyecto
    assert cfg.migrate_credentials() == []             # idempotente
    assert len(cfg.accounts) == 2


def test_migracion_con_conflicto(cfgmod):
    cfg = cfgmod.Config()
    for n in ("uno", "dos"):
        p = cfgmod.Project(name=n, url=f"https://svn.example.com/{n}", username="ana")
        cfg.save_project(p)
        cfg.passwords.set(p.id, secrets.token_urlsafe(12), True)
    conflicts = cfg.migrate_credentials()
    assert len(conflicts) == 1
    acc, names = conflicts[0]
    assert sorted(names) == ["dos", "uno"]
    assert cfg.passwords.get(acc.key) == ""            # no elige una al azar


def test_dialogo_ofrece_la_credencial_del_servidor(cfgmod, monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    import rabbitsvn.ui.project_dialog as pd
    importlib.reload(pd)
    cfg = cfgmod.Config()
    mio = cfgmod.Account(server="https://svn.example.com", username="ana")
    ajeno = cfgmod.Account(server="https://otro.example.com", username="pepe")
    cfg.save_account(mio, "x")
    cfg.save_account(ajeno, "y")
    preguntas = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(
        lambda *a, **k: preguntas.append(a[2]) or QMessageBox.Yes))
    dlg = pd.ProjectDialog(None, cfg)
    dlg.r_remote.setChecked(True)
    dlg.url.setText("https://svn.example.com/repo/trunk")
    dlg._set_server(cfgmod.server_of(dlg.url.text()), ask=True)
    assert len(preguntas) == 1 and "ana" in preguntas[0]
    assert dlg.cred.currentData() == mio.id
    ofrecidas = {dlg.cred.itemData(i) for i in range(dlg.cred.count())}
    assert ajeno.id not in ofrecidas                    # nunca la de otro servidor
    dlg._store(str(tmp_path / "wc"), dlg.url.text())
    assert cfg.projects[0].account_id == mio.id
    # cambiar a otro servidor deselecciona la credencial
    dlg.url.setText("https://tercero.example.com/r")
    assert dlg.cred.currentData() != mio.id
    app.processEvents()
