"""Ventana principal: cambiar de proyecto debe volver a listar los ficheros."""
import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("PySide6")
pytestmark = pytest.mark.skipif(not shutil.which("svnadmin"), reason="svnadmin no instalado")


def _wc(tmp_path, name, files):
    repo = tmp_path / f"repo-{name}"
    subprocess.run(["svnadmin", "create", str(repo)], check=True)
    wc = tmp_path / name
    subprocess.run(["svn", "checkout", "-q", f"file://{repo}", str(wc)], check=True)
    for f in files:
        (wc / f).write_text("x\n")
    subprocess.run(["svn", "add", "-q", *[str(wc / f) for f in files]], check=True)
    subprocess.run(["svn", "commit", "-q", "-m", "init", str(wc)], check=True)
    return str(wc)


def test_cambiar_de_proyecto_lista_ficheros(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("LC_CTYPE", "C.UTF-8")
    import importlib
    import rabbitsvn.config as cfgmod
    importlib.reload(cfgmod)
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import rabbitsvn.ui.main_window as mw
    importlib.reload(mw)

    a = _wc(tmp_path, "uno", ["a.txt"])
    b = _wc(tmp_path, "dos", ["b1.txt", "b2.txt"])
    cfg = cfgmod.Config()
    pa, pb = cfgmod.Project(name="uno", wc_path=a), cfgmod.Project(name="dos", wc_path=b)
    cfg.save_project(pa)
    cfg.save_project(pb)
    cfg.set("general", "auto_refresh_on_focus", False)
    cfg.set("general", "check_updates", False)
    win = mw.MainWindow(cfg)

    def wait_for(cond, secs=15):
        end = time.time() + secs
        while time.time() < end and not cond():
            app.processEvents()
            QThreadPool.globalInstance().waitForDone(20)
        return cond()

    for project, expected in ((pa, "a.txt"), (pb, "b1.txt"), (pa, "a.txt")):
        win.select_project(project.id)
        assert wait_for(lambda: os.path.join(project.wc_path, expected) in win.items), project.name
    win.close()
