"""Historial de revisiones (svn log)."""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .common import (LOG_ACTIONS, STATUS_COLORS, confirm, fmt_date, mono_font, open_path, safe_filename,
                     save_bytes, write_private_temp)
from .context import Context, run_svn
from .diff import diff_change, diff_revisions, diff_urls

ACTION_COLOR = {"A": "added", "D": "deleted", "M": "modified", "R": "replaced"}


class LogDialog(QDialog):
    def __init__(self, parent, ctx: Context, target: str, wc_path: Optional[str] = None,
                 actions=None, select_rev: Optional[int] = None, pick_mode: bool = False):
        super().__init__(parent)
        self.ctx = ctx
        self.target = target
        self.wc_path = wc_path          # si el target es una working copy
        self.actions = actions          # objeto Actions de la ventana principal (opcional)
        self.select_rev = select_rev
        self.pick_mode = pick_mode      # usado para elegir revisiones desde otros diálogos
        self.entries: list = []
        self.repo_root = ""
        self.target_url = ""
        self.kind = "dir"
        self.finished_loading = False
        self.setWindowTitle(f"Log: {target}")
        self.resize(1100, 760)
        if not pick_mode:
            self.setAttribute(Qt.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrar por mensaje, autor, ruta o revisión…")
        self.filter.textChanged.connect(self.apply_filter)
        top.addWidget(self.filter, 1)
        self.stop_on_copy = QCheckBox("Detener en copias")
        self.stop_on_copy.toggled.connect(self.reload)
        top.addWidget(self.stop_on_copy)
        self.merged = QCheckBox("Incluir revisiones fusionadas")
        self.merged.toggled.connect(self.reload)
        top.addWidget(self.merged)
        lay.addLayout(top)

        split = QSplitter(Qt.Vertical)
        self.revs = QTreeWidget()
        self.revs.setHeaderLabels(["Revisión", "Autor", "Fecha", "Mensaje", "Rutas"])
        self.revs.setRootIsDecorated(False)
        self.revs.setAlternatingRowColors(True)
        self.revs.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.revs.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.revs.itemSelectionChanged.connect(self.show_selected)
        self.revs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.revs.customContextMenuRequested.connect(self.rev_menu)
        split.addWidget(self.revs)
        self.msg = QPlainTextEdit()
        self.msg.setReadOnly(True)
        self.msg.setFont(mono_font())
        split.addWidget(self.msg)
        self.paths = QTreeWidget()
        self.paths.setHeaderLabels(["Acción", "Ruta", "Copiado desde", "Rev. origen"])
        self.paths.setRootIsDecorated(False)
        self.paths.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.paths.setContextMenuPolicy(Qt.CustomContextMenu)
        self.paths.customContextMenuRequested.connect(self.path_menu)
        self.paths.itemDoubleClicked.connect(lambda it, _c: self._path_diff(it))
        split.addWidget(self.paths)
        split.setSizes([380, 110, 220])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        bottom.addWidget(self.status, 1)
        self.more_btn = QPushButton(f"Siguientes {self._limit()}")
        self.more_btn.clicked.connect(self.load_more)
        bottom.addWidget(self.more_btn)
        self.all_btn = QPushButton("Mostrar todo")
        self.all_btn.clicked.connect(lambda: self.load_more(all_=True))
        bottom.addWidget(self.all_btn)
        refresh = QPushButton("Refrescar")
        refresh.clicked.connect(self.reload)
        bottom.addWidget(refresh)
        if pick_mode:
            ok = QPushButton("Usar selección")
            ok.clicked.connect(self.accept)
            bottom.addWidget(ok)
        close = QPushButton("Cerrar" if not pick_mode else "Cancelar")
        close.clicked.connect(self.reject if pick_mode else self.close)
        bottom.addWidget(close)
        lay.addLayout(bottom)

        self._init()

    def _peg(self) -> Optional[str]:
        last = self.target.rsplit("/", 1)[-1]
        if "@" in last and last.rsplit("@", 1)[1].isdigit():
            return last.rsplit("@", 1)[1]
        return None

    def _limit(self) -> int:
        return int(self.ctx.config.get("general", "log_limit", 100) or 100)

    def _init(self):
        def done(infos):
            if infos:
                self.repo_root = infos[0].repo_root
                self.target_url = infos[0].url
                self.kind = infos[0].kind
            self.reload()
        run_svn(self, self.ctx, lambda c: c.info(self.target), done)

    # ------------------------------------------------------------------ carga
    def reload(self):
        self.entries = []
        self.revs.clear()
        self.finished_loading = False
        self._load(f"{self._peg() or 'HEAD'}:1", self._limit())

    def load_more(self, all_: bool = False):
        if self.finished_loading:
            return
        last = self.entries[-1].revision if self.entries else None
        rng = f"{last - 1}:1" if last else f"{self._peg() or 'HEAD'}:1"
        if last is not None and last <= 1:
            return
        self._load(rng, None if all_ else self._limit())

    def _load(self, rng: str, limit: Optional[int]):
        self.status.setText("Cargando…")
        self.more_btn.setEnabled(False)
        self.all_btn.setEnabled(False)
        target = self.target if self._peg() else (self.target_url or self.target)
        soc, merged = self.stop_on_copy.isChecked(), self.merged.isChecked()

        def done(entries):
            if limit is None or len(entries) < limit:
                self.finished_loading = True
            first = not self.entries
            self.entries.extend(entries)
            for e in entries:
                self._add(e)
            if first:
                for col in (0, 1, 2):
                    self.revs.resizeColumnToContents(col)
            self.status.setText(f"{len(self.entries)} revisiones cargadas" +
                                (" (todas)" if self.finished_loading else ""))
            self.more_btn.setEnabled(not self.finished_loading)
            self.all_btn.setEnabled(not self.finished_loading)
            self.apply_filter()
            if self.select_rev is not None:
                self.select_revision(self.select_rev)
                self.select_rev = None
            elif self.revs.topLevelItemCount() and not self.revs.selectedItems():
                self.revs.setCurrentItem(self.revs.topLevelItem(0))

        def err(exc):
            self.status.setText("Error al cargar el log")
            self.more_btn.setEnabled(True)
            from .common import show_error
            show_error(self, exc)
        run_svn(self, self.ctx, lambda c: c.log(target, limit=limit, revision=rng, stop_on_copy=soc,
                                                use_merge_history=merged), done, err)

    def _add(self, e):
        first = (e.message or "").strip().splitlines()[0] if e.message.strip() else ""
        it = QTreeWidgetItem([str(e.revision), e.author, fmt_date(e.date, self.ctx.config), first,
                              str(len(e.paths))])
        it.setData(0, Qt.UserRole, e)
        it.setData(0, Qt.DisplayRole, e.revision)
        self.revs.addTopLevelItem(it)

    def select_revision(self, rev: int):
        for i in range(self.revs.topLevelItemCount()):
            it = self.revs.topLevelItem(i)
            if it.data(0, Qt.UserRole).revision == rev:
                self.revs.setCurrentItem(it)
                self.revs.scrollToItem(it)
                return

    def apply_filter(self):
        q = self.filter.text().strip().lower()
        for i in range(self.revs.topLevelItemCount()):
            it = self.revs.topLevelItem(i)
            e = it.data(0, Qt.UserRole)
            hay = f"{e.revision} {e.author} {e.message} " + " ".join(p.path for p in e.paths)
            it.setHidden(bool(q) and q not in hay.lower())

    # ------------------------------------------------------------------ selección
    def selected(self) -> list:
        return sorted((it.data(0, Qt.UserRole) for it in self.revs.selectedItems()),
                      key=lambda e: e.revision)

    def show_selected(self):
        sel = self.selected()
        self.paths.clear()
        if not sel:
            self.msg.clear()
            return
        e = sel[-1]
        self.msg.setPlainText(e.message)
        rel = self._relative_target()
        for p in sorted(e.paths, key=lambda p: p.path):
            it = QTreeWidgetItem([LOG_ACTIONS.get(p.action, p.action), p.path, p.copyfrom_path,
                                  str(p.copyfrom_rev or "")])
            it.setData(0, Qt.UserRole, (e, p))
            col = STATUS_COLORS.get(ACTION_COLOR.get(p.action, ""))
            if col:
                it.setForeground(0, QColor(col))
            if rel and not (p.path == rel or p.path.startswith(rel.rstrip("/") + "/")):
                for c in range(4):
                    it.setForeground(c, QColor("#9e9e9e"))
            self.paths.addTopLevelItem(it)

    def _relative_target(self) -> str:
        if self.target_url and self.repo_root and self.target_url.startswith(self.repo_root):
            from urllib.parse import unquote
            return unquote(self.target_url[len(self.repo_root):]) or "/"
        return ""

    def _url_of(self, repo_path: str) -> str:
        return self.repo_root + quote(repo_path)

    # ------------------------------------------------------------------ menús
    def rev_menu(self, pos):
        sel = self.selected()
        if not sel:
            return
        m = QMenu(self)
        target = self.target_url or self.target
        e = sel[-1]
        if len(sel) == 1:
            m.addAction("Ver cambios de esta revisión", lambda: diff_change(self, self.ctx, target, e.revision))
            if self.kind == "file":
                m.addAction("Comparar con la revisión anterior",
                            lambda: diff_revisions(self, self.ctx, target, str(e.revision - 1), str(e.revision)))
                if self.wc_path:
                    m.addAction("Comparar con la copia de trabajo",
                                lambda: self._compare_wc(e.revision))
        else:
            a, b = sel[0], sel[-1]
            m.addAction(f"Comparar r{a.revision} con r{b.revision}",
                        lambda: diff_revisions(self, self.ctx, target, str(a.revision), str(b.revision),
                                               kind=self.kind))
            m.addAction(f"Ver todos los cambios r{a.revision}–r{b.revision}",
                        lambda: diff_revisions(self, self.ctx, target, str(a.revision - 1), str(b.revision),
                                               prefer_external=False, kind="dir"))
        m.addAction("Ver lista de cambios entre revisiones…", lambda: self._changes(sel))
        if self.actions and self.wc_path:
            m.addSeparator()
            m.addAction(f"Actualizar copia de trabajo a r{e.revision}",
                        lambda: self.actions.update_to(self.wc_path_list(), str(e.revision)))
            revs = [s.revision for s in sel]
            m.addAction("Revertir los cambios de esta(s) revisión(es)",
                        lambda: self._merge(revs, reverse=True))
            m.addAction("Fusionar esta(s) revisión(es) en la copia de trabajo",
                        lambda: self._merge(revs, reverse=False))
        if self.actions:
            m.addSeparator()
            m.addAction("Crear rama/etiqueta desde esta revisión…",
                        lambda: self.actions.branch(self.wc_path or target, revision=str(e.revision)))
            m.addAction("Exportar esta revisión…",
                        lambda: self.actions.export(target, revision=str(e.revision)))
            m.addAction("Navegar el repositorio en esta revisión",
                        lambda: self.actions.browser(target, revision=str(e.revision)))
        m.addSeparator()
        m.addAction("Editar mensaje…", lambda: self._edit_revprop(e, "svn:log"))
        m.addAction("Editar autor…", lambda: self._edit_revprop(e, "svn:author"))
        m.addAction("Propiedades de revisión…", lambda: self._revprops(e))
        m.addSeparator()
        m.addAction("Copiar número(s) de revisión",
                    lambda: QGuiApplication.clipboard().setText(", ".join(str(s.revision) for s in sel)))
        m.addAction("Copiar mensaje(s)",
                    lambda: QGuiApplication.clipboard().setText(
                        "\n\n".join(f"r{s.revision} | {s.author}\n{s.message}" for s in sel)))
        m.exec(self.revs.viewport().mapToGlobal(pos))

    def wc_path_list(self):
        return [self.wc_path]

    def path_menu(self, pos):
        it = self.paths.itemAt(pos)
        if not it:
            return
        e, p = it.data(0, Qt.UserRole)
        url = self._url_of(p.path)
        m = QMenu(self)
        if p.action != "D":
            m.addAction("Ver diferencias con la revisión anterior", lambda: self._path_diff(it))
            if p.kind != "dir":
                m.addAction("Abrir esta versión", lambda: self._open_version(url, e.revision))
                m.addAction("Guardar esta versión como…", lambda: self._save_version(url, e.revision))
                if self.actions:
                    m.addAction("Annotate en esta revisión",
                                lambda: self.actions.annotate(url, revision=str(e.revision)))
            m.addAction("Mostrar log de esta ruta",
                        lambda: LogDialog(self.parent(), self.ctx, f"{url}@{e.revision}",
                                          actions=self.actions).show())
        m.addAction("Copiar URL", lambda: QGuiApplication.clipboard().setText(url))
        m.exec(self.paths.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ acciones
    def _path_diff(self, it):
        e, p = it.data(0, Qt.UserRole)
        url = self._url_of(p.path)
        if p.action == "A" and not p.copyfrom_path:
            if p.kind != "dir":
                self._open_version(url, e.revision)
            return
        if p.action == "D":
            return
        if p.copyfrom_path and p.action in ("A", "R"):
            diff_urls(self, self.ctx, f"{self._url_of(p.copyfrom_path)}@{p.copyfrom_rev}",
                      f"{url}@{e.revision}", kind=p.kind or "file")
        else:
            diff_revisions(self, self.ctx, f"{url}@{e.revision}", str(e.revision - 1), str(e.revision),
                           kind=p.kind or "file")

    def _compare_wc(self, rev):
        from .diff import external_diff, DiffDialog
        if self.ctx.config.get("external", "diff_tool"):
            external_diff(self, self.ctx, self.target, str(rev), self.target, None)
        else:
            DiffDialog(self, self.ctx, f"r{rev} ↔ copia de trabajo",
                       lambda c, ws: c.diff([self.target], revision=str(rev), ignore_whitespace=ws)).show()

    def _fetch(self, url, rev, cb):
        run_svn(self, self.ctx, lambda c: c.cat(f"{url}@{rev}"), cb)

    def _open_version(self, url, rev):
        base, ext = os.path.splitext(safe_filename(os.path.basename(url)))

        def done(data):
            open_path(write_private_temp(data, f"{base}.r{rev}{ext}"), self.ctx.config, untrusted=True)
        self._fetch(url, rev, done)

    def _save_version(self, url, rev):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar versión", os.path.join(
            os.path.expanduser("~"), safe_filename(os.path.basename(url))))
        if path:
            self._fetch(url, rev, lambda data: save_bytes(self, path, data))

    def _merge(self, revs, reverse: bool):
        src = self.target_url
        spec = [f"-{r}" if reverse else str(r) for r in sorted(revs, reverse=reverse)]
        txt = "revertir" if reverse else "fusionar"
        if confirm(self, f"¿{txt.capitalize()} las revisiones {', '.join(map(str, revs))} en la copia de trabajo?\n"
                         "Los cambios quedarán pendientes de commit."):
            self.actions.run_merge(self.wc_path, src, spec)

    def _changes(self, sel):
        from .ops import ChangesDialog
        target = self.target_url or self.target
        a = sel[0].revision - 1 if len(sel) == 1 else sel[0].revision
        ChangesDialog(self, self.ctx, f"{target}@{a}", f"{target}@{sel[-1].revision}").show()

    def _edit_revprop(self, e, name):
        url = self.repo_root or self.target_url
        current = e.message if name == "svn:log" else e.author
        if name == "svn:log":
            text, ok = QInputDialog.getMultiLineText(self, "Editar mensaje", f"Mensaje de r{e.revision}:", current)
        else:
            text, ok = QInputDialog.getText(self, "Editar autor", f"Autor de r{e.revision}:", text=current)
        if not ok or text == current:
            return

        def done(_):
            if name == "svn:log":
                e.message = text
            else:
                e.author = text
            for i in range(self.revs.topLevelItemCount()):
                it = self.revs.topLevelItem(i)
                if it.data(0, Qt.UserRole) is e:
                    it.setText(1, e.author)
                    it.setText(3, text.splitlines()[0] if name == "svn:log" and text else it.text(3))
            self.show_selected()

        def err(exc):
            QMessageBox.warning(self, "Propiedad de revisión",
                                "No se pudo cambiar. El repositorio necesita un hook pre-revprop-change "
                                "que lo permita.\n\n" + getattr(exc, "stderr", str(exc)))
        run_svn(self, self.ctx, lambda c: c.propset(name, text, url, revprop=True, revision=str(e.revision)),
                done, err)

    def _revprops(self, e):
        from .properties_dialog import PropertiesDialog
        PropertiesDialog(self, self.ctx, self.repo_root or self.target_url, revision=str(e.revision)).exec()

    # ------------------------------------------------------------------ modo selección
    def picked_revisions(self) -> list:
        return [e.revision for e in self.selected()]
