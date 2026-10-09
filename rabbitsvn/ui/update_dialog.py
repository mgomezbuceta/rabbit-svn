"""Aviso de versiones nuevas e instalación con un clic."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressDialog,
                               QPushButton, QTextBrowser, QVBoxLayout)

from .. import __version__, updater
from ..config import APP_NAME
from ..svn.client import Command
from .action import ActionDialog
from .common import esc, private_temp_dir, run_async

log = logging.getLogger(APP_NAME)
CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000
FIRST_CHECK_DELAY_MS = 8 * 1000


class UpdateDialog(QDialog):
    INSTALL, LATER, SKIP = 1, 2, 3

    def __init__(self, parent, rel: updater.Release, can_install: bool):
        super().__init__(parent)
        self.setWindowTitle("Actualización disponible")
        self.resize(620, 460)
        self.choice = self.LATER
        lay = QVBoxLayout(self)
        title = QLabel(f"<h3>RabbitSVN {esc(rel.version)} está disponible</h3>"
                       f"Tienes la versión {esc(__version__)}.")
        lay.addWidget(title)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(rel.notes or "_(Sin notas de versión)_")
        lay.addWidget(notes, 1)
        if can_install:
            hint = ("Se descargará el paquete, se comprobará su suma SHA-256 y se instalará con apt. "
                    "El sistema te pedirá tu contraseña.")
        else:
            hint = ("Esta copia no está instalada desde el paquete .deb (p. ej. desde el código fuente), así que "
                    "no se puede actualizar sola. Descarga la versión nueva o haz «git pull» y «./install.sh».")
        h = QLabel(hint)
        h.setWordWrap(True)
        h.setStyleSheet("color:gray")
        lay.addWidget(h)
        row = QHBoxLayout()
        skip = QPushButton("Omitir esta versión")
        skip.clicked.connect(lambda: self._done(self.SKIP))
        row.addWidget(skip)
        row.addStretch()
        later = QPushButton("Más tarde")
        later.clicked.connect(lambda: self._done(self.LATER))
        row.addWidget(later)
        main = QPushButton("Instalar ahora" if can_install else "Abrir página de descarga")
        main.setDefault(True)
        main.clicked.connect(lambda: self._done(self.INSTALL))
        row.addWidget(main)
        lay.addLayout(row)

    def _done(self, choice):
        self.choice = choice
        self.accept()


class _Progress(QObject):
    changed = Signal(int, int)


class UpdateManager(QObject):
    """Comprueba al arrancar y cada 6 horas (si está activado en Ajustes)."""

    def __init__(self, window):
        super().__init__(window)
        self.w = window
        self.config = window.config
        self.busy = False
        self.available: updater.Release | None = None
        self.notified: set = set()
        self.button = QPushButton()
        self.button.setFlat(True)
        self.button.setStyleSheet("QPushButton{color:#2e9d3a;font-weight:bold;padding:0 8px;}")
        self.button.clicked.connect(lambda: self._offer(self.available, manual=True))
        self.button.hide()
        window.statusBar().addPermanentWidget(self.button)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.check(manual=False))
        self.timer.start(CHECK_INTERVAL_MS)
        QTimer.singleShot(FIRST_CHECK_DELAY_MS, lambda: self.check(manual=False))

    def enabled(self) -> bool:
        return bool(self.config.get("general", "check_updates", True))

    # ------------------------------------------------------------------ comprobación
    def check(self, manual: bool):
        if self.busy or (not manual and not self.enabled()):
            return
        self.busy = True

        def done(rel: updater.Release):
            self.busy = False
            if not updater.is_newer(rel.version):
                self.available = None
                self.button.hide()
                if manual:
                    QMessageBox.information(self.w, "Actualizaciones",
                                            f"Tienes la última versión ({__version__}).")
                return
            self.available = rel
            self.button.setText(f"⬆ Versión {rel.version} disponible")
            self.button.show()
            skipped = self.config.get("general", "skip_version", "") == rel.version
            if manual or (not skipped and rel.version not in self.notified):
                self.notified.add(rel.version)
                self._offer(rel, manual)

        def err(exc):
            self.busy = False
            log.warning("Comprobación de actualizaciones fallida: %s", exc)
            if manual:
                QMessageBox.warning(self.w, "Actualizaciones", str(exc))
        run_async(updater.fetch_latest, done, err, self.w)

    def _offer(self, rel, manual: bool):
        if rel is None:
            return
        if QApplication.activeModalWidget() is not None and not manual:
            QTimer.singleShot(60 * 1000, lambda: self._offer(rel, manual))   # no interrumpir otro diálogo
            return
        can_install = updater.installed_from_deb()
        dlg = UpdateDialog(self.w, rel, can_install)
        dlg.exec()
        if dlg.choice == UpdateDialog.SKIP:
            self.config.set("general", "skip_version", rel.version)
            self.config.save()
        elif dlg.choice == UpdateDialog.INSTALL:
            if can_install:
                self._download_and_install(rel)
            else:
                QDesktopServices.openUrl(QUrl(rel.page_url))

    # ------------------------------------------------------------------ instalación
    def _download_and_install(self, rel: updater.Release):
        progress = QProgressDialog(f"Descargando RabbitSVN {rel.version}…", None, 0, 100, self.w)
        progress.setWindowTitle("Actualización")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setValue(0)
        sig = _Progress()
        sig.changed.connect(lambda d, t: progress.setValue(int(d * 100 / t)) if t else None)
        dest = private_temp_dir()

        def done(path):
            progress.close()
            self._install(rel, path)

        def err(exc):
            progress.close()
            QMessageBox.critical(self.w, "Actualización", f"No se pudo descargar la actualización:\n{exc}")
        run_async(lambda: updater.download(rel, dest, sig.changed.emit), done, err, self.w)

    def _install(self, rel: updater.Release, path: str):
        try:
            cmd = updater.install_command(path)
        except updater.UpdateError as exc:
            QMessageBox.warning(self.w, "Actualización", str(exc))
            return

        def ok(dlg):
            self.button.hide()
            self.available = None
            if QMessageBox.question(dlg, "Actualización instalada",
                                    f"RabbitSVN {rel.version} se ha instalado.\n¿Reiniciar la aplicación ahora?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes:
                QProcess.startDetached("/usr/bin/rabbit-svn", [])
                QTimer.singleShot(0, lambda: (self.w.close(), QApplication.quit()))
        dlg = ActionDialog(self.w, self.w.ctx, f"Instalando RabbitSVN {rel.version}",
                           [lambda _c: Command(argv=cmd)], ok)
        dlg.setModal(True)
        dlg.start()
