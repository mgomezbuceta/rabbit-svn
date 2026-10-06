"""Acciones SVN invocables desde menús, barra de herramientas y otros diálogos."""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtWidgets import QMenu, QMessageBox

from . import ops
from .action import ActionDialog
from .context import Context


class Actions:
    def __init__(self, window):
        self.w = window

    # ---------------------------------------------------------------- helpers
    @property
    def ctx(self) -> Context:
        return self.w.ctx

    @property
    def base(self) -> str:
        return self.ctx.project.wc_path if self.ctx.project else ""

    def _refresh(self, on_done: Optional[Callable] = None):
        def cb(*_):
            if on_done:
                on_done()
            self.w.refresh()
        return cb

    def _need_project(self) -> bool:
        if not self.ctx.project:
            QMessageBox.information(self.w, "Sin proyecto", "Selecciona o añade primero un proyecto.")
            return False
        return True

    def _run(self, title, steps, on_done=None):
        dlg = ActionDialog(self.w, self.ctx, title, steps, self._refresh(on_done))
        dlg.setModal(True)
        dlg.start()

    # ---------------------------------------------------------------- working copy
    def update(self, paths, on_done=None):
        if self._need_project():
            self._run("Actualizar", [lambda c: c.cmd_update(paths)], on_done)

    def update_to(self, paths, revision: Optional[str] = None):
        if self._need_project():
            ops.UpdateToRevisionDialog(self.w, self.ctx, paths, self._refresh(), revision).exec()

    def commit(self, paths):
        if self._need_project():
            from .commit_dialog import CommitDialog
            CommitDialog(self.w, self.ctx, paths, self.base, self._refresh()).exec()

    def checkmods(self, paths):
        if self._need_project():
            from .checkmods_dialog import CheckModsDialog
            CheckModsDialog(self.w, self.ctx, paths, self.base, self).show()

    def add(self, paths):
        ops.AddDialog(self.w, self.ctx, paths, self.base, self._refresh()).exec()

    def delete(self, paths):
        ops.delete(self.w, self.ctx, paths, self._refresh())

    def revert(self, paths, on_done=None):
        ops.RevertDialog(self.w, self.ctx, paths, self.base, self._refresh(on_done)).exec()

    def rename(self, path):
        ops.rename(self.w, self.ctx, path, self._refresh())

    def cleanup(self, path):
        ops.CleanupDialog(self.w, self.ctx, path, self._refresh()).exec()

    def resolve(self, paths):
        ops.ResolveDialog(self.w, self.ctx, paths, self.base, self._refresh()).exec()

    def edit_conflicts(self, path):
        ops.edit_conflicts(self.w, self.ctx, path, self._refresh())

    def lock(self, paths):
        ops.LockDialog(self.w, self.ctx, paths, self.base, self._refresh()).exec()

    def unlock(self, paths):
        ops.UnlockDialog(self.w, self.ctx, paths, self.base, self._refresh()).exec()

    def ignore(self, paths, by_extension=False, recursive=False):
        ops.add_to_ignore(self.w, self.ctx, paths, by_extension, self._refresh(), recursive)

    def changelist(self, paths, name: Optional[str]):
        self._run("Changelist", [lambda c: c.cmd_changelist(paths, name)])

    def ask_changelist(self, paths):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self.w, "Changelist", "Nombre de la lista de cambios:")
        if ok and name.strip():
            self.changelist(paths, name.strip())

    # ---------------------------------------------------------------- consultas
    def diff(self, paths):
        from .diff import diff_working
        diff_working(self.w, self.ctx, paths, self.base)

    def diff_previous(self, path):
        """Compara la última revisión en la que cambió el fichero con la anterior."""
        from .context import run_svn
        from .diff import diff_revisions

        def done(infos):
            i = infos[0]
            if not i.last_changed_rev or i.last_changed_rev <= 1:
                QMessageBox.information(self.w, "Diff", "No hay revisión anterior.")
                return
            diff_revisions(self.w, self.ctx, f"{i.url}@{i.last_changed_rev}", str(i.last_changed_rev - 1),
                           str(i.last_changed_rev), kind=i.kind)
        run_svn(self.w, self.ctx, lambda c: c.info(path), done)

    def compare_head(self, path):
        from .diff import DiffDialog, external_diff
        if self.ctx.config.get("external", "diff_tool") and os.path.isfile(path):
            external_diff(self.w, self.ctx, path, "HEAD", path, None)
        else:
            DiffDialog(self.w, self.ctx, f"HEAD ↔ copia de trabajo: {os.path.basename(path)}",
                       lambda c, ws: c.diff([path], revision="HEAD", ignore_whitespace=ws)).show()

    def log(self, target, revision=None):
        from .log_dialog import LogDialog
        wc = target if "://" not in target else None
        LogDialog(self.w, self.ctx, target, wc_path=wc, actions=self).show()

    def annotate(self, target, revision=None):
        from .annotate_dialog import AnnotateDialog
        AnnotateDialog(self.w, self.ctx, target, revision, self).show()

    def properties(self, path):
        from .properties_dialog import PropertiesDialog
        if PropertiesDialog(self.w, self.ctx, path).exec():
            self.w.refresh()

    def browser(self, url=None, revision=None):
        from .browser_dialog import RepoBrowser
        if url and "://" not in url:
            from .context import run_svn
            run_svn(self.w, self.ctx, lambda c: c.info(url),
                    lambda infos: RepoBrowser(self.w, self.ctx, infos[0].url, revision, self).show())
            return
        RepoBrowser(self.w, self.ctx, url or (self.ctx.project.url if self.ctx.project else ""),
                    revision, self).show()

    def changes(self, left="", right=""):
        if not left and self.ctx.project:
            left = right = self.ctx.project.url
        ops.ChangesDialog(self.w, self.ctx, left, right).show()

    # ---------------------------------------------------------------- ramas y repositorio
    def branch(self, source, revision=None):
        ops.BranchDialog(self.w, self.ctx, source, revision, self._refresh(self.w.reload_project_info)).exec()

    def switch(self, path):
        url = self.ctx.project.url if self.ctx.project else ""
        ops.SwitchDialog(self.w, self.ctx, path, url, self._refresh(self.w.reload_project_info)).exec()

    def merge(self, path):
        ops.MergeDialog(self.w, self.ctx, path, self._refresh()).exec()

    def run_merge(self, wc, src, spec):
        self._run("Merge", [lambda c: c.cmd_merge(wc, src, spec)])

    def export(self, source, revision=None):
        ops.ExportDialog(self.w, self.ctx, source, revision).exec()

    def import_(self, path=""):
        ops.ImportDialog(self.w, self.ctx, path).exec()

    def relocate(self, path):
        if self._need_project():
            ops.RelocateDialog(self.w, self.ctx, path, self.ctx.project.repo_root,
                               self._refresh(self.w.reload_project_info)).exec()

    def create_patch(self, paths):
        ops.CreatePatchDialog(self.w, self.ctx, paths, self.base).exec()

    def apply_patch(self, path):
        ops.ApplyPatchDialog(self.w, self.ctx, path, self._refresh()).exec()

    def create_repo(self):
        ops.CreateRepoDialog(self.w, self.ctx.with_project(None)).exec()

    def checkout(self, url=""):
        self.w.add_project(checkout_url=url)

    # ---------------------------------------------------------------- menú contextual
    def build_menu(self, menu: QMenu, paths, entries: dict):
        """Menú tipo RabbitVCS. `entries` = {ruta: StatusEntry|None}."""
        es = [entries.get(p) for p in paths]
        single = len(paths) == 1
        p0 = paths[0]
        is_dir = single and os.path.isdir(p0)
        versioned = all(e is not None and e.is_versioned for e in es)
        any_unversioned = any(e is None or e.item in ("unversioned", "ignored") for e in es)
        changed = any(e is not None and e.is_changed and e.item not in ("unversioned", "ignored") for e in es)
        conflicted = any(e is not None and e.is_conflicted for e in es)
        locked = any(e is not None and e.lock for e in es)
        is_file = single and os.path.isfile(p0)

        def add(text, fn, enabled=True, m=menu):
            a = m.addAction(text, fn)
            a.setEnabled(enabled)
            return a

        add("Actualizar", lambda: self.update(paths), versioned)
        add("Commit…", lambda: self.commit(paths), versioned or changed)
        add("Comprobar modificaciones…", lambda: self.checkmods(paths), versioned)
        menu.addSeparator()
        if any_unversioned:
            add("Añadir…", lambda: self.add(paths))
            ig = menu.addMenu("Ignorar")
            names = sorted({os.path.basename(p) for p in paths})
            add(f"Por nombre ({', '.join(names)[:40]})", lambda: self.ignore(paths), m=ig)
            exts = sorted({os.path.splitext(p)[1] for p in paths if os.path.splitext(p)[1]})
            if exts:
                add(f"Por extensión ({', '.join('*' + e for e in exts)})",
                    lambda: self.ignore(paths, by_extension=True), m=ig)
            add("Por nombre en todas las subcarpetas (svn:global-ignores)",
                lambda: self.ignore(paths, recursive=True), m=ig)
        add("Ver diferencias", lambda: self.diff(paths), changed)
        if single and versioned:
            add("Comparar con la revisión anterior", lambda: self.diff_previous(p0))
            if is_file:
                add("Comparar con HEAD", lambda: self.compare_head(p0))
        add("Mostrar log", lambda: self.log(p0), single and versioned)
        if is_file and versioned:
            add("Annotate", lambda: self.annotate(p0))
        menu.addSeparator()
        if conflicted:
            if is_file:
                add("Editar conflictos…", lambda: self.edit_conflicts(p0))
            add("Marcar como resuelto…", lambda: self.resolve(paths))
            menu.addSeparator()
        sub = menu.addMenu("Más")
        add("Revertir…", lambda: self.revert(paths), changed, m=sub)
        add("Eliminar…", lambda: self.delete(paths), versioned, m=sub)
        add("Renombrar…", lambda: self.rename(p0), single and versioned, m=sub)
        sub.addSeparator()
        add("Bloquear…", lambda: self.lock(paths), versioned, m=sub)
        add("Desbloquear…", lambda: self.unlock(paths), locked or versioned, m=sub)
        sub.addSeparator()
        add("Actualizar a revisión…", lambda: self.update_to(paths), versioned, m=sub)
        add("Crear parche…", lambda: self.create_patch(paths), changed, m=sub)
        add("Aplicar parche…", lambda: self.apply_patch(p0 if is_dir else self.base), True, m=sub)
        add("Añadir a changelist…", lambda: self.ask_changelist(paths), versioned, m=sub)
        add("Quitar de changelist", lambda: self.changelist(paths, None), versioned, m=sub)
        sub.addSeparator()
        add("Propiedades…", lambda: self.properties(p0), single and versioned, m=sub)
        add("Exportar…", lambda: self.export(p0), single and versioned, m=sub)
        if is_dir:
            add("Limpiar (cleanup)…", lambda: self.cleanup(p0), m=sub)
        rb = menu.addMenu("Ramas y fusiones")
        add("Crear rama / etiqueta…", lambda: self.branch(p0), single and versioned, m=rb)
        add("Cambiar (switch)…", lambda: self.switch(p0), single and versioned, m=rb)
        add("Fusionar (merge)…", lambda: self.merge(p0), single and versioned, m=rb)
        add("Navegador del repositorio", lambda: self.browser(p0), single and versioned, m=rb)
        add("Reubicar (relocate)…", lambda: self.relocate(self.base), True, m=rb)
        if is_dir and not versioned:
            add("Importar en el repositorio…", lambda: self.import_(p0))
