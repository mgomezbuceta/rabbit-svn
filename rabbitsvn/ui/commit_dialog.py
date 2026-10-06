"""Diálogo de commit."""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QMenu, QMessageBox,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from ..svn.client import SvnClient
from .action import ActionDialog
from .common import FileTable, MessageEdit, check_all_row, confirm, esc, open_path, status_key
from .context import Context, run_svn
from .diff import diff_working


class CommitDialog(QDialog):
    def __init__(self, parent, ctx: Context, paths, base_dir: str, on_done: Optional[Callable] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.paths = list(paths)
        self.base_dir = base_dir
        self.on_done = on_done
        self.setWindowTitle("Commit")
        self.resize(900, 680)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Destino:</b> {esc(ctx.project.url if ctx.project else '')}  "
                             f"<span style='color:gray'>({esc(base_dir)})</span>"))
        split = QSplitter(Qt.Vertical)
        self.msg = MessageEdit(ctx.config)
        split.addWidget(self.msg)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.table = FileTable(base_dir)
        self.table.itemDoubleClicked.connect(lambda it, _c: self._diff([it.data(0, Qt.UserRole)]))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        row = QHBoxLayout()
        self.check_all = check_all_row(self.table)
        row.addWidget(self.check_all)
        self.show_unversioned = QCheckBox("Mostrar sin versionar")
        self.show_unversioned.setChecked(ctx.config.get("general", "show_unversioned_files", True))
        self.show_unversioned.toggled.connect(self.refresh)
        row.addWidget(self.show_unversioned)
        self.keep_locks = QCheckBox("Mantener bloqueos")
        row.addWidget(self.keep_locks)
        refresh = QPushButton("Refrescar")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        bl.addLayout(row)
        bl.addWidget(self.table)
        self.count = QLabel("")
        bl.addWidget(self.count)
        split.addWidget(bottom)
        split.setSizes([200, 460])
        lay.addWidget(split)
        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Commit")
        ok.setDefault(True)
        ok.clicked.connect(self.do_commit)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        self.table.itemChanged.connect(self._update_count)
        self.refresh()

    def refresh(self):
        show_unv = self.show_unversioned.isChecked()
        no_ignore = self.ctx.config.get("general", "show_ignored_files", False)

        def done(entries):
            items = [e for e in entries
                     if e.is_changed and (show_unv or e.item != "unversioned")
                     and not (e.item == "ignored" and not no_ignore)
                     and e.item != "external"]
            self.table.set_entries(items, checked=lambda e: e.item not in ("unversioned", "ignored"))
            self._update_count()
        run_svn(self, self.ctx, lambda c: c.status(self.paths, no_ignore=False), done)

    def _update_count(self, *_):
        n = len(self.table.checked_paths())
        self.count.setText(f"{n} de {self.table.topLevelItemCount()} elementos seleccionados")

    def _diff(self, paths):
        paths = [p for p in paths if self.table.entries.get(p) and self.table.entries[p].item != "unversioned"]
        if paths:
            diff_working(self, self.ctx, paths, self.base_dir)

    def _menu(self, pos):
        sel = self.table.selected_paths()
        if not sel:
            return
        m = QMenu(self)
        m.addAction("Ver diferencias", lambda: self._diff(sel))
        m.addAction("Abrir", lambda: [open_path(p, self.ctx.config) for p in sel])
        m.addSeparator()
        m.addAction("Revertir…", lambda: self._revert(sel))
        unv = [p for p in sel if self.table.entries[p].item == "unversioned"]
        if unv:
            m.addAction("Añadir", lambda: self._quick(lambda c: c.cmd_add(unv), "Añadir"))
            m.addAction("Ignorar (añadir a svn:ignore)", lambda: self._ignore(unv))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _quick(self, factory, title):
        dlg = ActionDialog(self, self.ctx, title, [factory], lambda _d: self.refresh())
        dlg.setModal(True)
        dlg.start()

    def _revert(self, paths):
        if confirm(self, f"¿Revertir {len(paths)} elemento(s)? Se perderán los cambios locales."):
            self._quick(lambda c: c.cmd_revert(paths), "Revertir")

    def _ignore(self, paths):
        from .ops import add_to_ignore
        add_to_ignore(self, self.ctx, paths, by_extension=False, on_done=self.refresh)

    # ------------------------------------------------------------------
    def do_commit(self):
        message = self.msg.text()
        targets = self.table.checked_paths()
        if not targets:
            QMessageBox.information(self, "Commit", "No hay elementos seleccionados.")
            return
        if not message and not confirm(self, "El mensaje está vacío. ¿Hacer commit de todos modos?"):
            return
        entries = self.table.entries
        conflicted = [p for p in targets if entries[p].is_conflicted]
        if conflicted:
            QMessageBox.warning(self, "Commit", "Hay elementos en conflicto. Resuélvelos antes de hacer commit:\n\n"
                                + "\n".join(self.table.rel(p) for p in conflicted[:20]))
            return
        root = os.path.realpath(self.base_dir)
        outside = [p for p in targets if not (os.path.realpath(p) + os.sep).startswith(root + os.sep)]
        if outside:
            QMessageBox.warning(self, "Commit", "Hay rutas fuera de la working copy del proyecto.")
            return
        to_add = [p for p in targets if entries[p].item == "unversioned"]
        to_del = [p for p in targets if entries[p].item == "missing"]
        new_dirs = [p for p in to_add if os.path.isdir(p)]
        keep_locks = self.keep_locks.isChecked()
        steps = []
        if to_add:
            steps.append(lambda c: c.cmd_add(to_add, recursive=True))
        if to_del:
            steps.append(lambda c: c.cmd_delete(to_del))

        def commit_step(c: SvnClient):
            all_targets = list(targets)
            if new_dirs:  # incluir el contenido de las carpetas recién añadidas
                for e in c.status(new_dirs):
                    if e.item == "added" and e.path not in all_targets:
                        all_targets.append(e.path)
            return c.cmd_commit(all_targets, message, keep_locks=keep_locks)
        steps.append(commit_step)

        def ok(_dlg):
            self.ctx.config.remember_message(message)
            if self.on_done:
                self.on_done()
            self.accept()
        dlg = ActionDialog(self.parentWidget() or self, self.ctx, "Commit", steps, ok)
        dlg.setModal(True)
        dlg.start()
