"""Editor de propiedades (versionadas o de revisión)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from .common import esc, mono_font, std_buttons
from .context import CommandLog, Context, run_svn

COMMON_PROPS = {
    "svn:ignore": "Patrones a ignorar en esta carpeta (uno por línea)",
    "svn:global-ignores": "Patrones a ignorar heredables por las subcarpetas",
    "svn:externals": "Definiciones de externals: URL[@REV] carpeta",
    "svn:eol-style": "native, LF, CRLF o CR",
    "svn:keywords": "Date Revision Author HeadURL Id",
    "svn:mime-type": "Tipo MIME (application/octet-stream = binario)",
    "svn:executable": "Marca el fichero como ejecutable (valor '*')",
    "svn:needs-lock": "Obliga a bloquear antes de editar (valor '*')",
    "svn:auto-props": "Propiedades automáticas heredables",
    "svn:mergeinfo": "Información de fusiones (gestionada por svn)",
}
REVPROPS = {"svn:log": "Mensaje", "svn:author": "Autor", "svn:date": "Fecha"}


class PropEditDialog(QDialog):
    def __init__(self, parent, name: str = "", value: str = "", revprop: bool = False, allow_recursive: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Propiedad")
        self.resize(560, 380)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QComboBox()
        self.name.setEditable(True)
        self.name.addItems(list(REVPROPS) if revprop else list(COMMON_PROPS))
        self.name.setCurrentText(name)
        self.name.currentTextChanged.connect(self._hint)
        form.addRow("Nombre:", self.name)
        self.hint = QLabel("")
        self.hint.setStyleSheet("color:gray")
        form.addRow("", self.hint)
        lay.addLayout(form)
        self.value = QPlainTextEdit(value)
        self.value.setFont(mono_font())
        lay.addWidget(self.value)
        self.recursive = QCheckBox("Aplicar de forma recursiva")
        self.recursive.setVisible(allow_recursive and not revprop)
        lay.addWidget(self.recursive)
        lay.addWidget(std_buttons(self))
        self._hint(name)

    def _hint(self, name):
        self.hint.setText(COMMON_PROPS.get(name, ""))
        if name in ("svn:executable", "svn:needs-lock") and not self.value.toPlainText():
            self.value.setPlainText("*")


class PropertiesDialog(QDialog):
    """Si `revision` se indica, edita propiedades de revisión de `target` (URL del repo)."""

    def __init__(self, parent, ctx: Context, target: str, revision: Optional[str] = None):
        super().__init__(parent)
        self.ctx = ctx
        self.target = target
        self.revision = revision
        self.revprop = revision is not None
        self.original: dict = {}
        self.setWindowTitle(f"Propiedades de revisión r{revision}" if self.revprop else f"Propiedades: {target}")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{esc(target)}</b>" + (f" @ r{esc(revision)}" if self.revprop else "")))
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Nombre", "Valor"])
        self.table.setRootIsDecorated(False)
        self.table.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit())
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Nueva…", self.add), ("Editar…", self.edit), ("Eliminar", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        if self.revprop:
            note = QLabel("Modificar propiedades de revisión requiere el hook pre-revprop-change en el servidor.")
            note.setStyleSheet("color:gray")
            lay.addWidget(note)
        lay.addWidget(std_buttons(self, "Aplicar"))
        self.pending: dict = {}     # nombre -> (valor | None, recursivo)
        self.load()

    def load(self):
        def done(props):
            self.original = dict(props)
            self.table.clear()
            for k, v in sorted(props.items()):
                self._row(k, v)
        run_svn(self, self.ctx, lambda c: c.proplist(self.target, self.revision, self.revprop), done)

    def _row(self, name, value):
        it = QTreeWidgetItem([name, value.replace("\n", " ⏎ ")])
        it.setData(0, Qt.UserRole, value)
        it.setToolTip(1, value)
        self.table.addTopLevelItem(it)
        return it

    def _find(self, name):
        for i in range(self.table.topLevelItemCount()):
            it = self.table.topLevelItem(i)
            if it.text(0) == name:
                return it
        return None

    def add(self, name="", value=""):
        dlg = PropEditDialog(self, name, value, self.revprop)
        if dlg.exec() != QDialog.Accepted:
            return
        n, v = dlg.name.currentText().strip(), dlg.value.toPlainText()
        if not n:
            return
        it = self._find(n)
        if it:
            it.setText(1, v.replace("\n", " ⏎ "))
            it.setData(0, Qt.UserRole, v)
        else:
            it = self._row(n, v)
        f = it.font(0)
        f.setBold(True)
        it.setFont(0, f)
        self.pending[n] = (v, dlg.recursive.isChecked())

    def edit(self):
        it = self.table.currentItem()
        if it:
            self.add(it.text(0), it.data(0, Qt.UserRole))

    def remove(self):
        it = self.table.currentItem()
        if not it:
            return
        name = it.text(0)
        self.table.takeTopLevelItem(self.table.indexOfTopLevelItem(it))
        self.pending[name] = (None, False)

    def accept(self):
        if not self.pending:
            super().accept()
            return
        pending = dict(self.pending)

        def work(c):
            for name, (value, rec) in pending.items():
                if value is None:
                    if name in self.original:
                        c.propdel(name, self.target, rec, self.revprop, self.revision)
                else:
                    c.propset(name, value, self.target, rec, self.revprop, self.revision)
                CommandLog.get().info(f"Propiedad {name} {'eliminada' if value is None else 'establecida'}")
            return True

        def err(exc):
            QMessageBox.warning(self, "Propiedades", getattr(exc, "stderr", str(exc)))
        run_svn(self, self.ctx, work, lambda _: QDialog.accept(self), err)
