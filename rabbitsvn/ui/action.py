"""Ventana de notificación de operaciones largas (equivalente al 'action' de RabbitVCS)."""
from __future__ import annotations

import re
from typing import Callable, List, Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..svn.client import Command, SvnClient, SvnError, svn_env
from .common import STATUS_COLORS
from .context import CommandLog, Context

ACTION_CODES = {
    "A": "Añadido", "D": "Eliminado", "U": "Actualizado", "C": "Conflicto", "G": "Fusionado",
    "E": "Existente", "R": "Reemplazado", "M": "Modificado", "B": "Bloqueo roto",
    "Adding": "Añadiendo", "Deleting": "Eliminando", "Sending": "Enviando", "Replacing": "Reemplazando",
    "Reverted": "Revertido", "Resolved": "Resuelto", "Transmitting": "Transmitiendo",
}
ACTION_COLOR = {"A": "added", "Añadido": "added", "D": "deleted", "Eliminado": "deleted",
                "C": "conflicted", "Conflicto": "conflicted", "U": "modified", "G": "modified",
                "M": "modified", "R": "replaced"}

MAX_ROWS = 20000

LINE_RE = re.compile(r"^([ADUCGERMB ]{1,4})\s{1,}(\S.*)$")
WORD_RE = re.compile(r"^(Adding|Deleting|Sending|Replacing|Reverted|Resolved|Añadiendo|Eliminando|"
                     r"Enviando|Reemplazando|Revertido|Resuelto)(?: \(bin\))?\s+'?(.+?)'?$")


class ActionDialog(QDialog):
    """Ejecuta una o varias órdenes en secuencia mostrando su salida.

    `steps` es una lista de funciones client -> Command, para poder reconstruirlas si hay que
    reintentar con nuevas credenciales.
    """
    succeeded = Signal()

    def __init__(self, parent, ctx: Context, title: str, steps: List[Callable[[SvnClient], Command]],
                 on_success: Optional[Callable] = None, auto_close: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 440)
        self.ctx = ctx
        self.steps = steps
        self.on_success = on_success
        self.auto_close = auto_close
        self.index = 0
        self.proc: Optional[QProcess] = None
        self.cmd: Optional[Command] = None
        self.stderr_buf = ""
        self.stdout_buf = ""
        self.failed = False
        self.output_text: list = []
        self.rows = 0

        lay = QVBoxLayout(self)
        self.table = QTreeWidget()
        self.table.setHeaderLabels(["Acción", "Ruta / mensaje"])
        self.table.setRootIsDecorated(False)
        self.table.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 140)
        lay.addWidget(self.table)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        lay.addWidget(self.progress)
        row = QHBoxLayout()
        self.status = QLabel("Ejecutando…")
        row.addWidget(self.status, 1)
        self.cancel_btn = QPushButton("Cancelar")
        self.cancel_btn.clicked.connect(self.cancel)
        self.close_btn = QPushButton("Cerrar")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

    # ------------------------------------------------------------------
    def start(self):
        self.show()
        self._run_next()
        return self

    def _run_next(self):
        if self.index >= len(self.steps):
            self._finished_all()
            return
        try:
            client = self.ctx.client()
            self.cmd = self.steps[self.index](client)
        except Exception as exc:  # noqa: BLE001  (validación de datos, fichero inexistente…)
            self.cmd = None
            self._add_row("Error", str(exc), "err")
            CommandLog.get().err(str(exc))
            self._fail()
            return
        CommandLog.get().cmd(self.cmd.display())
        self._add_row("Orden", self.cmd.display(), "info")
        self.stderr_buf = ""
        self.stdout_buf = ""
        self.proc = QProcess(self)
        env = QProcessEnvironment()
        for k, v in svn_env().items():
            env.insert(k, v)
        self.proc.setProcessEnvironment(env)
        if self.cmd.cwd:
            self.proc.setWorkingDirectory(self.cmd.cwd)
        self.proc.readyReadStandardOutput.connect(self._read_out)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(self._proc_finished)
        self.proc.errorOccurred.connect(self._proc_error)
        self.proc.start(self.cmd.argv[0], self.cmd.argv[1:])
        if self.cmd.stdin:
            self.proc.write(self.cmd.stdin.encode())
        self.proc.closeWriteChannel()

    def _read_out(self):
        self.stdout_buf += bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self.stdout_buf = self.stdout_buf.split("\n")
        for line in lines:
            self._parse_line(line)

    def _read_err(self):
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        self.stderr_buf += data
        for line in data.splitlines():
            if line.strip():
                self._add_row("Error" if "E" in line[:8] else "Aviso", line, "err")
                CommandLog.get().err(line)

    def _parse_line(self, line: str):
        if not line.strip():
            return
        if len(self.output_text) < MAX_ROWS:
            self.output_text.append(line)
        CommandLog.get().out(line)
        m = WORD_RE.match(line)
        if m:
            self._add_row(ACTION_CODES.get(m.group(1), m.group(1)), m.group(2), ACTION_COLOR.get(m.group(1)))
            return
        m = LINE_RE.match(line)
        if m and len(m.group(1).strip()) >= 1 and not line.startswith(("Updat", "At ", "Checked", "Committ",
                                                                         "Export", "Fetch", "Summary",
                                                                         "Merge", "Record", "--- ")):
            code = m.group(1).strip()
            label = ", ".join(ACTION_CODES.get(c, c) for c in code if c.strip())
            self._add_row(label, m.group(2), ACTION_COLOR.get(code[0]))
            return
        self._add_row("", line, None)

    def _add_row(self, action: str, text: str, color_key: Optional[str]):
        self.rows += 1
        if self.rows > MAX_ROWS and color_key != "err":
            if self.rows == MAX_ROWS + 1:
                self.table.addTopLevelItem(QTreeWidgetItem(["", f"… (más de {MAX_ROWS} líneas; "
                                                                 "el resto solo en la consola)"]))
            return
        it = QTreeWidgetItem([action, text[:2000]])
        if color_key == "err":
            it.setForeground(0, QColor("#c62828"))
            it.setForeground(1, QColor("#c62828"))
        elif color_key == "info":
            it.setForeground(1, QColor("#808080"))
        elif color_key and color_key in STATUS_COLORS:
            it.setForeground(0, QColor(STATUS_COLORS[color_key]))
        self.table.addTopLevelItem(it)
        self.table.scrollToItem(it)

    def _proc_error(self, err):
        if err == QProcess.FailedToStart:
            self._add_row("Error", f"No se pudo ejecutar {self.cmd.argv[0]}", "err")
            self._fail()

    def _proc_finished(self, code, status):
        if self.stdout_buf:
            self._parse_line(self.stdout_buf)
            self.stdout_buf = ""
        if self.cmd:
            self.cmd.cleanup()
        if code != 0 or status != QProcess.NormalExit:
            if SvnError(self.cmd.argv if self.cmd else [], code, self.stderr_buf).is_auth_error:
                if self.ctx.ask_credentials(self, "Credenciales rechazadas:\n" + self.stderr_buf.strip()):
                    self._add_row("", "Reintentando con nuevas credenciales…", "info")
                    self._run_next()
                    return
            self._fail()
            return
        self.index += 1
        self._run_next()

    def _fail(self):
        self.failed = True
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status.setText("<b style='color:#c62828'>La operación ha fallado</b>")
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

    def _finished_all(self):
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.status.setText("<b style='color:#2e9d3a'>Completado</b>")
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self.close_btn.setFocus()
        if self.on_success:
            try:
                self.on_success(self)
            except Exception as exc:  # noqa: BLE001
                self._add_row("Error", f"Error tras la operación: {exc}", "err")
        self.succeeded.emit()
        if self.auto_close:
            self.accept()

    def running(self) -> bool:
        return bool(self.proc and self.proc.state() != QProcess.NotRunning)

    def cancel(self):
        if self.running():
            # SIGTERM primero para que svn libere los bloqueos de la working copy; SIGKILL si no responde
            self.proc.terminate()
            if not self.proc.waitForFinished(3000):
                self.proc.kill()
                self.proc.waitForFinished(1000)
            self._add_row("", "Cancelado por el usuario. Si la working copy queda bloqueada, usa "
                              "«Limpiar (cleanup)».", "err")
        self.index = len(self.steps)
        self._fail()
        self.status.setText("Cancelado")

    def reject(self):
        if self.running():
            if QMessageBox.question(self, "Cancelar", "La operación sigue en curso. ¿Cancelarla?") != QMessageBox.Yes:
                return
            self.cancel()
        super().reject()

    def closeEvent(self, ev):
        if self.running():
            if QMessageBox.question(self, "Cancelar", "La operación sigue en curso. ¿Cancelarla?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.cancel()
        super().closeEvent(ev)


def run_action(parent, ctx: Context, title: str, steps, on_success=None, modal: bool = True) -> ActionDialog:
    dlg = ActionDialog(parent, ctx, title, steps, on_success)
    dlg.setModal(modal)
    dlg.start()
    return dlg
