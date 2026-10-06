"""Visor de diferencias interno + lanzamiento de la herramienta externa."""
from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from ..svn.client import SvnClient, peg
from .common import launch_tool, mono_font, safe_filename, save_bytes, write_private_temp

MAX_DIFF_DISPLAY = 8 * 1024 * 1024
from .context import Context, run_svn


class DiffHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.f_add = QTextCharFormat()
        self.f_add.setForeground(QColor("#2e7d32"))
        self.f_add.setBackground(QColor(46, 125, 50, 30))
        self.f_del = QTextCharFormat()
        self.f_del.setForeground(QColor("#c62828"))
        self.f_del.setBackground(QColor(198, 40, 40, 30))
        self.f_hunk = QTextCharFormat()
        self.f_hunk.setForeground(QColor("#1565c0"))
        self.f_head = QTextCharFormat()
        self.f_head.setForeground(QColor("#6a1b9a"))
        self.f_head.setFontWeight(700)

    def highlightBlock(self, text):
        if text.startswith(("Index:", "=====", "+++", "---", "Property changes on:", "___")):
            self.setFormat(0, len(text), self.f_head)
        elif text.startswith("@@") or text.startswith("##"):
            self.setFormat(0, len(text), self.f_hunk)
        elif text.startswith("+"):
            self.setFormat(0, len(text), self.f_add)
        elif text.startswith("-"):
            self.setFormat(0, len(text), self.f_del)


class DiffDialog(QDialog):
    """Muestra un diff unificado. `producer(client, ignore_ws)` devuelve el texto."""

    def __init__(self, parent, ctx: Context, title: str, producer: Callable[[SvnClient, bool], str],
                 external: Optional[Callable] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.producer = producer
        self.setWindowTitle(title)
        self.resize(1000, 700)
        self.setAttribute(Qt.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.stats = QLabel("")
        top.addWidget(self.stats, 1)
        self.ignore_ws = QCheckBox("Ignorar espacios en blanco")
        self.ignore_ws.toggled.connect(self.reload)
        top.addWidget(self.ignore_ws)
        if external and ctx.config.get("external", "diff_tool"):
            b = QPushButton("Abrir en herramienta externa")
            b.clicked.connect(external)
            top.addWidget(b)
        save = QPushButton("Guardar como parche…")
        save.clicked.connect(self.save_patch)
        top.addWidget(save)
        lay.addLayout(top)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setFont(mono_font())
        if ctx.config.get("general", "enable_highlighting", True):
            self.hl = DiffHighlighter(self.text.document())
        lay.addWidget(self.text)
        self.raw = None
        self.reload()

    def reload(self):
        self.text.setPlainText("Calculando diferencias…")
        ws = self.ignore_ws.isChecked()

        def done(out: str):
            self.raw = out
            shown = out
            if len(out) > MAX_DIFF_DISPLAY:
                shown = out[:MAX_DIFF_DISPLAY] + ("\n\n… diff truncado en pantalla "
                                                  f"({len(out) // 1048576} MB). Usa «Guardar como parche» "
                                                  "para obtenerlo completo.")
            self.text.setPlainText(shown or "(Sin diferencias)")
            adds = sum(1 for l in out.splitlines() if l.startswith("+") and not l.startswith("+++"))
            dels = sum(1 for l in out.splitlines() if l.startswith("-") and not l.startswith("---"))
            files = sum(1 for l in out.splitlines() if l.startswith("Index: "))
            self.stats.setText(f"{files} fichero(s) · <span style='color:#2e7d32'>+{adds}</span> "
                               f"<span style='color:#c62828'>-{dels}</span>")
        run_svn(self, self.ctx, lambda c: self.producer(c, ws), done)

    def save_patch(self):
        path, _ = QFileDialog.getSaveFileName(self, "Guardar parche", os.path.expanduser("~/cambios.patch"),
                                              "Parches (*.patch *.diff);;Todos (*)")
        if path and self.raw is not None:
            save_bytes(self, path, self.raw.encode("utf-8"))


# ---------------------------------------------------------------------- helpers
def _tmp_copy(data: bytes, name: str, label: str) -> str:
    base, ext = os.path.splitext(safe_filename(name))
    return write_private_temp(data, f"{base}.{safe_filename(label)}{ext}")


def external_diff(parent, ctx: Context, left_target: str, left_rev: Optional[str],
                  right_target: str, right_rev: Optional[str]):
    """Prepara ambos lados (cat a temporales cuando no es la copia de trabajo) y lanza la herramienta."""
    tool = ctx.config.get("external", "diff_tool")
    if not tool:
        QMessageBox.information(parent, "Diff", "No hay herramienta de diff externa configurada (Ajustes).")
        return
    swap = ctx.config.get("external", "diff_tool_swap")

    def side(c: SvnClient, target, rev):
        if rev is None and os.path.exists(target):
            return target
        name = os.path.basename(target.rstrip("/"))
        try:
            data = c.cat(target, rev)
        except Exception:  # noqa: BLE001  (fichero inexistente en esa revisión)
            data = b""
        return _tmp_copy(data, name, f"r{rev or 'WC'}")

    def work(c):
        return side(c, left_target, left_rev), side(c, right_target, right_rev)

    def done(paths):
        left, right = paths
        launch_tool([tool, right, left] if swap else [tool, left, right])
    run_svn(parent, ctx, work, done)


def diff_working(parent, ctx: Context, paths, base_dir: str = ""):
    """Diff de la copia de trabajo frente a BASE."""
    paths = list(paths)
    tool = ctx.config.get("external", "diff_tool")
    single_file = len(paths) == 1 and os.path.isfile(paths[0])
    ext = (lambda: external_diff(parent, ctx, paths[0], "BASE", paths[0], None)) if single_file else None
    if tool and single_file:
        ext()
        return
    title = "Diferencias: " + (os.path.relpath(paths[0], base_dir) if base_dir and len(paths) == 1 else
                               f"{len(paths)} elementos")
    DiffDialog(parent, ctx, title, lambda c, ws: c.diff(paths, ignore_whitespace=ws), ext).show()


def diff_revisions(parent, ctx: Context, target: str, old_rev: str, new_rev: str, prefer_external: bool = True,
                   kind: str = "file"):
    """Diff de un mismo target (URL o ruta) entre dos revisiones."""
    tool = ctx.config.get("external", "diff_tool")
    ext = (lambda: external_diff(parent, ctx, target, old_rev, target, new_rev)) if kind == "file" else None
    if tool and prefer_external and ext:
        ext()
        return
    DiffDialog(parent, ctx, f"Diferencias r{old_rev} → r{new_rev}: {target}",
               lambda c, ws: c.diff(old=peg(target, old_rev), new=peg(target, new_rev),
                                    ignore_whitespace=ws), ext).show()


def diff_change(parent, ctx: Context, target: str, rev: int):
    """Cambios introducidos por una revisión concreta (svn diff -c)."""
    DiffDialog(parent, ctx, f"Cambios de la revisión {rev}",
               lambda c, ws: c.diff([target], change=rev, ignore_whitespace=ws)).show()


def diff_urls(parent, ctx: Context, old: str, new: str, kind: str = "file"):
    tool = ctx.config.get("external", "diff_tool")

    def split(t):
        if "@" in t.rsplit("/", 1)[-1]:
            a, r = t.rsplit("@", 1)
            return a, r
        return t, None
    (lt, lr), (rt, rr) = split(old), split(new)
    ext = (lambda: external_diff(parent, ctx, lt, lr or "HEAD", rt, rr)) if kind == "file" else None
    if tool and ext:
        ext()
        return
    DiffDialog(parent, ctx, f"Comparar {old} ↔ {new}",
               lambda c, ws: c.diff(old=old, new=new, ignore_whitespace=ws), ext).show()
