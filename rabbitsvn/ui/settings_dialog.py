"""Diálogo de ajustes generales."""
from __future__ import annotations

import glob
import os
import shutil

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from ..config import LOG_FILE, Config, setup_logging
from ..svn.client import SvnError
from .common import PathEdit, confirm, esc, mono_font, open_path, std_buttons

DIFF_TOOLS = ["meld", "kdiff3", "kompare", "diffuse", "bcompare", "code"]


class SettingsDialog(QDialog):
    def __init__(self, parent, config: Config):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Ajustes")
        self.resize(640, 480)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)
        tabs.addTab(self._general(), "General")
        tabs.addTab(self._external(), "Herramientas externas")
        tabs.addTab(self._svn(), "Subversion")
        tabs.addTab(self._cache(), "Caché e historial")
        tabs.addTab(self._logging(), "Registro")
        lay.addWidget(std_buttons(self, "Guardar"))

    # -------------------------------------------------------------- pestañas
    def _general(self):
        g = lambda k: self.config.get("general", k)  # noqa: E731
        w = QWidget()
        form = QFormLayout(w)
        self.show_unversioned = QCheckBox("Mostrar ficheros sin versionar")
        self.show_unversioned.setChecked(g("show_unversioned_files"))
        self.show_ignored = QCheckBox("Mostrar ficheros ignorados")
        self.show_ignored.setChecked(g("show_ignored_files"))
        self.recursive = QCheckBox("Calcular el estado de forma recursiva")
        self.recursive.setChecked(g("enable_recursive"))
        self.colorize = QCheckBox("Colorear los ficheros según su estado")
        self.colorize.setChecked(g("enable_colorize"))
        self.highlight = QCheckBox("Resaltar sintaxis en diff y annotate")
        self.highlight.setChecked(g("enable_highlighting"))
        self.switch_after_branch = QCheckBox("Cambiar (switch) a la nueva rama tras crearla")
        self.switch_after_branch.setChecked(g("switch_after_branch"))
        self.auto_refresh = QCheckBox("Refrescar el estado al volver a la ventana")
        self.auto_refresh.setChecked(g("auto_refresh_on_focus"))
        self.confirm_revert = QCheckBox("Pedir confirmación antes de revertir")
        self.confirm_revert.setChecked(g("confirm_revert"))
        self.check_updates = QCheckBox("Buscar versiones nuevas al arrancar y cada 6 horas")
        self.check_updates.setChecked(g("check_updates"))
        for cb in (self.show_unversioned, self.show_ignored, self.recursive, self.colorize,
                   self.highlight, self.switch_after_branch, self.auto_refresh, self.confirm_revert,
                   self.check_updates):
            form.addRow(cb)
        self.datetime_format = QLineEdit(g("datetime_format"))
        self.datetime_format.setToolTip("Formato strftime de Python, p. ej. %d/%m/%Y %H:%M")
        form.addRow("Formato de fecha:", self.datetime_format)
        self.log_limit = QSpinBox()
        self.log_limit.setRange(10, 10000)
        self.log_limit.setValue(int(g("log_limit") or 100))
        form.addRow("Revisiones por página en el log:", self.log_limit)
        self.default_msg = QPlainTextEdit(g("default_commit_message"))
        self.default_msg.setMaximumHeight(80)
        form.addRow("Mensaje de commit por defecto:", self.default_msg)
        return w

    def _external(self):
        w = QWidget()
        form = QFormLayout(w)
        self.diff_tool = PathEdit("file", self.config.get("external", "diff_tool"))
        form.addRow("Herramienta de diff:", self.diff_tool)
        self.diff_swap = QCheckBox("Intercambiar lados (izquierda = copia de trabajo)")
        self.diff_swap.setChecked(self.config.get("external", "diff_tool_swap"))
        form.addRow("", self.diff_swap)
        self.merge_tool = PathEdit("file", self.config.get("external", "merge_tool"))
        form.addRow("Herramienta de fusión (conflictos):", self.merge_tool)
        self.opener = QLineEdit(self.config.get("external", "file_opener"))
        form.addRow("Abrir ficheros con:", self.opener)
        detect = QPushButton("Detectar herramientas instaladas")
        detect.clicked.connect(self._detect_tools)
        form.addRow("", detect)
        hint = QLabel("Si no se configura herramienta de diff se usa el visor interno.")
        hint.setStyleSheet("color:gray")
        form.addRow(hint)
        return w

    def _detect_tools(self):
        found = [shutil.which(t) for t in DIFF_TOOLS if shutil.which(t)]
        if not found:
            QMessageBox.information(self, "Herramientas", "No se ha encontrado ninguna (meld, kdiff3, kompare…).")
            return
        if not self.diff_tool.text():
            self.diff_tool.setText(found[0])
        if not self.merge_tool.text():
            self.merge_tool.setText(found[0])
        QMessageBox.information(self, "Herramientas", "Encontradas:\n" + "\n".join(found))

    def _svn(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.svn_bin = PathEdit("file", self.config.get("svn", "svn_binary"))
        form.addRow("Binario svn:", self.svn_bin)
        self.svnadmin_bin = PathEdit("file", self.config.get("svn", "svnadmin_binary"))
        form.addRow("Binario svnadmin:", self.svnadmin_bin)
        self.config_dir = PathEdit("dir", self.config.get("svn", "config_dir"))
        self.config_dir.edit.setPlaceholderText("~/.subversion (por defecto)")
        form.addRow("Directorio de configuración:", self.config_dir)
        self.timeout = QSpinBox()
        self.timeout.setRange(0, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setSpecialValueText("Sin límite")
        self.timeout.setValue(int(self.config.get("svn", "timeout", 0) or 0))
        form.addRow("Tiempo máximo de consultas:", self.timeout)
        lay.addLayout(form)
        client = self.config.client()
        ver = client.version() if client.available() else "no encontrado"
        lay.addWidget(QLabel(f"Versión detectada de svn: <b>{esc(ver)}</b>"))
        store = self.config.passwords
        kr = QLabel(("Contraseñas de proyectos: llavero del sistema (" + esc(store.backend_name) + ")")
                    if store.persistent else
                    "<span style='color:#e65100'>No hay llavero seguro disponible: las contraseñas solo se "
                    "recuerdan mientras la aplicación está abierta.</span>")
        kr.setWordWrap(True)
        lay.addWidget(kr)
        self.plain_label = QLabel("")
        self.plain_label.setWordWrap(True)
        lay.addWidget(self.plain_label)
        self._check_plaintext()
        box = QGroupBox("Caché de autenticación de Subversion")
        bl = QHBoxLayout(box)
        b1 = QPushButton("Ver credenciales guardadas")
        b1.clicked.connect(self._show_auth)
        b2 = QPushButton("Borrar caché de autenticación")
        b2.clicked.connect(self._clear_auth)
        bl.addWidget(b1)
        bl.addWidget(b2)
        lay.addWidget(box)
        lay.addStretch()
        return w

    def _check_plaintext(self):
        """Avisa si la caché de svn guarda contraseñas en texto plano (passtype 'simple')."""
        n = 0
        for f in glob.glob(os.path.join(self._auth_dir(), "svn.simple", "*")):
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines[:-2]):
                if line == "passtype" and lines[i + 2] == "simple":
                    n += 1
                    break
        if n:
            self.plain_label.setText(
                f"<span style='color:#c62828'><b>Atención:</b> {n} credencial(es) de svn guardadas en texto plano "
                f"en {esc(self._auth_dir())}. Bórralas con el botón de abajo y marca --no-auth-cache en tus "
                "proyectos, o configura password-stores = gnome-keyring en ~/.subversion/config.</span>")
        else:
            self.plain_label.setText("<span style='color:#2e9d3a'>No hay contraseñas de svn en texto plano.</span>")

    def _auth_dir(self):
        base = self.config_dir.text() or os.path.expanduser("~/.subversion")
        return os.path.join(base, "auth")

    def _show_auth(self):
        try:
            out = self.config.client().run("auth")
        except SvnError as e:
            out = e.stderr
        dlg = QDialog(self)
        dlg.setWindowTitle("Credenciales en caché")
        dlg.resize(600, 400)
        l = QVBoxLayout(dlg)
        t = QPlainTextEdit(out or "(vacío)")
        t.setReadOnly(True)
        t.setFont(mono_font())
        l.addWidget(t)
        dlg.exec()

    def _clear_auth(self):
        d = self._auth_dir()
        if not confirm(self, f"Se borrarán las credenciales y certificados aceptados en\n{d}\n\n¿Continuar?"):
            return
        n = 0
        for sub in ("svn.simple", "svn.ssl.server", "svn.ssl.client-passphrase", "svn.username"):
            for f in glob.glob(os.path.join(d, sub, "*")):
                try:
                    os.unlink(f)
                    n += 1
                except OSError:
                    pass
        QMessageBox.information(self, "Caché", f"Eliminadas {n} entradas.")
        self._check_plaintext()

    def _cache(self):
        w = QWidget()
        form = QFormLayout(w)
        self.n_repos = QSpinBox()
        self.n_repos.setRange(1, 500)
        self.n_repos.setValue(int(self.config.get("cache", "number_repositories", 30)))
        form.addRow("URLs recientes a recordar:", self.n_repos)
        self.n_msgs = QSpinBox()
        self.n_msgs.setRange(1, 500)
        self.n_msgs.setValue(int(self.config.get("cache", "number_messages", 30)))
        form.addRow("Mensajes recientes a recordar:", self.n_msgs)
        b = QPushButton("Vaciar historial de URLs y mensajes")
        b.clicked.connect(self._clear_history)
        form.addRow("", b)
        return w

    def _clear_history(self):
        self.config.clear_history()
        QMessageBox.information(self, "Historial", "Historial vaciado.")

    def _logging(self):
        w = QWidget()
        form = QFormLayout(w)
        self.log_type = QComboBox()
        self.log_type.addItems(["Ninguno", "Archivo", "Consola", "Ambos"])
        self.log_type.setCurrentText(self.config.get("logging", "type"))
        form.addRow("Destino:", self.log_type)
        self.log_level = QComboBox()
        self.log_level.addItems(["Debug", "Info", "Warning", "Error", "Critical"])
        self.log_level.setCurrentText(self.config.get("logging", "level"))
        form.addRow("Nivel:", self.log_level)
        b = QPushButton("Abrir fichero de registro")
        b.clicked.connect(lambda: open_path(LOG_FILE, self.config) if os.path.exists(LOG_FILE)
                          else QMessageBox.information(self, "Registro", "Aún no existe."))
        form.addRow(QLabel(LOG_FILE), b)
        return w

    # -------------------------------------------------------------- guardar
    def accept(self):
        c = self.config
        c.set("general", "show_unversioned_files", self.show_unversioned.isChecked())
        c.set("general", "show_ignored_files", self.show_ignored.isChecked())
        c.set("general", "enable_recursive", self.recursive.isChecked())
        c.set("general", "enable_colorize", self.colorize.isChecked())
        c.set("general", "enable_highlighting", self.highlight.isChecked())
        c.set("general", "switch_after_branch", self.switch_after_branch.isChecked())
        c.set("general", "auto_refresh_on_focus", self.auto_refresh.isChecked())
        c.set("general", "confirm_revert", self.confirm_revert.isChecked())
        c.set("general", "check_updates", self.check_updates.isChecked())
        c.set("general", "datetime_format", self.datetime_format.text())
        c.set("general", "log_limit", self.log_limit.value())
        c.set("general", "default_commit_message", self.default_msg.toPlainText())
        c.set("external", "diff_tool", self.diff_tool.text())
        c.set("external", "diff_tool_swap", self.diff_swap.isChecked())
        c.set("external", "merge_tool", self.merge_tool.text())
        c.set("external", "file_opener", self.opener.text().strip() or "xdg-open")
        c.set("svn", "svn_binary", self.svn_bin.text() or "svn")
        c.set("svn", "svnadmin_binary", self.svnadmin_bin.text() or "svnadmin")
        c.set("svn", "config_dir", self.config_dir.text())
        c.set("svn", "timeout", self.timeout.value())
        c.set("cache", "number_repositories", self.n_repos.value())
        c.set("cache", "number_messages", self.n_msgs.value())
        c.set("logging", "type", self.log_type.currentText())
        c.set("logging", "level", self.log_level.currentText())
        c.save()
        setup_logging(c)
        super().accept()
