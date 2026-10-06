"""Contexto de trabajo (configuración + proyecto activo + credenciales) y diálogo de login."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QVBoxLayout)

from ..config import APP_NAME, Config, Project
from ..svn.client import SvnClient, SvnError
from .common import run_async, show_error, std_buttons

log = logging.getLogger(APP_NAME)


class CommandLog(QObject):
    """Bus global para mostrar en la consola de la ventana principal lo que se ejecuta."""
    message = Signal(str, str)   # texto, nivel (cmd, out, err, info)

    _instance = None

    @classmethod
    def get(cls) -> "CommandLog":
        if cls._instance is None:
            cls._instance = CommandLog()
        return cls._instance

    def cmd(self, text):
        log.info("$ %s", text)
        self.message.emit(text, "cmd")

    def out(self, text):
        self.message.emit(text, "out")

    def err(self, text):
        log.error(text)
        self.message.emit(text, "err")

    def info(self, text):
        self.message.emit(text, "info")


class LoginDialog(QDialog):
    def __init__(self, parent, project: Optional[Project], reason: str = "", can_persist: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Autenticación SVN")
        lay = QVBoxLayout(self)
        if reason:
            lbl = QLabel(reason[:1500])
            lbl.setTextFormat(Qt.PlainText)   # el texto viene del servidor: nunca como HTML
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color:#c62828")
            lay.addWidget(lbl)
        form = QFormLayout()
        self.user = QLineEdit(project.username if project else "")
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        form.addRow("Usuario:", self.user)
        form.addRow("Contraseña:", self.pwd)
        lay.addLayout(form)
        self.remember = QCheckBox("Guardar en el llavero del sistema" if can_persist
                                  else "Recordar durante esta sesión")
        self.remember.setChecked(bool(project and project.remember_password))
        lay.addWidget(self.remember)
        lay.addWidget(std_buttons(self, "Conectar"))
        (self.pwd if self.user.text() else self.user).setFocus()


class Context:
    """Agrupa lo necesario para ejecutar órdenes svn desde cualquier diálogo."""

    def __init__(self, config: Config, project: Optional[Project] = None,
                 password_override: Optional[str] = None):
        self.config = config
        self.project = project
        # Contraseña provisional (formulario sin guardar): no toca el llavero ni la sesión
        self.password_override = password_override
        self._anon_password = ""   # credenciales sin proyecto (p. ej. checkout nuevo)
        self._anon_user = ""

    def client(self) -> SvnClient:
        if self.project:
            return self.config.client(self.project, password=self.password_override)
        c = self.config.client(None)
        c.creds.username = self._anon_user
        c.creds.password = self._anon_password
        return c

    def with_project(self, project: Optional[Project]) -> "Context":
        return Context(self.config, project)

    def ask_credentials(self, parent, reason: str = "") -> bool:
        dlg = LoginDialog(parent, self.project, reason, self.config.passwords.persistent)
        if dlg.exec() != QDialog.Accepted:
            return False
        user, pwd = dlg.user.text().strip(), dlg.pwd.text()
        if self.password_override is not None:   # proyecto aún sin guardar
            self.project.username = user
            self.password_override = pwd
            return True
        if self.project:
            self.project.username = user
            self.project.remember_password = dlg.remember.isChecked()
            self.config.passwords.set(self.project.id, pwd, dlg.remember.isChecked())
            if self.config.project(self.project.id):
                self.config.save_project(self.project)
        else:
            self._anon_user, self._anon_password = user, pwd
        return True


def run_svn(parent, ctx: Context, fn: Callable[[SvnClient], object], on_done: Callable = None,
            on_error: Callable = None, label: str = ""):
    """Ejecuta fn(client) en segundo plano; si falla la autenticación pide credenciales y reintenta."""
    if label:
        CommandLog.get().cmd(label)

    def err(exc):
        if isinstance(exc, SvnError) and exc.is_auth_error:
            if ctx.ask_credentials(parent, "El servidor ha rechazado las credenciales:\n" + exc.stderr):
                run_svn(parent, ctx, fn, on_done, on_error)
                return
        if isinstance(exc, SvnError):
            CommandLog.get().err(exc.stderr)
        if on_error:
            on_error(exc)
        else:
            show_error(parent, exc)

    run_async(lambda: fn(ctx.client()), on_done, err, parent)
