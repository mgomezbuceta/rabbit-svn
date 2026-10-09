"""Contexto de trabajo (configuración + proyecto activo + credenciales) y diálogo de login."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QVBoxLayout)

from ..config import APP_NAME, Account, Config, Project, server_of
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
    def __init__(self, parent, project: Optional[Project], reason: str = "", can_persist: bool = True,
                 account=None):
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
        self.user = QLineEdit(account.username if account else (project.username if project else ""))
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        form.addRow("Usuario:", self.user)
        form.addRow("Contraseña:", self.pwd)
        lay.addLayout(form)
        self.remember = QCheckBox("Guardar en el llavero del sistema" if can_persist
                                  else "Recordar durante esta sesión")
        self.remember.setChecked(account.remember_password if account
                                 else bool(project and project.remember_password))
        lay.addWidget(self.remember)
        if account:
            note = QLabel(f"La contraseña se actualizará en todos los proyectos de {account.server}.")
            note.setTextFormat(Qt.PlainText)
            note.setWordWrap(True)
            note.setStyleSheet("color:gray")
            lay.addWidget(note)
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
        acc = self.config.account(self.project.account_id) if self.project and self.project.account_id else None
        dlg = LoginDialog(parent, self.project, reason, self.config.passwords.persistent, acc)
        if dlg.exec() != QDialog.Accepted:
            return False
        user, pwd, remember = dlg.user.text().strip(), dlg.pwd.text(), dlg.remember.isChecked()
        if self.password_override is not None:   # proyecto aún sin guardar
            if acc is None or acc.username != user:
                self.project.account_id = ""
            self.project.username = user
            self.password_override = pwd
            return True
        if self.project:
            server = server_of(self.project.url or self.project.repo_root)
            if server and user:
                # Credencial compartida por servidor: se crea o se actualiza la de ese usuario
                account = self.config.find_account(server, user) or Account(server=server, username=user)
                account.remember_password = remember
                self.config.save_account(account, pwd)
                self.project.account_id = account.id
                self.project.username = user
                self.config.passwords.delete(self.project.id)   # restos del formato antiguo
            else:   # file:// o sin usuario: se guarda en el propio proyecto
                self.project.account_id = ""
                self.project.username = user
                self.project.remember_password = remember
                self.config.passwords.set(self.project.id, pwd, remember)
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
