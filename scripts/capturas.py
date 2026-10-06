"""Genera las capturas del README con un repositorio de demostración.

Uso:  .venv/bin/python scripts/capturas.py [carpeta_salida]

No toca tu configuración: usa un XDG_CONFIG_HOME temporal y un repositorio
file:// creado en /tmp. Se ejecuta sin mostrar ventanas (plataforma offscreen).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "capturas"))
WORK = os.path.join(tempfile.gettempdir(), "rabbitsvn-demo")
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
os.environ["XDG_CONFIG_HOME"] = os.path.join(WORK, "config")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LC_CTYPE"] = "C.UTF-8"
sys.path.insert(0, ROOT)

REPO = os.path.join(WORK, "repo")
URL = "file://" + REPO
WC = os.path.join(WORK, "portal-clientes")


def svn(*args, user="marcos", cwd=None):
    subprocess.run(["svn", *args, "--username", user, "--non-interactive", "-q"], check=True, cwd=cwd,
                   stdout=subprocess.DEVNULL)


def write(rel, text, mode="w"):
    path = os.path.join(WC, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding="utf-8") as fh:
        fh.write(text)


def commit(msg, user):
    svn("add", "--force", "--parents", ".", cwd=WC)
    svn("commit", "-m", msg, user=user, cwd=WC)


def build_demo():
    subprocess.run(["svnadmin", "create", REPO], check=True)
    hook = os.path.join(REPO, "hooks", "pre-revprop-change")
    with open(hook, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(hook, 0o755)
    svn("mkdir", "--parents", "-m", "Estructura inicial del repositorio",
        URL + "/trunk", URL + "/branches", URL + "/tags")
    svn("checkout", URL + "/trunk", WC)
    write("composer.json", '{\n  "name": "acme/portal-clientes",\n  "require": {"php": ">=8.2"}\n}\n')
    write("src/Controller/ClientesController.php",
          "<?php\nnamespace App\\Controller;\n\nclass ClientesController extends AppController\n{\n"
          "    public function index()\n    {\n        $clientes = $this->Clientes->find('all');\n"
          "        $this->set(compact('clientes'));\n    }\n}\n")
    write("src/Model/Table/ClientesTable.php",
          "<?php\nnamespace App\\Model\\Table;\n\nuse Cake\\ORM\\Table;\n\nclass ClientesTable extends Table\n{\n"
          "    public function initialize(array $config): void\n    {\n        $this->setTable('clientes');\n"
          "    }\n}\n")
    write("config/app.php", "<?php\nreturn [\n    'debug' => false,\n    'App' => ['encoding' => 'UTF-8'],\n];\n")
    write("README.txt", "Portal de clientes\n==================\n")
    commit("Versión inicial del portal de clientes", "marcos")
    write("src/Controller/FacturasController.php",
          "<?php\nnamespace App\\Controller;\n\nclass FacturasController extends AppController\n{\n"
          "    public function index()\n    {\n    }\n}\n")
    commit("Añadir el controlador de facturas", "ana")
    write("src/Controller/ClientesController.php",
          "<?php\nnamespace App\\Controller;\n\nclass ClientesController extends AppController\n{\n"
          "    public function index()\n    {\n        $clientes = $this->paginate($this->Clientes);\n"
          "        $this->set(compact('clientes'));\n    }\n\n    public function view($id = null)\n    {\n"
          "        $cliente = $this->Clientes->get($id);\n        $this->set(compact('cliente'));\n    }\n}\n")
    commit("Paginación en el listado de clientes y vista de detalle\n\nRefs #482", "luis")
    write("config/app.php", "<?php\nreturn [\n    'debug' => false,\n    'App' => ['encoding' => 'UTF-8', "
                            "'defaultTimezone' => 'Europe/Madrid'],\n];\n")
    commit("Zona horaria por defecto: Europe/Madrid", "marta")
    svn("copy", "-m", "Rama para la nueva API REST", URL + "/trunk", URL + "/branches/api-rest", user="ana")
    write("README.txt", "Portal de clientes\n==================\n\nRequiere PHP 8.2 y MySQL 8.\n")
    commit("Documentar requisitos", "marcos")
    svn("update", cwd=WC)
    # Cambios locales para que se vean estados distintos
    write("src/Controller/ClientesController.php",
          "<?php\nnamespace App\\Controller;\n\nclass ClientesController extends AppController\n{\n"
          "    public function index()\n    {\n        $query = $this->Clientes->find()->where(['activo' => true]);\n"
          "        $clientes = $this->paginate($query);\n        $this->set(compact('clientes'));\n    }\n\n"
          "    public function view($id = null)\n    {\n        $cliente = $this->Clientes->get($id, "
          "contain: ['Facturas']);\n        $this->set(compact('cliente'));\n    }\n}\n")
    write("config/app.php", "    // TODO: revisar caché\n", mode="a")
    write("src/Service/ExportadorCsv.php", "<?php\nnamespace App\\Service;\n\nclass ExportadorCsv\n{\n}\n")
    svn("add", os.path.join(WC, "src/Service"), cwd=WC)
    write("notas-reunion.txt", "Pendiente: exportación a CSV\n")
    os.remove(os.path.join(WC, "README.txt"))


def main():
    os.makedirs(OUT, exist_ok=True)
    build_demo()

    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication(["rabbit-svn"])
    app.setStyle("Fusion")
    from rabbitsvn.config import Config, Project
    from rabbitsvn.ui import ops
    from rabbitsvn.ui.annotate_dialog import AnnotateDialog
    from rabbitsvn.ui.browser_dialog import RepoBrowser
    from rabbitsvn.ui.commit_dialog import CommitDialog
    from rabbitsvn.ui.diff import DiffDialog
    from rabbitsvn.ui.log_dialog import LogDialog
    from rabbitsvn.ui.main_window import MainWindow
    from rabbitsvn.ui.project_dialog import ProjectDialog
    from rabbitsvn.ui.settings_dialog import SettingsDialog

    # Icono PNG para el README a partir del SVG
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    icon = QImage(256, 256, QImage.Format_ARGB32)
    icon.fill(Qt.transparent)
    painter = QPainter(icon)
    QSvgRenderer(os.path.join(ROOT, "packaging", "rabbit-svn.svg")).render(painter)
    painter.end()
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    icon.save(os.path.join(ROOT, "assets", "icon.png"))

    cfg = Config()
    cfg.set("external", "diff_tool", "")   # visor interno en las capturas
    cfg.set("general", "auto_refresh_on_focus", False)
    for name in ("intranet", "api-facturacion"):
        os.makedirs(os.path.join(WORK, name), exist_ok=True)
        cfg.save_project(Project(name=name, wc_path=os.path.join(WORK, name)))
    demo = Project(name="portal-clientes", wc_path=WC, url=URL + "/trunk", repo_root=URL)
    cfg.save_project(demo)
    cfg.remember_url(URL + "/trunk")
    cfg.remember_url(URL + "/branches/api-rest")
    cfg.remember_message("Paginación en el listado de clientes y vista de detalle")

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            app.processEvents()
            QThreadPool.globalInstance().waitForDone(30)
            app.processEvents()

    def save(widget, name, size=None):
        if size:
            widget.resize(*size)
        widget.show()
        pump(2.5)
        widget.grab().save(os.path.join(OUT, name + ".png"))
        print("✓", name)

    win = MainWindow(cfg, WC)
    win.resize(1280, 760)
    win.select_project(demo.id)
    pump(2)
    for path, item in win.items.items():
        if os.path.isdir(path):
            item.setExpanded(True)
    win.console_dock.hide()
    save(win, "principal", (1280, 720))
    win.console_dock.show()

    ctx = win.ctx
    save(ProjectDialog(win, cfg, initial_path=WC), "anadir-proyecto", (720, 700))
    commit_dlg = CommitDialog(win, ctx, [WC], WC)
    commit_dlg.msg.edit.setPlainText("Filtrar clientes activos y cargar sus facturas en el detalle\n\nRefs #497")
    save(commit_dlg, "commit", (900, 640))
    log = LogDialog(win, ctx, WC, wc_path=WC, actions=win.actions)
    pump(2)
    if log.revs.topLevelItemCount() > 2:
        log.revs.setCurrentItem(log.revs.topLevelItem(2))
    save(log, "log", (1100, 700))
    save(DiffDialog(win, ctx, "Diferencias: ClientesController.php",
                    lambda c, ws: c.diff([os.path.join(WC, "src/Controller/ClientesController.php")],
                                         ignore_whitespace=ws)), "diff", (1000, 560))
    save(AnnotateDialog(win, ctx, os.path.join(WC, "src/Controller/ClientesController.php")),
         "annotate", (1100, 560))
    browser = RepoBrowser(win, ctx, URL, actions=win.actions)
    pump(2)
    root = browser.tree.topLevelItem(0)
    for i in range(root.childCount()):
        if root.child(i).text(0) in ("trunk", "branches"):
            root.child(i).setExpanded(True)
    save(browser, "navegador", (1000, 560))
    merge = ops.MergeDialog(win, ctx, WC)
    merge.src.setText(URL + "/branches/api-rest")
    save(merge, "merge", (760, 600))
    save(SettingsDialog(win, cfg), "ajustes", (680, 560))

    shutil.rmtree(WORK, ignore_errors=True)
    print("Capturas en", OUT)


if __name__ == "__main__":
    main()
