"""Navegador del repositorio (svn list)."""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QMenu, QPushButton, QStyle, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from .action import ActionDialog
from .common import (RevisionWidget, esc, UrlCombo, confirm, fmt_date, human_size, open_path, safe_filename,
                     save_bytes, write_private_temp)
from .context import Context, run_svn

PLACEHOLDER = "__cargando__"


class RepoBrowser(QDialog):
    def __init__(self, parent, ctx: Context, url: str = "", revision: Optional[str] = None, actions=None,
                 pick_mode: bool = False):
        super().__init__(parent)
        self.ctx = ctx
        self.actions = actions
        self.pick_mode = pick_mode
        self.picked_url = ""
        self.setWindowTitle("Navegador del repositorio" + (" — seleccionar URL" if pick_mode else ""))
        self.resize(1000, 650)
        if not pick_mode:
            self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("URL:"))
        self.url = UrlCombo(ctx.config, url or (ctx.project.url if ctx.project else ""))
        self.url.lineEdit().returnPressed.connect(self.go)
        top.addWidget(self.url, 1)
        go = QPushButton("Ir")
        go.clicked.connect(self.go)
        top.addWidget(go)
        lay.addLayout(top)
        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("Revisión:"))
        self.rev = RevisionWidget()
        if revision:
            self.rev.set_revision(revision)
        rrow.addWidget(self.rev, 1)
        root_btn = QPushButton("Ir a la raíz")
        root_btn.clicked.connect(self.go_root)
        rrow.addWidget(root_btn)
        lay.addLayout(rrow)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nombre", "Tamaño", "Revisión", "Autor", "Fecha", "Bloqueo"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemDoubleClicked.connect(self._double)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.menu)
        lay.addWidget(self.tree)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        bottom.addWidget(self.status, 1)
        if pick_mode:
            ok = QPushButton("Seleccionar")
            ok.clicked.connect(self._pick)
            bottom.addWidget(ok)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        self.repo_root = ""
        if self.url.text():
            self.go()

    def _revision(self) -> Optional[str]:
        v = self.rev.value()
        return None if v == "HEAD" else v

    def go_root(self):
        if self.repo_root:
            self.url.setText(self.repo_root)
            self.go()

    def go(self):
        url = self.url.text()
        if not url:
            return
        self.tree.clear()
        style = QApplication.style()
        root = QTreeWidgetItem([url])
        root.setIcon(0, style.standardIcon(QStyle.SP_DriveNetIcon))
        root.setData(0, Qt.UserRole, (url, "dir"))
        self.tree.addTopLevelItem(root)
        root.addChild(QTreeWidgetItem([PLACEHOLDER]))
        self.ctx.config.remember_url(url)
        run_svn(self, self.ctx, lambda c: c.info(url, self._revision()),
                lambda infos: setattr(self, "repo_root", infos[0].repo_root if infos else ""), lambda e: None)
        root.setExpanded(True)

    def _expand(self, item):
        if item.childCount() != 1 or item.child(0).text(0) != PLACEHOLDER:
            return
        url, _kind = item.data(0, Qt.UserRole)
        item.child(0).setText(0, "Cargando…")
        self.status.setText(f"Listando {esc(unquote(url))}…")
        style = QApplication.style()

        def done(entries):
            item.takeChildren()
            for e in sorted(entries, key=lambda e: (e.kind != "dir", e.name.lower())):
                if not e.name:
                    continue
                child = QTreeWidgetItem([e.name, human_size(e.size) if e.kind == "file" else "",
                                         str(e.revision or ""), e.author, fmt_date(e.date, self.ctx.config),
                                         e.lock.owner if e.lock else ""])
                child.setIcon(0, style.standardIcon(QStyle.SP_DirIcon if e.kind == "dir" else QStyle.SP_FileIcon))
                child.setData(0, Qt.UserRole, (url.rstrip("/") + "/" + quote(e.name), e.kind))
                if e.kind == "dir":
                    child.addChild(QTreeWidgetItem([PLACEHOLDER]))
                item.addChild(child)
            self.status.setText(f"{len(entries)} elementos en {esc(unquote(url))}")
            for c in range(1, 5):
                self.tree.resizeColumnToContents(c)

        def err(exc):
            item.takeChildren()
            item.addChild(QTreeWidgetItem([PLACEHOLDER]))
            item.setExpanded(False)
            self.status.setText("Error al listar")
            from .common import show_error
            show_error(self, exc)
        run_svn(self, self.ctx, lambda c: c.list(url, self._revision()), done, err)

    def _current(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else (None, None)

    def _double(self, item, _col):
        url, kind = item.data(0, Qt.UserRole) or (None, None)
        if kind == "file":
            if self.pick_mode:
                self._pick()
            else:
                self.open_file(url)

    def _pick(self):
        url, _ = self._current()
        self.picked_url = url or self.url.text()
        self.accept()

    def _refresh_item(self, it):
        if it is None:
            return
        it.takeChildren()
        it.addChild(QTreeWidgetItem([PLACEHOLDER]))
        it.setExpanded(False)
        it.setExpanded(True)

    # ------------------------------------------------------------------ menú
    def menu(self, pos):
        it = self.tree.itemAt(pos)
        if not it:
            return
        url, kind = it.data(0, Qt.UserRole)
        parent_it = it.parent() or it
        rev = self._revision()
        m = QMenu(self)
        if kind == "file":
            m.addAction("Abrir", lambda: self.open_file(url))
            m.addAction("Guardar como…", lambda: self.save_file(url))
        m.addAction("Mostrar log", lambda: self._log(url))
        if kind == "file" and self.actions:
            m.addAction("Annotate", lambda: self.actions.annotate(url, revision=rev))
        m.addSeparator()
        if kind == "dir" and self.actions:
            m.addAction("Checkout…", lambda: self.actions.checkout(url))
        if self.actions:
            m.addAction("Exportar…", lambda: self.actions.export(url, revision=rev))
            m.addAction("Crear rama/etiqueta desde aquí…", lambda: self.actions.branch(url, revision=rev))
        m.addSeparator()
        if kind == "dir":
            m.addAction("Crear carpeta…", lambda: self.mkdir(url, it))
        m.addAction("Renombrar / mover…", lambda: self.rename(url, parent_it))
        m.addAction("Eliminar…", lambda: self.delete(url, parent_it))
        m.addSeparator()
        m.addAction("Propiedades…", lambda: self._props(url))
        m.addAction("Copiar URL", lambda: QGuiApplication.clipboard().setText(url))
        m.addAction("Refrescar", lambda: self._refresh_item(it if kind == "dir" else parent_it))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _peg(self, url):
        rev = self._revision()
        return f"{url}@{rev}" if rev and rev.isdigit() else url

    def _log(self, url):
        from .log_dialog import LogDialog
        LogDialog(self.parent() or self, self.ctx, self._peg(url), actions=self.actions).show()

    def _props(self, url):
        from .properties_dialog import PropertiesDialog
        PropertiesDialog(self, self.ctx, self._peg(url)).exec()

    def open_file(self, url):
        name = safe_filename(os.path.basename(url))

        def done(data):
            open_path(write_private_temp(data, name), self.ctx.config, untrusted=True)
        run_svn(self, self.ctx, lambda c: c.cat(url, self._revision()), done)

    def save_file(self, url):
        name = safe_filename(os.path.basename(url))
        path, _ = QFileDialog.getSaveFileName(self, "Guardar como", os.path.join(os.path.expanduser("~"), name))
        if path:
            run_svn(self, self.ctx, lambda c: c.cat(url, self._revision()),
                    lambda data: save_bytes(self, path, data))

    def _message(self, title, default=""):
        text, ok = QInputDialog.getMultiLineText(self, title, "Mensaje de log:", default)
        return text if ok else None

    def mkdir(self, url, it):
        name, ok = QInputDialog.getText(self, "Crear carpeta", "Nombre de la nueva carpeta:")
        if not ok or not name.strip():
            return
        msg = self._message("Crear carpeta", f"Crear carpeta {name.strip()}")
        if msg is None:
            return
        new = url.rstrip("/") + "/" + quote(name.strip())
        ActionDialog(self, self.ctx, "Crear carpeta", [lambda c: c.cmd_mkdir([new], msg)],
                     lambda d: (self._refresh_item(it), d.accept())).start()

    def rename(self, url, parent_it):
        new, ok = QInputDialog.getText(self, "Renombrar / mover", "Nueva URL:", text=unquote(url))
        if not ok or not new.strip() or new.strip() == unquote(url):
            return
        msg = self._message("Renombrar", f"Mover {unquote(url)} a {new.strip()}")
        if msg is None:
            return
        ActionDialog(self, self.ctx, "Renombrar", [lambda c: c.cmd_move(url, new.strip(), message=msg)],
                     lambda d: (self._refresh_item(parent_it), d.accept())).start()

    def delete(self, url, parent_it):
        if not confirm(self, f"¿Eliminar del repositorio?\n{unquote(url)}"):
            return
        msg = self._message("Eliminar", f"Eliminar {unquote(os.path.basename(url))}")
        if msg is None:
            return
        ActionDialog(self, self.ctx, "Eliminar", [lambda c: c.cmd_delete([url], message=msg)],
                     lambda d: (self._refresh_item(parent_it), d.accept())).start()


def pick_url(parent, ctx: Context, start: str = "") -> Optional[str]:
    dlg = RepoBrowser(parent, ctx, start, pick_mode=True)
    if dlg.exec() == QDialog.Accepted:
        return dlg.picked_url
    return None
