"""Diálogos de las operaciones SVN menos voluminosas."""
from __future__ import annotations

import os
from typing import Callable, Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QRadioButton, QSpinBox, QStackedWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..svn.client import SvnClient
from .action import ActionDialog
from .common import (ACCEPT_OPTIONS, esc, DEPTHS, FileTable, MessageEdit, PathEdit, RevisionWidget,
                     STATUS_LABELS, UrlCombo, check_all_row, color_for, combo_from, confirm,
                     launch_tool, std_buttons)
from .context import Context, run_svn


def _action(parent, ctx, title, steps, on_done: Optional[Callable] = None, close: Optional[QDialog] = None):
    host = (close.parentWidget() if close else None) or parent

    def ok(_d):
        if on_done:
            on_done()
        if close:
            QDialog.accept(close)
    dlg = ActionDialog(host, ctx, title, steps, ok)
    dlg.setModal(True)
    dlg.start()
    return dlg


def url_row(ctx: Context, combo: UrlCombo, parent) -> QWidget:
    """Combo de URL + botón para elegirla en el navegador del repositorio."""
    w = QWidget()
    l = QHBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    l.addWidget(combo, 1)
    b = QPushButton("…")
    b.setToolTip("Elegir en el navegador del repositorio")
    b.setFixedWidth(32)

    def pick():
        from .browser_dialog import pick_url
        start = combo.text() or (ctx.project.repo_root if ctx.project else "")
        u = pick_url(parent, ctx, start)
        if u:
            combo.setText(u)
    b.clicked.connect(pick)
    l.addWidget(b)
    return w


def pick_revisions(parent, ctx, target) -> list:
    from .log_dialog import LogDialog
    dlg = LogDialog(parent, ctx, target, pick_mode=True)
    if dlg.exec() == QDialog.Accepted:
        return dlg.picked_revisions()
    return []


# ====================================================================== listas de ficheros
class _FileListDialog(QDialog):
    """Base para Añadir / Revertir / Bloquear / Crear parche: lista con casillas."""
    title = ""
    ok_text = "Aceptar"

    def __init__(self, parent, ctx: Context, paths, base_dir: str, on_done=None):
        super().__init__(parent)
        self.ctx, self.paths, self.base_dir, self.on_done = ctx, list(paths), base_dir, on_done
        self.setWindowTitle(self.title)
        self.resize(760, 520)
        self.lay = QVBoxLayout(self)
        self.extra_top()
        self.table = FileTable(base_dir)
        self.lay.addWidget(check_all_row(self.table))
        self.lay.addWidget(self.table)
        self.extra_bottom()
        self.lay.addWidget(std_buttons(self, self.ok_text))
        self.load()

    def extra_top(self):
        pass

    def extra_bottom(self):
        pass

    def filter(self, e) -> bool:
        return e.is_changed

    def load(self):
        def done(entries):
            self.table.set_entries([e for e in entries if self.filter(e)])
            if not self.table.topLevelItemCount():
                self.table.addTopLevelItem(QTreeWidgetItem(["(No hay elementos aplicables)"]))
        run_svn(self, self.ctx, lambda c: self.query(c), done)

    def query(self, c: SvnClient):
        return c.status(self.paths, no_ignore=self.ctx.config.get("general", "show_ignored_files", False))

    def selection(self) -> list:
        return [p for p in self.table.checked_paths() if p]


class AddDialog(_FileListDialog):
    title = "Añadir al control de versiones"
    ok_text = "Añadir"

    def filter(self, e):
        return e.item in ("unversioned", "ignored")

    def accept(self):
        sel = self.selection()
        if sel:
            _action(self, self.ctx, "Añadir", [lambda c: c.cmd_add(sel, no_ignore=True)],
                    self.on_done, self)


class RevertDialog(_FileListDialog):
    title = "Revertir cambios"
    ok_text = "Revertir"

    def filter(self, e):
        return e.is_changed and e.item not in ("unversioned", "ignored", "external")

    def extra_bottom(self):
        self.remove_added = QCheckBox("Eliminar del disco los ficheros que estaban añadidos (--remove-added)")
        self.lay.addWidget(self.remove_added)

    def accept(self):
        sel = self.selection()
        if not sel:
            return
        if self.ctx.config.get("general", "confirm_revert", True) and not confirm(
                self, f"Se descartarán los cambios locales de {len(sel)} elemento(s). ¿Continuar?"):
            return
        rm = self.remove_added.isChecked()
        _action(self, self.ctx, "Revertir", [lambda c: c.cmd_revert(sel, remove_added=rm)], self.on_done, self)


class LockDialog(_FileListDialog):
    title = "Bloquear"
    ok_text = "Bloquear"

    def filter(self, e):
        return e.is_versioned and os.path.isfile(e.path)

    def query(self, c):
        return c.status(self.paths, verbose=True)

    def extra_top(self):
        self.msg = MessageEdit(self.ctx.config, "")
        self.msg.setMaximumHeight(140)
        self.lay.addWidget(self.msg)

    def extra_bottom(self):
        self.steal = QCheckBox("Robar el bloqueo si lo tiene otro usuario (--force)")
        self.lay.addWidget(self.steal)

    def load(self):
        def done(entries):
            self.table.set_entries([e for e in entries if self.filter(e)],
                                   checked=lambda e: e.path in self.paths or len(self.paths) == 1)
        run_svn(self, self.ctx, self.query, done)

    def accept(self):
        sel = self.selection()
        if sel:
            msg, force = self.msg.text(), self.steal.isChecked()
            _action(self, self.ctx, "Bloquear", [lambda c: c.cmd_lock(sel, msg, force)], self.on_done, self)


class UnlockDialog(_FileListDialog):
    title = "Desbloquear"
    ok_text = "Desbloquear"

    def filter(self, e):
        return e.lock is not None

    def query(self, c):
        return c.status(self.paths, verbose=True)

    def extra_bottom(self):
        self.force = QCheckBox("Romper bloqueos de otros usuarios (--force)")
        self.lay.addWidget(self.force)

    def accept(self):
        sel = self.selection()
        if sel:
            f = self.force.isChecked()
            _action(self, self.ctx, "Desbloquear", [lambda c: c.cmd_unlock(sel, f)], self.on_done, self)


class CreatePatchDialog(_FileListDialog):
    title = "Crear parche"
    ok_text = "Guardar parche"

    def filter(self, e):
        return e.is_changed and e.is_versioned

    def extra_bottom(self):
        f = QFormLayout()
        self.out = PathEdit("save", os.path.join(self.base_dir, "cambios.patch"), filter_="Parches (*.patch *.diff)")
        f.addRow("Fichero:", self.out)
        self.git = QCheckBox("Formato git (svn diff --git)")
        f.addRow("", self.git)
        self.lay.addLayout(f)

    def accept(self):
        sel, out, git = self.selection(), self.out.text(), self.git.isChecked()
        if not sel or not out:
            return

        def done(text):
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            QMessageBox.information(self, "Parche", f"Parche guardado en\n{out}")
            QDialog.accept(self)
        run_svn(self, self.ctx, lambda c: c.diff(sel, cwd=self.base_dir, git_format=git,
                                                 depth="empty"), done)


# ====================================================================== diálogos simples
class CleanupDialog(QDialog):
    def __init__(self, parent, ctx, path, on_done=None):
        super().__init__(parent)
        self.ctx, self.path, self.on_done = ctx, path, on_done
        self.setWindowTitle("Limpiar (cleanup)")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{esc(path)}</b>"))
        self.unv = QCheckBox("Eliminar ficheros sin versionar")
        self.ign = QCheckBox("Eliminar ficheros ignorados")
        self.vac = QCheckBox("Eliminar copias prístinas no referenciadas (vacuum)")
        self.ext = QCheckBox("Incluir externals")
        self.upg = QCheckBox("Actualizar el formato de la working copy (svn upgrade)")
        for w in (self.unv, self.ign, self.vac, self.ext, self.upg):
            lay.addWidget(w)
        lay.addWidget(std_buttons(self, "Limpiar"))

    def accept(self):
        if (self.unv.isChecked() or self.ign.isChecked()) and not confirm(
                self, "Se borrarán ficheros del disco de forma permanente. ¿Continuar?"):
            return
        a = (self.unv.isChecked(), self.ign.isChecked(), self.vac.isChecked(), self.ext.isChecked())
        steps = []
        if self.upg.isChecked():
            steps.append(lambda c: c.cmd_upgrade(self.path))
        steps.append(lambda c: c.cmd_cleanup(self.path, *a))
        _action(self, self.ctx, "Limpiar", steps, self.on_done, self)


class ResolveDialog(_FileListDialog):
    title = "Marcar como resuelto"
    ok_text = "Resolver"

    def filter(self, e):
        return e.is_conflicted

    def extra_bottom(self):
        f = QFormLayout()
        self.accept_opt = combo_from([o for o in ACCEPT_OPTIONS if o[0] != "postpone"], "working")
        f.addRow("Resolver usando:", self.accept_opt)
        self.lay.addLayout(f)

    def accept(self):
        sel = self.selection()
        if sel:
            acc = self.accept_opt.currentData()
            _action(self, self.ctx, "Resolver", [lambda c: c.cmd_resolve(sel, acc)], self.on_done, self)


class UpdateToRevisionDialog(QDialog):
    def __init__(self, parent, ctx, paths, on_done=None, revision: Optional[str] = None):
        super().__init__(parent)
        self.ctx, self.paths, self.on_done = ctx, list(paths), on_done
        self.setWindowTitle("Actualizar a revisión")
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.rev = RevisionWidget(show_log_cb=self._log)
        if revision:
            self.rev.set_revision(revision)
        f.addRow("Revisión:", self.rev)
        self.depth = combo_from([("", "Profundidad actual de la working copy")] + DEPTHS, "")
        f.addRow("Profundidad:", self.depth)
        self.sticky = QCheckBox("Hacer persistente la profundidad (--set-depth)")
        f.addRow("", self.sticky)
        self.ign = QCheckBox("Omitir externals")
        f.addRow("", self.ign)
        self.acc = combo_from(ACCEPT_OPTIONS, "postpone")
        f.addRow("Si hay conflictos:", self.acc)
        lay.addLayout(f)
        lay.addWidget(std_buttons(self, "Actualizar"))

    def _log(self):
        revs = pick_revisions(self, self.ctx, self.paths[0])
        if revs:
            self.rev.set_revision(revs[-1])

    def accept(self):
        rev = self.rev.value()
        d = self.depth.currentData() or None
        sticky = self.sticky.isChecked()
        ign, acc = self.ign.isChecked(), self.acc.currentData()
        _action(self, self.ctx, "Actualizar", [lambda c: c.cmd_update(
            self.paths, rev, depth=None if sticky else d, set_depth=d if sticky else None,
            ignore_externals=ign, accept=acc)], self.on_done, self)


class BranchDialog(QDialog):
    """Rama / etiqueta (svn copy)."""

    def __init__(self, parent, ctx, source: str, revision: Optional[str] = None, on_done=None):
        super().__init__(parent)
        self.ctx, self.source, self.on_done = ctx, source, on_done
        self.is_wc = "://" not in source
        self.setWindowTitle("Crear rama / etiqueta")
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        f.addRow("Desde:", QLabel(f"<b>{esc(source)}</b>"))
        self.rev = RevisionWidget(allow_working=self.is_wc, show_log_cb=self._log)
        if revision:
            self.rev.set_revision(revision)
        elif self.is_wc:
            self.rev.working.setChecked(True)
        f.addRow("Revisión de origen:", self.rev)
        root = ctx.project.repo_root if ctx.project else ""
        self.dest = UrlCombo(ctx.config, (root + "/branches/") if root else "")
        f.addRow("A la URL:", url_row(ctx, self.dest, self))
        self.switch = QCheckBox("Cambiar la working copy a la nueva rama (switch)")
        self.switch.setChecked(ctx.config.get("general", "switch_after_branch", True))
        self.switch.setEnabled(self.is_wc)
        if not self.is_wc:
            self.switch.setChecked(False)
        f.addRow("", self.switch)
        self.pin = QCheckBox("Fijar revisiones de los externals (--pin-externals)")
        f.addRow("", self.pin)
        lay.addLayout(f)
        self.msg = MessageEdit(ctx.config)
        lay.addWidget(self.msg)
        lay.addWidget(std_buttons(self, "Crear"))

    def _log(self):
        revs = pick_revisions(self, self.ctx, self.source)
        if revs:
            self.rev.set_revision(revs[-1])

    def accept(self):
        dest = self.dest.text()
        if not dest or dest.endswith(("/branches", "/tags")):
            QMessageBox.warning(self, "Rama", "Indica la URL completa de destino (incluido el nombre de la rama).")
            return
        rev = self.rev.value()
        msg = self.msg.text() or f"Crear {dest.rsplit('/', 1)[-1]}"
        pin = self.pin.isChecked()
        steps = [lambda c: c.cmd_copy(self.source, dest, msg, rev, pin_externals=pin)]
        if self.switch.isChecked():
            steps.append(lambda c: c.cmd_switch(dest, self.source))
        self.ctx.config.remember_url(dest)
        self.ctx.config.remember_message(msg)
        _action(self, self.ctx, "Crear rama / etiqueta", steps, self.on_done, self)


class SwitchDialog(QDialog):
    def __init__(self, parent, ctx, path, current_url: str = "", on_done=None):
        super().__init__(parent)
        self.ctx, self.path, self.on_done = ctx, path, on_done
        self.setWindowTitle("Cambiar (switch)")
        self.resize(700, 300)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        f.addRow("Copia de trabajo:", QLabel(f"<b>{esc(path)}</b>"))
        self.url = UrlCombo(ctx.config, current_url)
        f.addRow("Cambiar a la URL:", url_row(ctx, self.url, self))
        self.rev = RevisionWidget()
        f.addRow("Revisión:", self.rev)
        self.depth = combo_from([("", "Profundidad actual")] + DEPTHS, "")
        f.addRow("Profundidad:", self.depth)
        self.anc = QCheckBox("Ignorar ancestros (--ignore-ancestry)")
        self.ign = QCheckBox("Omitir externals")
        f.addRow("", self.anc)
        f.addRow("", self.ign)
        self.acc = combo_from(ACCEPT_OPTIONS, "postpone")
        f.addRow("Si hay conflictos:", self.acc)
        lay.addLayout(f)
        lay.addWidget(std_buttons(self, "Cambiar"))

    def accept(self):
        url = self.url.text()
        if not url:
            return
        rev = self.rev.value()
        a = (self.depth.currentData() or None, self.anc.isChecked(), self.ign.isChecked(), self.acc.currentData())
        self.ctx.config.remember_url(url)
        _action(self, self.ctx, "Switch", [lambda c: c.cmd_switch(url, self.path, None if rev == "HEAD" else rev, *a)],
                self.on_done, self)


class MergeDialog(QDialog):
    def __init__(self, parent, ctx, path, on_done=None):
        super().__init__(parent)
        self.ctx, self.path, self.on_done = ctx, path, on_done
        self.setWindowTitle("Fusionar (merge)")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>Destino:</b> {esc(path)}"))
        modes = QGroupBox("Tipo de fusión")
        ml = QVBoxLayout(modes)
        self.m_range = QRadioButton("Fusionar un rango de revisiones (cherry-pick)")
        self.m_auto = QRadioButton("Fusión automática: sincronizar con otra rama o reintegrarla")
        self.m_tree = QRadioButton("Fusionar las diferencias entre dos árboles")
        grp = QButtonGroup(self)
        for i, r in enumerate((self.m_range, self.m_auto, self.m_tree)):
            grp.addButton(r, i)
            ml.addWidget(r)
        lay.addWidget(modes)
        self.stack = QStackedWidget()
        # rango
        w1 = QWidget()
        f1 = QFormLayout(w1)
        self.src = UrlCombo(ctx.config)
        f1.addRow("URL origen:", url_row(ctx, self.src, self))
        rr = QHBoxLayout()
        self.revs = QLineEdit()
        self.revs.setPlaceholderText("p. ej. 12, 15-18, -20 (negativo = revertir)")
        rr.addWidget(self.revs, 1)
        pick = QPushButton("Elegir del log…")
        pick.clicked.connect(self._pick)
        rr.addWidget(pick)
        f1.addRow("Revisiones:", rr)
        self.stack.addWidget(w1)
        # automática
        w2 = QWidget()
        f2 = QFormLayout(w2)
        self.src_auto = UrlCombo(ctx.config)
        f2.addRow("URL origen:", url_row(ctx, self.src_auto, self))
        f2.addRow(QLabel("Fusiona todos los cambios aún no fusionados (svn merge URL WC)."))
        self.stack.addWidget(w2)
        # dos árboles
        w3 = QWidget()
        f3 = QFormLayout(w3)
        self.from_url = UrlCombo(ctx.config)
        self.from_rev = RevisionWidget()
        self.to_url = UrlCombo(ctx.config)
        self.to_rev = RevisionWidget()
        f3.addRow("Desde URL:", url_row(ctx, self.from_url, self))
        f3.addRow("Revisión:", self.from_rev)
        f3.addRow("Hasta URL:", url_row(ctx, self.to_url, self))
        f3.addRow("Revisión:", self.to_rev)
        self.stack.addWidget(w3)
        lay.addWidget(self.stack)
        grp.idToggled.connect(lambda i, on: on and self.stack.setCurrentIndex(i))
        self.m_range.setChecked(True)
        opts = QGroupBox("Opciones")
        of = QFormLayout(opts)
        self.depth = combo_from([("", "Profundidad de la working copy")] + DEPTHS, "")
        of.addRow("Profundidad:", self.depth)
        self.acc = combo_from(ACCEPT_OPTIONS, "postpone")
        of.addRow("Si hay conflictos:", self.acc)
        self.anc = QCheckBox("Ignorar ancestros")
        self.rec = QCheckBox("Solo registrar la fusión (--record-only)")
        self.mix = QCheckBox("Permitir revisiones mezcladas en la working copy")
        for w in (self.anc, self.rec, self.mix):
            of.addRow("", w)
        lay.addWidget(opts)
        row = QHBoxLayout()
        row.addStretch()
        test = QPushButton("Probar (dry-run)")
        test.clicked.connect(lambda: self._run(True))
        row.addWidget(test)
        ok = QPushButton("Fusionar")
        ok.setDefault(True)
        ok.clicked.connect(lambda: self._run(False))
        row.addWidget(ok)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _pick(self):
        src = self.src.text()
        if not src:
            QMessageBox.information(self, "Merge", "Indica primero la URL origen.")
            return
        revs = pick_revisions(self, self.ctx, src)
        if revs:
            self.revs.setText(", ".join(str(r) for r in revs))

    def _run(self, dry: bool):
        opts = dict(dry_run=dry, record_only=self.rec.isChecked(), ignore_ancestry=self.anc.isChecked(),
                    accept=self.acc.currentData(), depth=self.depth.currentData() or None,
                    allow_mixed=self.mix.isChecked())
        if self.m_range.isChecked():
            src, revs = self.src.text(), [r for r in self.revs.text().replace(" ", "").split(",") if r]
            if not src or not revs:
                QMessageBox.warning(self, "Merge", "Indica URL y revisiones.")
                return
            step = lambda c: c.cmd_merge(self.path, src, revs, **opts)  # noqa: E731
            self.ctx.config.remember_url(src)
        elif self.m_auto.isChecked():
            src = self.src_auto.text()
            if not src:
                return
            step = lambda c: c.cmd_merge(self.path, src, **opts)  # noqa: E731
            self.ctx.config.remember_url(src)
        else:
            a = f"{self.from_url.text()}@{self.from_rev.value()}"
            b = f"{self.to_url.text()}@{self.to_rev.value()}"
            step = lambda c: c.cmd_merge(self.path, a, source2=b, **opts)  # noqa: E731
        title = "Merge (prueba)" if dry else "Merge"
        _action(self, self.ctx, title, [step], None if dry else self.on_done, None if dry else self)


class ImportDialog(QDialog):
    def __init__(self, parent, ctx, path: str = "", on_done=None):
        super().__init__(parent)
        self.ctx, self.on_done = ctx, on_done
        self.setWindowTitle("Importar")
        self.resize(700, 460)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.path = PathEdit("dir", path)
        f.addRow("Carpeta local:", self.path)
        self.url = UrlCombo(ctx.config, ctx.project.repo_root + "/" if ctx.project and ctx.project.repo_root else "")
        f.addRow("URL destino:", url_row(ctx, self.url, self))
        self.noign = QCheckBox("Incluir ficheros ignorados (--no-ignore)")
        f.addRow("", self.noign)
        lay.addLayout(f)
        self.msg = MessageEdit(ctx.config)
        lay.addWidget(self.msg)
        lay.addWidget(std_buttons(self, "Importar"))

    def accept(self):
        p, u, m = self.path.text(), self.url.text(), self.msg.text()
        if not p or not u:
            return
        ni = self.noign.isChecked()
        self.ctx.config.remember_url(u)
        _action(self, self.ctx, "Importar", [lambda c: c.cmd_import(p, u, m or "Importación inicial", ni)],
                self.on_done, self)


class ExportDialog(QDialog):
    def __init__(self, parent, ctx, source: str, revision: Optional[str] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Exportar")
        self.resize(720, 330)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.src = UrlCombo(ctx.config, source)
        f.addRow("Origen (URL o ruta):", self.src)
        name = unquote(source.rstrip("/").rsplit("/", 1)[-1].split("@")[0]) or "export"
        self.dest = PathEdit("dir", os.path.join(os.path.expanduser("~"), name + "-export"))
        f.addRow("Destino:", self.dest)
        self.rev = RevisionWidget(allow_working="://" not in source)
        if revision:
            self.rev.set_revision(revision)
        elif "://" not in source:
            self.rev.working.setChecked(True)
        f.addRow("Revisión:", self.rev)
        self.depth = combo_from(DEPTHS, "infinity")
        f.addRow("Profundidad:", self.depth)
        self.eol = combo_from([("", "Sin cambios"), ("LF", "LF (Unix)"), ("CRLF", "CRLF (Windows)"), ("CR", "CR")], "")
        f.addRow("Fin de línea nativo:", self.eol)
        self.force = QCheckBox("Sobrescribir si existe (--force)")
        self.ign = QCheckBox("Omitir externals")
        f.addRow("", self.force)
        f.addRow("", self.ign)
        lay.addLayout(f)
        lay.addWidget(std_buttons(self, "Exportar"))

    def accept(self):
        s, d = self.src.currentText().strip(), self.dest.text()
        if not s or not d:
            return
        if os.path.exists(d) and self.force.isChecked() and not confirm(
                self, f"«{d}» ya existe y se sobrescribirán sus ficheros. ¿Continuar?"):
            return
        rev = self.rev.value()
        a = (self.force.isChecked(), self.ign.isChecked(), self.eol.currentData() or None, self.depth.currentData())
        _action(self, self.ctx, "Exportar", [lambda c: c.cmd_export(s, d, rev, *a)], None, self)


class RelocateDialog(QDialog):
    def __init__(self, parent, ctx, path, repo_root: str, on_done=None):
        super().__init__(parent)
        self.ctx, self.path, self.on_done, self.root = ctx, path, on_done, repo_root
        self.setWindowTitle("Reubicar (relocate)")
        self.resize(640, 200)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        f.addRow("Copia de trabajo:", QLabel(f"<b>{esc(path)}</b>"))
        self.frm = QLineEdit(repo_root)
        f.addRow("Desde:", self.frm)
        self.to = QLineEdit(repo_root)
        f.addRow("A:", self.to)
        lay.addLayout(f)
        lay.addWidget(QLabel("Úsalo solo si el servidor ha cambiado de dirección (mismo repositorio)."))
        lay.addWidget(std_buttons(self, "Reubicar"))

    def accept(self):
        frm, to = self.frm.text().strip(), self.to.text().strip()
        if not to or to == frm:
            return
        _action(self, self.ctx, "Reubicar", [lambda c: c.cmd_relocate(self.path, to, frm)], self.on_done, self)


class ApplyPatchDialog(QDialog):
    def __init__(self, parent, ctx, wc_path, on_done=None):
        super().__init__(parent)
        self.ctx, self.on_done = ctx, on_done
        self.setWindowTitle("Aplicar parche")
        self.resize(640, 240)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.patch = PathEdit("file", "", filter_="Parches (*.patch *.diff);;Todos (*)")
        f.addRow("Fichero de parche:", self.patch)
        self.wc = PathEdit("dir", wc_path)
        f.addRow("Aplicar en:", self.wc)
        self.strip = QSpinBox()
        self.strip.setRange(0, 20)
        f.addRow("Eliminar componentes de ruta (--strip):", self.strip)
        self.rev = QCheckBox("Aplicar en sentido inverso")
        f.addRow("", self.rev)
        lay.addLayout(f)
        row = QHBoxLayout()
        row.addStretch()
        t = QPushButton("Probar (dry-run)")
        t.clicked.connect(lambda: self._run(True))
        row.addWidget(t)
        ok = QPushButton("Aplicar")
        ok.clicked.connect(lambda: self._run(False))
        row.addWidget(ok)
        c = QPushButton("Cancelar")
        c.clicked.connect(self.reject)
        row.addWidget(c)
        lay.addLayout(row)

    def _run(self, dry):
        p, w = self.patch.text(), self.wc.text()
        if not os.path.isfile(p):
            QMessageBox.warning(self, "Parche", "Selecciona un fichero de parche.")
            return
        a = (dry, self.rev.isChecked(), self.strip.value())
        _action(self, self.ctx, "Aplicar parche" + (" (prueba)" if dry else ""),
                [lambda c: c.cmd_patch(p, w, *a)], None if dry else self.on_done, None if dry else self)


class CreateRepoDialog(QDialog):
    def __init__(self, parent, ctx, on_done=None):
        super().__init__(parent)
        self.ctx, self.on_done = ctx, on_done
        self.created_url = ""
        self.setWindowTitle("Crear repositorio local")
        self.resize(600, 220)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.path = PathEdit("dir", os.path.expanduser("~/svn-repos/nuevo"))
        f.addRow("Carpeta del repositorio:", self.path)
        self.fs = combo_from([("fsfs", "FSFS (recomendado)"), ("bdb", "Berkeley DB")], "fsfs")
        f.addRow("Tipo:", self.fs)
        self.layout_cb = QCheckBox("Crear estructura trunk / branches / tags")
        self.layout_cb.setChecked(True)
        f.addRow("", self.layout_cb)
        lay.addLayout(f)
        lay.addWidget(std_buttons(self, "Crear"))

    def accept(self):
        path = self.path.text()
        if not path:
            return
        if os.path.exists(path) and os.listdir(path):
            QMessageBox.warning(self, "Repositorio", "La carpeta existe y no está vacía.")
            return
        url = "file://" + quote(os.path.abspath(path))
        fs = self.fs.currentData()
        steps = [lambda c: c.cmd_create_repo(path, fs)]
        if self.layout_cb.isChecked():
            steps.append(lambda c: c.cmd_mkdir([url + "/trunk", url + "/branches", url + "/tags"],
                                               "Estructura inicial"))
        self.created_url = url
        self.ctx.config.remember_url(url)

        def done():
            QMessageBox.information(self.parentWidget(), "Repositorio",
                                    f"Repositorio creado:\n{url}\n\nPuedes hacer checkout desde "
                                    "«Proyecto → Añadir proyecto».")
            if self.on_done:
                self.on_done(url)
        _action(self, self.ctx, "Crear repositorio", steps, done, self)


class ChangesDialog(QDialog):
    """Compara dos URL@rev y lista los cambios (diff --summarize)."""

    def __init__(self, parent, ctx, left: str = "", right: str = ""):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Cambios entre revisiones / ramas")
        self.resize(950, 560)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.left = UrlCombo(ctx.config, left)
        self.right = UrlCombo(ctx.config, right)
        f.addRow("Izquierda (URL[@rev]):", url_row(ctx, self.left, self))
        f.addRow("Derecha (URL[@rev]):", url_row(ctx, self.right, self))
        lay.addLayout(f)
        row = QHBoxLayout()
        row.addStretch()
        b = QPushButton("Comparar")
        b.clicked.connect(self.compare)
        row.addWidget(b)
        u = QPushButton("Diff unificado completo")
        u.clicked.connect(self.full_diff)
        row.addWidget(u)
        lay.addLayout(row)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Ruta", "Cambio", "Propiedades", "Tipo"])
        self.table.setRootIsDecorated(False)
        self.table.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(self._diff_item)
        lay.addWidget(self.table)
        self.status = QLabel("")
        lay.addWidget(self.status)
        if left and right:
            self.compare()

    @staticmethod
    def _split(t):
        last = t.rsplit("/", 1)[-1]
        if "@" in last:
            base, rev = t.rsplit("@", 1)
            return base, rev
        return t, None

    def compare(self):
        l, r = self.left.text(), self.right.text()
        if not l or not r:
            return
        self.status.setText("Comparando…")
        lb, _ = self._split(l)

        def done(entries):
            self.table.clear()
            for e in entries:
                rel = unquote(e.path[len(lb):].lstrip("/")) if e.path.startswith(lb) else unquote(e.path)
                it = QTreeWidgetItem([rel or ".", STATUS_LABELS.get(e.item, e.item),
                                      STATUS_LABELS.get(e.props, "") if e.props not in ("none", "normal") else "",
                                      "carpeta" if e.kind == "dir" else "fichero"])
                col = color_for(e.item)
                if col:
                    it.setForeground(1, col)
                it.setData(0, Qt.UserRole, (rel, e))
                self.table.addTopLevelItem(it)
            self.status.setText(f"{len(entries)} elementos cambiados")
        run_svn(self, self.ctx, lambda c: c.diff_summarize(l, r), done)

    def _diff_item(self, it, _c):
        rel, e = it.data(0, Qt.UserRole)
        if e.kind == "dir":
            return
        from .diff import diff_urls
        (lb, lr), (rb, rr) = self._split(self.left.text()), self._split(self.right.text())
        lu = f"{lb}/{quote(rel)}" + (f"@{lr}" if lr else "")
        ru = f"{rb}/{quote(rel)}" + (f"@{rr}" if rr else "")
        diff_urls(self, self.ctx, lu, ru)

    def full_diff(self):
        from .diff import DiffDialog
        l, r = self.left.text(), self.right.text()
        if l and r:
            DiffDialog(self, self.ctx, f"{l} ↔ {r}", lambda c, ws: c.diff(old=l, new=r, ignore_whitespace=ws)).show()


# ====================================================================== funciones sueltas
def add_to_ignore(parent, ctx: Context, paths, by_extension: bool, on_done=None, recursive: bool = False):
    """Añade el nombre (o *.ext) a svn:ignore (o svn:global-ignores) de la carpeta padre."""
    groups: dict = {}
    for p in paths:
        name = os.path.basename(p.rstrip("/"))
        pattern = "*" + os.path.splitext(name)[1] if by_extension and os.path.splitext(name)[1] else name
        groups.setdefault(os.path.dirname(p), set()).add(pattern)
    prop = "svn:global-ignores" if recursive else "svn:ignore"

    def work(c: SvnClient):
        for parent_dir, patterns in groups.items():
            cur = c.propget(prop, parent_dir)
            lines = [l for l in cur.splitlines() if l.strip()]
            for pat in sorted(patterns):
                if pat not in lines:
                    lines.append(pat)
            c.propset(prop, "\n".join(lines) + "\n", parent_dir)
        return True
    run_svn(parent, ctx, work, lambda _: on_done and on_done())


def rename(parent, ctx: Context, path: str, on_done=None):
    from PySide6.QtWidgets import QInputDialog
    name, ok = QInputDialog.getText(parent, "Renombrar", "Nuevo nombre:", text=os.path.basename(path))
    if not ok or not name.strip() or name.strip() == os.path.basename(path):
        return
    dst = os.path.join(os.path.dirname(path), name.strip()) if "/" not in name else name.strip()
    _action(parent, ctx, "Renombrar", [lambda c: c.cmd_move(path, dst)], on_done)


def delete(parent, ctx: Context, paths, on_done=None):
    box = QMessageBox(QMessageBox.Warning, "Eliminar",
                      f"¿Eliminar {len(paths)} elemento(s) del control de versiones?\n\n" +
                      "\n".join(os.path.basename(p) for p in paths[:15]) + ("\n…" if len(paths) > 15 else ""),
                      QMessageBox.Yes | QMessageBox.No, parent)
    keep = QCheckBox("Conservar los ficheros en disco (--keep-local)")
    box.setCheckBox(keep)
    if box.exec() != QMessageBox.Yes:
        return
    k = keep.isChecked()
    _action(parent, ctx, "Eliminar", [lambda c: c.cmd_delete(paths, keep_local=k, force=True)], on_done)


def edit_conflicts(parent, ctx: Context, path: str, on_done=None):
    tool = ctx.config.get("external", "merge_tool") or ctx.config.get("external", "diff_tool")
    if not tool:
        QMessageBox.information(parent, "Conflictos", "Configura una herramienta de fusión en Ajustes.")
        return

    def done(infos):
        files = infos[0].conflict_files if infos else {}
        base, mine, theirs = files.get("prev-base-file"), files.get("prev-wc-file"), files.get("cur-base-file")
        if not (mine and theirs):
            QMessageBox.information(parent, "Conflictos", "No hay un conflicto de texto en este fichero "
                                                          "(puede ser de árbol o de propiedades).")
            return
        name = os.path.basename(tool).lower()
        if "meld" in name:
            argv = [tool, mine, path, theirs]
        elif "kdiff3" in name:
            argv = [tool, base or mine, mine, theirs, "-o", path]
        else:
            argv = [tool, base or mine, mine, theirs, path]
        if launch_tool(argv) and confirm(parent, "Cuando termines de editar el fichero, ¿marcarlo como resuelto?",
                                         "Editar conflictos"):
            _action(parent, ctx, "Resolver", [lambda c: c.cmd_resolve([path], "working")], on_done)
    run_svn(parent, ctx, lambda c: c.info(path), done)
