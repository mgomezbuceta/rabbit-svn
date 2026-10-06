"""Annotate / blame."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .common import RevisionWidget, fmt_date, mono_font
from .context import Context, run_svn
from .diff import diff_change


def _rev_color(rev: Optional[int], newest: int, oldest: int) -> QColor:
    if rev is None:
        return QColor(255, 236, 179)  # cambio local sin commit
    span = max(1, newest - oldest)
    t = (rev - oldest) / span  # 0 antiguo .. 1 reciente
    return QColor.fromHsvF(0.58 - 0.5 * t, 0.12 + 0.25 * t, 1.0)


class AnnotateDialog(QDialog):
    def __init__(self, parent, ctx: Context, target: str, revision: Optional[str] = None, actions=None):
        super().__init__(parent)
        self.ctx = ctx
        self.target = target
        self.actions = actions
        self.setWindowTitle(f"Annotate: {target}")
        self.resize(1100, 760)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.rev = RevisionWidget(allow_working=True)
        if revision:
            self.rev.set_revision(revision)
        elif "://" not in target:
            self.rev.working.setChecked(True)
        top.addWidget(QLabel("Revisión:"))
        top.addWidget(self.rev, 1)
        self.merged = QCheckBox("Usar historial de fusiones")
        top.addWidget(self.merged)
        b = QPushButton("Cargar")
        b.clicked.connect(self.load)
        top.addWidget(b)
        lay.addLayout(top)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Resaltar texto, autor o revisión…")
        self.search.textChanged.connect(self.highlight)
        lay.addWidget(self.search)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Línea", "Rev.", "Autor", "Fecha", "Texto"])
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setFont(mono_font())
        self.table.header().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        lay.addWidget(self.table)
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.load()

    def load(self):
        self.table.clear()
        self.status.setText("Cargando…")
        rev = self.rev.value()
        merged = self.merged.isChecked()

        def done(lines):
            revs = [l.revision for l in lines if l.revision is not None]
            newest, oldest = (max(revs), min(revs)) if revs else (0, 0)
            colorize = self.ctx.config.get("general", "enable_highlighting", True)
            items = []
            for l in lines:
                it = QTreeWidgetItem([str(l.line_no), str(l.revision or "—"), l.author or "(local)",
                                      fmt_date(l.date, self.ctx.config), l.text.expandtabs(4)])
                it.setData(0, Qt.UserRole, l)
                if colorize:
                    col = _rev_color(l.revision, newest, oldest)
                    for c in range(4):
                        it.setBackground(c, col)
                items.append(it)
            self.table.addTopLevelItems(items)
            for c in range(4):
                self.table.resizeColumnToContents(c)
            authors = len({l.author for l in lines})
            self.status.setText(f"{len(lines)} líneas · {len(set(revs))} revisiones · {authors} autores")
        run_svn(self, self.ctx, lambda c: c.blame(self.target, rev, merged), done)

    def highlight(self, text):
        q = text.strip().lower()
        first = None
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            hit = bool(q) and any(q in it.text(c).lower() for c in (1, 2, 4))
            f = it.font(4)
            f.setBold(hit)
            it.setFont(4, f)
            if hit and first is None:
                first = it
        if first:
            self.table.scrollToItem(first)

    def menu(self, pos):
        it = self.table.itemAt(pos)
        if not it:
            return
        l = it.data(0, Qt.UserRole)
        m = QMenu(self)
        if l.revision:
            m.addAction(f"Ver cambios de r{l.revision}", lambda: diff_change(self, self.ctx, self.target, l.revision))
            m.addAction(f"Mostrar log en r{l.revision}", lambda: self._log(l.revision))
            m.addAction(f"Annotate de la revisión anterior (r{l.revision - 1})",
                        lambda: AnnotateDialog(self.parent(), self.ctx, self.target, str(l.revision - 1),
                                               self.actions).show())
        m.addAction("Copiar línea", lambda: QGuiApplication.clipboard().setText(l.text))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _log(self, rev):
        from .log_dialog import LogDialog
        LogDialog(self.parent(), self.ctx, self.target, actions=self.actions, select_rev=rev).show()
