"""Ventana principal: proyectos, explorador de la working copy y consola."""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QGuiApplication, QIcon, QKeySequence, QTextCharFormat
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDockWidget, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QStyle,
                               QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import __version__
from ..config import Config, Project
from .actions import Actions
from .common import (STATUS_LABELS, color_for, confirm, esc, fmt_date, mono_font, open_path, status_icon,
                     status_key)
from .context import CommandLog, Context, run_svn

COLS = ["Nombre", "Estado", "Propiedades", "Revisión", "Últ. cambio", "Autor", "Fecha", "Bloqueo", "Changelist"]


class SortItem(QTreeWidgetItem):
    """Carpetas primero; columnas numéricas ordenadas como números."""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 0
        if col == 0:
            a = (self.data(0, Qt.UserRole + 1) or 0, self.text(0).lower())
            b = (other.data(0, Qt.UserRole + 1) or 0, other.text(0).lower())
            return a < b
        if col in (3, 4):
            def num(t):
                return int(t) if t.isdigit() else -1
            return num(self.text(col)) < num(other.text(col))
        return self.text(col).lower() < other.text(col).lower()


class MainWindow(QMainWindow):
    def __init__(self, config: Config, open_path_arg: str = ""):
        super().__init__()
        self.config = config
        self.ctx = Context(config)
        self.actions = Actions(self)
        self.entries: dict = {}
        self.items: dict = {}
        self.wc_info = None
        self.last_refresh = 0.0
        self.loading = False
        self.setWindowTitle("RabbitSVN")
        icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "packaging", "rabbit-svn.svg")
        self.setWindowIcon(QIcon(icon_path) if os.path.exists(icon_path)
                           else QApplication.style().standardIcon(QStyle.SP_DriveNetIcon))
        self.resize(1280, 820)
        self.setAcceptDrops(True)

        self._build_projects_dock()
        self._build_central()
        self._build_console()
        self._build_actions()
        self._restore_state()
        CommandLog.get().message.connect(self._console_write)

        self.reload_projects()
        target = None
        if open_path_arg:
            target = self.config.project_for_path(open_path_arg)
            if target is None and os.path.isdir(open_path_arg):
                QTimer.singleShot(200, lambda: self.add_project(local_path=open_path_arg))
        if target is None:
            target = self.config.project(self.config.get("ui", "last_project", ""))
        if target is None and self.config.projects:
            target = self.config.projects[0]
        if target:
            self.select_project(target.id)
        else:
            self._show_welcome()
        if config.load_warning:
            QTimer.singleShot(400, lambda: QMessageBox.warning(self, "Configuración", config.load_warning))
        client = config.client()
        self.statusBar().addPermanentWidget(QLabel(
            f"svn {client.version() if client.available() else 'NO ENCONTRADO'}  "))
        if not client.available():
            QTimer.singleShot(300, lambda: QMessageBox.critical(
                self, "Subversion", "No se encuentra el binario 'svn'.\nInstálalo (sudo apt install subversion) "
                                    "o configura su ruta en Ajustes."))

    # ================================================================== construcción
    def _build_projects_dock(self):
        dock = QDockWidget("Proyectos", self)
        dock.setObjectName("projects")
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        self.projects = QListWidget()
        self.projects.currentItemChanged.connect(self._project_changed)
        self.projects.setContextMenuPolicy(Qt.CustomContextMenu)
        self.projects.customContextMenuRequested.connect(self._project_menu)
        self.projects.itemDoubleClicked.connect(lambda *_: self.edit_project())
        lay.addWidget(self.projects)
        row = QHBoxLayout()
        for text, tip, fn in (("+", "Añadir proyecto", self.add_project), ("✎", "Editar proyecto", self.edit_project),
                              ("−", "Quitar proyecto", self.remove_project)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(36)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        dock.setWidget(w)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.projects_dock = dock

    def _build_central(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 0)
        self.header = QLabel("")
        self.header.setTextFormat(Qt.RichText)
        self.header.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.header.setWordWrap(True)
        self.header.setStyleSheet("QLabel{padding:6px;border-radius:4px;background:palette(alternate-base);}")
        lay.addWidget(self.header)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filtrar por nombre…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        row.addWidget(self.search, 1)
        self.only_changes = QCheckBox("Solo cambios")
        self.only_changes.setChecked(self.config.get("ui", "only_changes", False))
        self.only_changes.toggled.connect(self._toggle_only_changes)
        row.addWidget(self.only_changes)
        self.show_unversioned = QCheckBox("Sin versionar")
        self.show_unversioned.setChecked(self.config.get("general", "show_unversioned_files", True))
        self.show_unversioned.toggled.connect(lambda _: self.populate())
        row.addWidget(self.show_unversioned)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 380)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.itemDoubleClicked.connect(self._double_click)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        lay.addWidget(self.tree)
        self.setCentralWidget(w)

    def _build_console(self):
        dock = QDockWidget("Consola", self)
        dock.setObjectName("console")
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(mono_font())
        self.console.setMaximumBlockCount(5000)
        dock.setWidget(self.console)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.console_dock = dock

    def _act(self, text, fn, shortcut=None, icon=None, tip=None) -> QAction:
        a = QAction(text, self)
        a.triggered.connect(lambda _=False: fn())
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if icon is not None:
            a.setIcon(QApplication.style().standardIcon(icon) if isinstance(icon, QStyle.StandardPixmap)
                      else QIcon.fromTheme(icon))
        if tip:
            a.setToolTip(tip)
        return a

    def _build_actions(self):
        S = QStyle
        A = self.actions
        sel = self.selected_paths
        self.a_update = self._act("Actualizar", lambda: A.update(sel()), "Ctrl+U", S.SP_ArrowDown,
                                  "Actualizar desde el repositorio (svn update)")
        self.a_commit = self._act("Commit…", lambda: A.commit(sel()), "Ctrl+K", S.SP_ArrowUp,
                                  "Enviar cambios al repositorio")
        self.a_checkmods = self._act("Comprobar modificaciones…", lambda: A.checkmods(sel()), "Ctrl+M",
                                     S.SP_FileDialogContentsView)
        self.a_diff = self._act("Diferencias", lambda: A.diff(sel()), "Ctrl+D", S.SP_FileDialogDetailedView)
        self.a_log = self._act("Log", lambda: A.log(sel()[0]), "Ctrl+L", S.SP_FileDialogInfoView,
                               "Historial de revisiones")
        self.a_revert = self._act("Revertir…", lambda: A.revert(sel()), None, S.SP_BrowserReload)
        self.a_browser = self._act("Navegador del repositorio", lambda: A.browser(), "Ctrl+B", S.SP_DriveNetIcon)
        self.a_refresh = self._act("Refrescar", self.refresh, "F5", S.SP_BrowserReload)
        self.a_settings = self._act("Ajustes…", self.open_settings, "Ctrl+,", S.SP_FileDialogDetailedView)

        tb = QToolBar("Principal")
        tb.setObjectName("main_toolbar")
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        for a in (self.a_update, self.a_commit, self.a_checkmods, None, self.a_diff, self.a_log, self.a_revert,
                  None, self.a_browser, self.a_refresh, None, self.a_settings):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

        mb = self.menuBar()
        m = mb.addMenu("&Proyecto")
        m.addAction(self._act("Añadir proyecto…", self.add_project, "Ctrl+N"))
        m.addAction(self._act("Editar proyecto…", self.edit_project))
        m.addAction(self._act("Quitar proyecto", self.remove_project))
        m.addSeparator()
        m.addAction(self._act("Abrir carpeta del proyecto", lambda: self.ctx.project and open_path(
            self.ctx.project.wc_path, self.config)))
        m.addAction(self._act("Crear repositorio local…", A.create_repo))
        m.addAction(self._act("Importar carpeta en un repositorio…", lambda: A.import_()))
        m.addSeparator()
        m.addAction(self._act("Salir", self.close, "Ctrl+Q"))

        m = mb.addMenu("&SVN")
        for a in (self.a_update, self.a_commit, self.a_checkmods):
            m.addAction(a)
        m.addAction(self._act("Actualizar a revisión…", lambda: A.update_to(sel())))
        m.addSeparator()
        m.addAction(self._act("Añadir…", lambda: A.add(sel())))
        m.addAction(self._act("Eliminar…", lambda: A.delete(self.selected_paths(False))))
        m.addAction(self.a_revert)
        m.addAction(self._act("Renombrar…", lambda: A.rename(sel()[0]), "F2"))
        m.addSeparator()
        m.addAction(self.a_diff)
        m.addAction(self.a_log)
        m.addAction(self._act("Annotate", lambda: A.annotate(sel()[0])))
        m.addAction(self._act("Propiedades…", lambda: A.properties(sel()[0])))
        m.addSeparator()
        m.addAction(self._act("Bloquear…", lambda: A.lock(sel())))
        m.addAction(self._act("Desbloquear…", lambda: A.unlock(sel())))
        m.addAction(self._act("Marcar como resuelto…", lambda: A.resolve(sel())))
        m.addAction(self._act("Limpiar (cleanup)…", lambda: A.cleanup(self._root())))
        m.addSeparator()
        m.addAction(self._act("Crear parche…", lambda: A.create_patch(sel())))
        m.addAction(self._act("Aplicar parche…", lambda: A.apply_patch(self._root())))
        m.addAction(self._act("Exportar…", lambda: A.export(sel()[0])))

        m = mb.addMenu("&Ramas")
        m.addAction(self._act("Crear rama / etiqueta…", lambda: A.branch(self._root())))
        m.addAction(self._act("Cambiar (switch)…", lambda: A.switch(self._root())))
        m.addAction(self._act("Fusionar (merge)…", lambda: A.merge(self._root())))
        m.addAction(self._act("Comparar ramas / revisiones…", lambda: A.changes()))
        m.addAction(self._act("Reubicar (relocate)…", lambda: A.relocate(self._root())))

        m = mb.addMenu("&Herramientas")
        m.addAction(self.a_browser)
        m.addAction(self.a_refresh)
        m.addSeparator()
        m.addAction(self.projects_dock.toggleViewAction())
        m.addAction(self.console_dock.toggleViewAction())
        m.addAction(self._act("Limpiar consola", self.console.clear))
        m.addSeparator()
        m.addAction(self.a_settings)

        m = mb.addMenu("A&yuda")
        m.addAction(self._act("Acerca de", self.about))

    # ================================================================== estado UI
    def _restore_state(self):
        g = self.config.get("ui", "geometry", "")
        s = self.config.get("ui", "state", "")
        if g:
            self.restoreGeometry(QByteArray.fromBase64(g.encode()))
        if s:
            self.restoreState(QByteArray.fromBase64(s.encode()))

    def closeEvent(self, ev):
        self.config.set("ui", "geometry", bytes(self.saveGeometry().toBase64()).decode())
        self.config.set("ui", "state", bytes(self.saveState().toBase64()).decode())
        self.config.set("ui", "only_changes", self.only_changes.isChecked())
        try:
            self.config.save()
        except OSError as exc:
            QMessageBox.warning(self, "Configuración", f"No se pudo guardar la configuración:\n{exc}")
        self.config.passwords.forget_session()
        super().closeEvent(ev)

    def changeEvent(self, ev):
        if (ev.type() == QEvent.ActivationChange and self.isActiveWindow()
                and self.config.get("general", "auto_refresh_on_focus", True)
                and self.ctx.project and time.time() - self.last_refresh > 5 and not self.loading
                and QApplication.activeModalWidget() is None):
            QTimer.singleShot(100, self.refresh)
        super().changeEvent(ev)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.isdir(p):
                self.add_project(local_path=p)
                break

    def _console_write(self, text: str, level: str):
        colors = {"cmd": "#1565c0", "err": "#c62828", "info": "#757575"}
        fmt = QTextCharFormat()
        if level in colors:
            fmt.setForeground(QColor(colors[level]))
        if level == "cmd":
            text = "$ " + text
            fmt.setFontWeight(700)
        cur = self.console.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        cur.insertText(text + "\n", fmt)
        self.console.setTextCursor(cur)
        self.console.ensureCursorVisible()

    def _show_welcome(self):
        self.header.setText(
            "<h3>Bienvenido a RabbitSVN</h3>"
            "Añade un proyecto con <b>Proyecto → Añadir proyecto</b> (Ctrl+N): puedes indicar una carpeta "
            "local que ya sea una working copy o hacer checkout desde una URL.<br>"
            "También puedes arrastrar una carpeta a esta ventana.")
        self.tree.clear()
        self.items = {}

    def about(self):
        QMessageBox.about(self, "Acerca de RabbitSVN",
                          f"<h3>RabbitSVN {__version__}</h3>"
                          "Cliente gráfico de Subversion inspirado en RabbitVCS (solo SVN).<br>"
                          "Python + PySide6, usando el cliente de línea de órdenes <tt>svn</tt>.")

    # ================================================================== proyectos
    def reload_projects(self, select_id: Optional[str] = None):
        cur = select_id or (self.ctx.project.id if self.ctx.project else None)
        self.projects.blockSignals(True)
        self.projects.clear()
        style = QApplication.style()
        for p in sorted(self.config.projects, key=lambda p: p.name.lower()):
            it = QListWidgetItem(style.standardIcon(QStyle.SP_DirIcon), p.name)
            it.setData(Qt.UserRole, p.id)
            it.setToolTip(f"{p.wc_path}\n{p.url}")
            if not os.path.isdir(p.wc_path):
                it.setForeground(QColor("#c62828"))
                it.setToolTip("La carpeta no existe: " + p.wc_path)
            self.projects.addItem(it)
            if p.id == cur:
                self.projects.setCurrentItem(it)
        self.projects.blockSignals(False)

    def select_project(self, project_id: str):
        for i in range(self.projects.count()):
            it = self.projects.item(i)
            if it.data(Qt.UserRole) == project_id:
                self.projects.setCurrentItem(it)
                return

    def _project_changed(self, cur, _prev):
        if cur is None:
            self.ctx = Context(self.config)
            self._show_welcome()
            return
        p = self.config.project(cur.data(Qt.UserRole))
        self.ctx = Context(self.config, p)
        self.config.set("ui", "last_project", p.id)
        self.config.save()
        self.setWindowTitle(f"{p.name} — RabbitSVN")
        self.tree.clear()
        self.items = {}
        self.entries.clear()
        self.reload_project_info()
        self.refresh()

    def add_project(self, local_path: str = "", checkout_url: str = ""):
        from .project_dialog import ProjectDialog
        dlg = ProjectDialog(self, self.config, initial_path=local_path)
        if checkout_url:
            dlg.r_remote.setChecked(True)
            dlg.url.setText(checkout_url)
            dlg._suggest_dest(checkout_url)
        if dlg.exec():
            self.reload_projects(dlg.project.id)
            self.select_project(dlg.project.id)
            self._project_changed(self.projects.currentItem(), None)

    def edit_project(self):
        if not self.ctx.project:
            return
        from .project_dialog import ProjectDialog
        dlg = ProjectDialog(self, self.config, self.config.project(self.ctx.project.id))
        if dlg.exec():
            self.reload_projects(dlg.project.id)
            self._project_changed(self.projects.currentItem(), None)

    def remove_project(self):
        p = self.ctx.project
        if p and confirm(self, f"¿Quitar «{p.name}» de la lista?\n(No se borra ningún fichero del disco.)"):
            self.config.remove_project(p.id)
            self.ctx = Context(self.config)
            self.reload_projects()
            if self.projects.count():
                self.projects.setCurrentRow(0)
            else:
                self._show_welcome()

    def _project_menu(self, pos):
        it = self.projects.itemAt(pos)
        m = QMenu(self)
        m.addAction("Añadir proyecto…", self.add_project)
        if it:
            m.addAction("Editar…", self.edit_project)
            m.addAction("Abrir carpeta", lambda: open_path(self.ctx.project.wc_path, self.config))
            m.addAction("Copiar URL", lambda: QGuiApplication.clipboard().setText(self.ctx.project.url))
            m.addSeparator()
            m.addAction("Olvidar contraseña guardada", self.forget_password)
            m.addAction("Quitar de la lista", self.remove_project)
        m.exec(self.projects.viewport().mapToGlobal(pos))

    def forget_password(self):
        p = self.ctx.project
        if p and confirm(self, f"¿Borrar la contraseña de «{p.name}» del llavero y de la sesión?"):
            self.config.passwords.delete(p.id)
            p.remember_password = False
            self.config.save_project(p)
            self.statusBar().showMessage("Contraseña olvidada", 4000)

    def open_settings(self):
        from .settings_dialog import SettingsDialog
        if SettingsDialog(self, self.config).exec():
            self.show_unversioned.setChecked(self.config.get("general", "show_unversioned_files", True))
            self.refresh()

    # ================================================================== working copy
    def _root(self) -> str:
        return self.ctx.project.wc_path if self.ctx.project else ""

    def reload_project_info(self):
        p = self.ctx.project
        if not p:
            return
        if not os.path.isdir(p.wc_path):
            self.header.setText(f"<b style='color:#c62828'>La carpeta {esc(p.wc_path)} no existe.</b> "
                                "Edita el proyecto o vuelve a hacer checkout.")
            return

        def done(infos):
            i = infos[0]
            self.wc_info = i
            changed = False
            if i.url != p.url or i.repo_root != p.repo_root:
                p.url, p.repo_root = i.url, i.repo_root
                changed = True
            if changed:
                self.config.save_project(p)
            branch = i.relative_url or i.url
            self.header.setText(
                f"<b style='font-size:14px'>{esc(p.name)}</b> &nbsp; <span style='color:#1565c0'>{esc(branch)}</span>"
                f" &nbsp;·&nbsp; revisión <b>{esc(i.revision)}</b>"
                f" &nbsp;·&nbsp; último cambio r{esc(i.last_changed_rev)} por {esc(i.last_changed_author)} "
                f"({esc(fmt_date(i.last_changed_date, self.config))})<br>"
                f"<span style='color:gray'>{esc(i.wc_root or p.wc_path)} &nbsp;←&nbsp; {esc(i.url)}</span>")

        def err(exc):
            self.header.setText(f"<b>{esc(p.name)}</b><br><span style='color:#c62828'>"
                                f"{esc(getattr(exc, 'stderr', str(exc)))}</span>")
        run_svn(self, self.ctx, lambda c: c.info(p.wc_path), done, err)

    def refresh(self):
        p = self.ctx.project
        if not p or not os.path.isdir(p.wc_path) or self.loading:
            return
        self.loading = True
        self.last_refresh = time.time()
        self.statusBar().showMessage("Leyendo estado…")
        depth = "infinity" if self.config.get("general", "enable_recursive", True) else "immediates"
        no_ignore = self.config.get("general", "show_ignored_files", False)
        pid = p.id

        def done(entries):
            self.loading = False
            if not self.ctx.project or self.ctx.project.id != pid:
                return
            self.entries = {os.path.normpath(e.path): e for e in entries}
            self.populate()
            self.reload_project_info()

        def err(exc):
            self.loading = False
            self.statusBar().showMessage("Error al leer el estado", 5000)
            self.header.setText(f"<b>{esc(p.name)}</b><br><span style='color:#c62828'>"
                                f"{esc(getattr(exc, 'stderr', str(exc)))}</span>")
        run_svn(self, self.ctx, lambda c: c.status([p.wc_path], depth=depth, verbose=True, no_ignore=no_ignore),
                done, err)

    def _toggle_only_changes(self, _on):
        self.config.set("ui", "only_changes", self.only_changes.isChecked())
        self.populate()

    def populate(self):
        root = os.path.normpath(self._root()) if self.ctx.project else ""
        if not root:
            return
        expanded = set()
        for path, it in self.items.items():
            try:
                if it.isExpanded():
                    expanded.add(path)
            except RuntimeError:  # el elemento ya no existe (árbol vaciado)
                pass
        selected = set(self.selected_paths(default_root=False))
        first_load = not self.items
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self.items = {}
        only = self.only_changes.isChecked()
        show_unv = self.show_unversioned.isChecked()
        colorize = self.config.get("general", "enable_colorize", True)
        counts: dict = {}

        dirty_dirs = set()
        for path, e in self.entries.items():
            k = status_key(e)
            counts[k] = counts.get(k, 0) + 1
            if e.is_changed and e.item not in ("ignored", "external") and (show_unv or e.item != "unversioned"):
                d = os.path.dirname(path)
                while d.startswith(root) and d not in dirty_dirs:
                    dirty_dirs.add(d)
                    if d == root:
                        break
                    d = os.path.dirname(d)

        def visible(e):
            if not show_unv and e.item == "unversioned":
                return False
            if only:
                return e.is_changed and e.item not in ("ignored", "external")
            return True

        def make(path, e):
            name = os.path.relpath(path, root) if only else os.path.basename(path)
            if path == root:
                name = os.path.basename(root) + "  (raíz)"
            k = status_key(e)
            is_dir = os.path.isdir(path)
            icon_key = k
            if is_dir and k == "normal" and path in dirty_dirs:
                icon_key = "modified"
            lock = e.lock.owner if e and e.lock else ""
            it = SortItem([
                name, STATUS_LABELS.get(k, k) if k != "normal" else ("Contiene cambios" if icon_key != k else ""),
                STATUS_LABELS.get(e.props, "") if e.props not in ("none", "normal") else "",
                str(e.revision or ""), str(e.changed_rev or ""), e.author, fmt_date(e.date, self.config),
                lock, e.changelist or ""])
            it.setData(0, Qt.UserRole, path)
            it.setData(0, Qt.UserRole + 1, 0 if is_dir else 1)  # carpetas primero
            it.setIcon(0, status_icon(icon_key, is_dir))
            if colorize:
                col = color_for(k)
                if col:
                    for c in (0, 1, 2):
                        it.setForeground(c, col)
                elif icon_key == "modified":
                    it.setForeground(1, color_for("modified"))
            if e.switched:
                it.setToolTip(0, "Switched")
            return it

        if only:
            for path in sorted(self.entries):
                e = self.entries[path]
                if visible(e) and path != root:
                    it = make(path, e)
                    self.tree.addTopLevelItem(it)
                    self.items[path] = it
            if root in self.entries and visible(self.entries[root]):
                it = make(root, self.entries[root])
                self.tree.insertTopLevelItem(0, it)
                self.items[root] = it
        else:
            for path in sorted(self.entries, key=lambda p: (p.count(os.sep), p)):
                e = self.entries[path]
                if not visible(e):
                    continue
                it = make(path, e)
                parent = self.items.get(os.path.dirname(path)) if path != root else None
                if parent is not None:
                    parent.addChild(it)
                elif path == root or os.path.dirname(path) not in self.entries:
                    self.tree.addTopLevelItem(it)
                else:
                    continue  # padre oculto
                self.items[path] = it
            if root in self.items:
                self.items[root].setExpanded(True)
        for path in expanded:
            if path in self.items:
                self.items[path].setExpanded(True)
        for path in selected:
            if path in self.items:
                self.items[path].setSelected(True)
        self.tree.setSortingEnabled(True)
        if first_load:
            for c in range(1, len(COLS)):
                self.tree.resizeColumnToContents(c)
        self.apply_filter()
        parts = [f"{STATUS_LABELS.get(k, k)}: {n}" for k, n in sorted(counts.items())
                 if k not in ("normal",) and n]
        self.statusBar().showMessage(f"{len(self.entries)} elementos" + (" · " + ", ".join(parts) if parts else
                                                                           " · sin cambios locales"))

    def apply_filter(self):
        q = self.search.text().strip().lower()

        def walk(it) -> bool:
            match = not q or q in it.text(0).lower()
            child_match = False
            for i in range(it.childCount()):
                child_match = walk(it.child(i)) or child_match
            show = match or child_match
            it.setHidden(not show)
            if q and child_match:
                it.setExpanded(True)
            return show
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    # ================================================================== selección y menús
    def selected_paths(self, default_root: bool = True) -> list:
        paths = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]
        if not paths and default_root and self.ctx.project:
            return [self.ctx.project.wc_path]
        return paths

    def _selection_changed(self):
        n = len(self.tree.selectedItems())
        if n > 1:
            self.statusBar().showMessage(f"{n} elementos seleccionados", 3000)

    def _double_click(self, item, _col):
        path = item.data(0, Qt.UserRole)
        if os.path.isdir(path):
            return
        e = self.entries.get(path)
        if e and e.is_changed and e.is_versioned and e.item not in ("added", "missing"):
            self.actions.diff([path])
        elif os.path.exists(path):
            open_path(path, self.config)

    def _tree_menu(self, pos):
        if not self.ctx.project:
            return
        paths = self.selected_paths(default_root=False)
        if not paths:
            paths = [self.ctx.project.wc_path]
        m = QMenu(self)
        if len(paths) == 1:
            p = paths[0]
            m.addAction("Abrir", lambda: open_path(p, self.config))
            m.addAction("Abrir carpeta contenedora",
                        lambda: open_path(p if os.path.isdir(p) else os.path.dirname(p), self.config))
        m.addAction("Copiar ruta(s)", lambda: QGuiApplication.clipboard().setText("\n".join(paths)))
        m.addSeparator()
        self.actions.build_menu(m, paths, self.entries)
        m.exec(self.tree.viewport().mapToGlobal(pos))
