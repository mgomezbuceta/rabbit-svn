"""Comprobar modificaciones locales y remotas (svn status / svn status -u)."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QPushButton,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .common import STATUS_LABELS, color_for, fmt_date, status_icon, status_key
from .context import Context, run_svn
from .diff import diff_working, external_diff, DiffDialog


class CheckModsDialog(QDialog):
    def __init__(self, parent, ctx: Context, paths, base_dir: str, actions=None):
        super().__init__(parent)
        self.ctx = ctx
        self.paths = list(paths)
        self.base_dir = base_dir
        self.actions = actions
        self.setWindowTitle("Comprobar modificaciones")
        self.resize(950, 560)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.local = self._table(["Ruta", "Extensión", "Estado", "Propiedades"])
        self.remote = self._table(["Ruta", "Extensión", "Estado remoto", "Propiedades remotas",
                                   "Bloqueo remoto", "Última rev. local", "Autor", "Fecha"])
        self.tabs.addTab(self.local, "Modificaciones locales")
        self.tabs.addTab(self.remote, "Modificaciones en el repositorio")
        self.tabs.currentChanged.connect(self._tab_changed)
        lay.addWidget(self.tabs)
        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        b = QPushButton("Refrescar")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        c = QPushButton("Cerrar")
        c.clicked.connect(self.close)
        row.addWidget(c)
        lay.addLayout(row)
        self.remote_loaded = False
        self.refresh_local()

    def _table(self, headers):
        t = QTreeWidget()
        t.setHeaderLabels(headers)
        t.setRootIsDecorated(False)
        t.setSortingEnabled(True)
        t.setSelectionMode(QTreeWidget.ExtendedSelection)
        t.header().setSectionResizeMode(0, QHeaderView.Stretch)
        t.setContextMenuPolicy(Qt.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos, t=t: self._menu(t, pos))
        t.itemDoubleClicked.connect(lambda it, _c, t=t: self._diff(t, [it.data(0, Qt.UserRole)]))
        return t

    def _rel(self, p):
        r = os.path.relpath(p, self.base_dir)
        return p if r.startswith("..") else r

    def refresh(self):
        self.refresh_local()
        if self.remote_loaded or self.tabs.currentIndex() == 1:
            self.refresh_remote()

    def _tab_changed(self, idx):
        if idx == 1 and not self.remote_loaded:
            self.refresh_remote()

    def refresh_local(self):
        def done(entries):
            self.local.clear()
            for e in entries:
                if not e.is_changed or e.item in ("ignored", "external"):
                    continue
                k = status_key(e)
                it = QTreeWidgetItem([self._rel(e.path), os.path.splitext(e.path)[1],
                                      STATUS_LABELS.get(k, k),
                                      STATUS_LABELS.get(e.props, "") if e.props not in ("none", "normal") else ""])
                it.setIcon(0, status_icon(k, os.path.isdir(e.path)))
                col = color_for(k)
                if col:
                    it.setForeground(0, col)
                    it.setForeground(2, col)
                it.setData(0, Qt.UserRole, e.path)
                self.local.addTopLevelItem(it)
            self.status.setText(f"{self.local.topLevelItemCount()} cambios locales")
        run_svn(self, self.ctx, lambda c: c.status(self.paths), done)

    def refresh_remote(self):
        self.status.setText("Consultando el repositorio…")

        def done(entries):
            self.remote_loaded = True
            self.remote.clear()
            n = 0
            for e in entries:
                if not e.has_remote_changes and not e.repos_lock:
                    continue
                k = e.repos_item or "normal"
                it = QTreeWidgetItem([self._rel(e.path), os.path.splitext(e.path)[1],
                                      STATUS_LABELS.get(k, k) if k not in ("none", "normal") else "",
                                      STATUS_LABELS.get(e.repos_props or "", "")
                                      if e.repos_props not in (None, "none", "normal") else "",
                                      e.repos_lock.owner if e.repos_lock else "",
                                      str(e.changed_rev or ""), e.author, fmt_date(e.date, self.ctx.config)])
                col = color_for(k)
                if col:
                    it.setForeground(2, col)
                it.setData(0, Qt.UserRole, e.path)
                self.remote.addTopLevelItem(it)
                n += 1
            self.status.setText(f"{n} elementos cambiados en el repositorio")
        run_svn(self, self.ctx, lambda c: c.status(self.paths, show_updates=True), done)

    def _diff(self, table, paths):
        if table is self.local:
            diff_working(self, self.ctx, paths, self.base_dir)
        else:
            for p in paths:
                if self.ctx.config.get("external", "diff_tool") and os.path.isfile(p):
                    external_diff(self, self.ctx, p, None, p, "HEAD")
                else:
                    DiffDialog(self, self.ctx, f"Copia de trabajo ↔ HEAD: {self._rel(p)}",
                               lambda c, ws, p=p: c.diff([p], revision="BASE:HEAD", ignore_whitespace=ws)).show()

    def _menu(self, table, pos):
        sel = [it.data(0, Qt.UserRole) for it in table.selectedItems()]
        if not sel:
            return
        m = QMenu(self)
        m.addAction("Ver diferencias", lambda: self._diff(table, sel))
        if self.actions:
            m.addAction("Mostrar log", lambda: self.actions.log(sel[0]))
            if table is self.remote:
                m.addAction("Actualizar estos elementos", lambda: self.actions.update(sel, on_done=self.refresh))
            else:
                m.addAction("Commit…", lambda: self.actions.commit(sel))
                m.addAction("Revertir…", lambda: self.actions.revert(sel, on_done=self.refresh))
        m.exec(table.viewport().mapToGlobal(pos))
