"""Alta/edición de proyectos: desde una working copy local o haciendo checkout de una URL."""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QStackedWidget, QVBoxLayout, QWidget)

from ..config import Config, Project
from ..svn.client import TRUST_FAILURES, SvnError, ValidationError, validate_url
from .action import ActionDialog
from .common import DEPTHS, PathEdit, RevisionWidget, UrlCombo, combo_from, esc, fmt_date, show_error, std_buttons
from .context import Context, run_svn

TRUST_LABELS = {
    "unknown-ca": "Autoridad de certificación desconocida (autofirmado)",
    "cn-mismatch": "El nombre del certificado no coincide con el host",
    "expired": "Certificado caducado",
    "not-yet-valid": "Certificado aún no válido",
    "other": "Otros errores del certificado",
}


class ProjectDialog(QDialog):
    def __init__(self, parent, config: Config, project: Optional[Project] = None, initial_path: str = ""):
        super().__init__(parent)
        self.config = config
        self.editing = project is not None
        self.project = project or Project(name="")
        self.setWindowTitle("Editar proyecto" if self.editing else "Añadir proyecto SVN")
        self.resize(720, 640)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(self.project.name)
        self.name.setPlaceholderText("Nombre descriptivo del proyecto")
        form.addRow("Nombre:", self.name)
        lay.addLayout(form)

        # ---- origen
        origin = QGroupBox("Origen")
        ol = QVBoxLayout(origin)
        rl = QHBoxLayout()
        self.r_local = QRadioButton("Carpeta local ya descargada (working copy)")
        self.r_remote = QRadioButton("Descargar desde repositorio (checkout)")
        grp = QButtonGroup(self)
        grp.addButton(self.r_local)
        grp.addButton(self.r_remote)
        rl.addWidget(self.r_local)
        rl.addWidget(self.r_remote)
        ol.addLayout(rl)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._local_page())
        self.stack.addWidget(self._remote_page())
        ol.addWidget(self.stack)
        self.r_local.toggled.connect(lambda on: self.stack.setCurrentIndex(0 if on else 1))
        self.r_local.setChecked(True)
        if self.editing:
            self.r_remote.setEnabled(False)
        lay.addWidget(origin)

        # ---- conexión
        conn = QGroupBox("Conexión y credenciales")
        cf = QFormLayout(conn)
        self.username = QLineEdit(self.project.username)
        self.username.setPlaceholderText("Vacío = anónimo o credenciales en caché de svn")
        cf.addRow("Usuario:", self.username)
        # La contraseña guardada no se vuelca al formulario: vacío = mantener la actual
        self._stored_pwd = config.passwords.get(self.project.id) if self.editing else ""
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        if self._stored_pwd:
            self.password.setPlaceholderText("(guardada — déjalo vacío para mantenerla)")
        cf.addRow("Contraseña:", self.password)
        store = ("Guardar contraseña en el llavero del sistema" if config.passwords.persistent
                 else "Recordar contraseña (solo esta sesión: no hay llavero disponible)")
        self.remember = QCheckBox(store)
        self.remember.setChecked(self.project.remember_password)
        cf.addRow("", self.remember)
        self.no_auth_cache = QCheckBox("No guardar credenciales en la caché de svn (--no-auth-cache)")
        self.no_auth_cache.setChecked(self.project.no_auth_cache)
        cf.addRow("", self.no_auth_cache)
        trust = QGroupBox("Aceptar certificados SSL con estos problemas")
        tl = QVBoxLayout(trust)
        self.trust = {}
        for key in TRUST_FAILURES:
            cb = QCheckBox(TRUST_LABELS[key])
            cb.setChecked(key in self.project.trust_failures)
            tl.addWidget(cb)
            self.trust[key] = cb
        cf.addRow(trust)
        warn = QLabel("Acepta solo los problemas que conozcas (p. ej. un certificado autofirmado de tu "
                      "servidor). Aceptar certificados no válidos permite ataques de intermediario.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color:gray")
        cf.addRow(warn)
        lay.addWidget(conn)

        self.notes = QPlainTextEdit(self.project.notes)
        self.notes.setPlaceholderText("Notas (opcional)")
        self.notes.setMaximumHeight(60)
        lay.addWidget(self.notes)
        lay.addWidget(std_buttons(self, "Guardar" if self.editing else "Añadir"))

        if initial_path:
            self.local_path.setText(initial_path)
            self._read_local()

    # -------------------------------------------------------------- páginas
    def _local_page(self):
        w = QWidget()
        f = QFormLayout(w)
        self.local_path = PathEdit("dir", self.project.wc_path)
        f.addRow("Carpeta:", self.local_path)
        b = QPushButton("Leer información de la carpeta")
        b.clicked.connect(self._read_local)
        f.addRow("", b)
        self.info_label = QLabel(self._info_text(self.project.url, self.project.repo_root, None, "", None)
                                 if self.project.url else "")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(self.info_label)
        return w

    def _remote_page(self):
        w = QWidget()
        f = QFormLayout(w)
        self.url = UrlCombo(self.config, self.project.url)
        f.addRow("URL del repositorio:", self.url)
        self.dest = PathEdit("dir", os.path.expanduser("~/"))
        f.addRow("Carpeta destino:", self.dest)
        self.url.currentTextChanged.connect(self._suggest_dest)
        self.revision = RevisionWidget()
        f.addRow("Revisión:", self.revision)
        self.depth = combo_from(DEPTHS, "infinity")
        f.addRow("Profundidad:", self.depth)
        self.ignore_externals = QCheckBox("Omitir externals")
        f.addRow("", self.ignore_externals)
        b = QPushButton("Probar conexión")
        b.clicked.connect(self._test_remote)
        f.addRow("", b)
        self.remote_label = QLabel("")
        self.remote_label.setWordWrap(True)
        f.addRow(self.remote_label)
        return w

    def _suggest_dest(self, url: str):
        url = url.strip().rstrip("/")
        if not url:
            return
        cur = self.dest.text()
        base = os.path.dirname(cur.rstrip("/")) if cur and not cur.endswith("/") else cur.rstrip("/")
        name = url.split("/")[-1]
        if name in ("trunk",) and len(url.split("/")) > 1:
            name = url.split("/")[-2]
        self.dest.setText(os.path.join(base or os.path.expanduser("~"), name))
        if not self.name.text():
            self.name.setPlaceholderText(name)

    def _info_text(self, url, root, rev, author, date):
        parts = [f"<b>URL:</b> {esc(url)}", f"<b>Raíz del repositorio:</b> {esc(root)}"]
        if rev is not None:
            parts.append(f"<b>Revisión:</b> {esc(rev)}")
        if author:
            parts.append(f"<b>Último cambio:</b> {esc(author)} — {esc(fmt_date(date, self.config))}")
        return "<br>".join(parts)

    @staticmethod
    def _err_html(prefix: str, exc) -> str:
        msg = exc.stderr if isinstance(exc, SvnError) else str(exc)
        return f"<span style='color:#c62828'>{esc(prefix)}{esc(msg)}</span>"

    # -------------------------------------------------------------- acciones
    def _temp_project(self) -> Project:
        p = Project(name=self.name.text().strip(), username=self.username.text().strip(),
                    trust_failures=[k for k, cb in self.trust.items() if cb.isChecked()],
                    no_auth_cache=self.no_auth_cache.isChecked(), id=self.project.id)
        return p

    def _ctx(self) -> Context:
        """Contexto con la contraseña del formulario, sin guardarla en ningún sitio todavía.
        Si el servidor la rechaza, LoginDialog la sustituye dentro de este contexto."""
        self._last_ctx = Context(self.config, self._temp_project(),
                                 password_override=self.password.text() or self._stored_pwd)
        return self._last_ctx

    def _read_local(self):
        path = self.local_path.text()
        if not path or not os.path.isdir(path):
            self.info_label.setText("<span style='color:#c62828'>La carpeta no existe.</span>")
            return

        def done(infos):
            i = infos[0]
            self.project.url, self.project.repo_root = i.url, i.repo_root
            if i.wc_root and os.path.realpath(i.wc_root) != os.path.realpath(path):
                self.local_path.setText(i.wc_root)
            self.info_label.setText(self._info_text(i.url, i.repo_root, i.revision,
                                                    i.last_changed_author, i.last_changed_date))
            if not self.name.text():
                self.name.setText(os.path.basename(os.path.normpath(i.wc_root or path)))

        def err(exc):
            self.info_label.setText(self._err_html("No es una working copy válida: ", exc))
        run_svn(self, self._ctx(), lambda c: c.info(path), done, err)

    def _valid_url(self, url: str) -> bool:
        try:
            validate_url(url)
            return True
        except ValidationError as exc:
            QMessageBox.warning(self, "URL", str(exc))
            return False

    def _test_remote(self):
        url = self.url.text()
        if not url or not self._valid_url(url):
            return
        self.remote_label.setText("Conectando…")

        def done(infos):
            i = infos[0]
            self.project.repo_root = i.repo_root
            self.remote_label.setText("<span style='color:#2e9d3a'>Conexión correcta.</span><br>" +
                                      self._info_text(i.url, i.repo_root, i.revision,
                                                      i.last_changed_author, i.last_changed_date))

        def err(exc):
            self.remote_label.setText(self._err_html("", exc))
        run_svn(self, self._ctx(), lambda c: c.info(url), done, err)

    def _duplicate(self, wc_path: str) -> bool:
        real = os.path.realpath(wc_path)
        for p in self.config.projects:
            if p.id != self.project.id and p.wc_path and os.path.realpath(p.wc_path) == real:
                QMessageBox.warning(self, "Proyecto", f"Esa carpeta ya está registrada como «{p.name}».")
                return True
        return False

    # -------------------------------------------------------------- guardar
    def _store(self, wc_path: str, url: str, ctx: Optional[Context] = None):
        p = self.project
        tmp = ctx.project if ctx else self._temp_project()   # el usuario puede venir del LoginDialog
        p.name = self.name.text().strip() or os.path.basename(os.path.normpath(wc_path))
        p.username, p.trust_failures, p.no_auth_cache = tmp.username, tmp.trust_failures, tmp.no_auth_cache
        p.remember_password = self.remember.isChecked()
        p.notes = self.notes.toPlainText()
        p.wc_path = os.path.normpath(wc_path)
        p.url = url or p.url
        pwd = (ctx.password_override if ctx and ctx.password_override is not None
               else self.password.text() or self._stored_pwd)
        self.config.save_project(p)
        if p.username and pwd:
            self.config.passwords.set(p.id, pwd, p.remember_password)
        elif not p.username:
            self.config.passwords.delete(p.id)
        if p.url:
            self.config.remember_url(p.url)

    def accept(self):
        if self.r_local.isChecked():
            path = self.local_path.text()
            if not os.path.isdir(path):
                QMessageBox.warning(self, "Proyecto", "Selecciona una carpeta existente.")
                return
            ctx = self._ctx()

            def done(infos):
                i = infos[0]
                root = i.wc_root or path
                if self._duplicate(root):
                    return
                self.project.repo_root = i.repo_root
                self._store(root, i.url, ctx)
                super(ProjectDialog, self).accept()

            def err(exc):
                if isinstance(exc, SvnError) and ("E155007" in exc.stderr or "not a working copy" in exc.stderr):
                    QMessageBox.warning(self, "Proyecto", "La carpeta no es una working copy de SVN.\n\n" + exc.stderr)
                else:
                    show_error(self, exc)
            run_svn(self, ctx, lambda c: c.info(path), done, err)
            return

        url, dest = self.url.text(), self.dest.text()
        if not url or not dest:
            QMessageBox.warning(self, "Proyecto", "Indica la URL y la carpeta destino.")
            return
        if not self._valid_url(url) or self._duplicate(dest):
            return
        if os.path.isdir(dest) and os.listdir(dest):
            if QMessageBox.question(self, "Checkout", f"La carpeta {dest} no está vacía. ¿Continuar?") != QMessageBox.Yes:
                return
        ctx = self._ctx()
        rev = self.revision.value()
        depth = self.depth.currentData()
        ign = self.ignore_externals.isChecked()

        def ok(_dlg):
            self._store(dest, url, ctx)
            super(ProjectDialog, self).accept()

        dlg = ActionDialog(self, ctx, f"Checkout de {url}",
                           [lambda c: c.cmd_checkout(url, dest, None if rev == "HEAD" else rev, depth, ign)], ok)
        dlg.setModal(True)
        dlg.start()
