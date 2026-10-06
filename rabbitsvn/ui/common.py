"""Utilidades compartidas por la interfaz: hilos, iconos de estado, widgets comunes."""
from __future__ import annotations

import atexit
import html
import os
import shutil
import subprocess
import tempfile
import traceback
from typing import Callable, Optional, Sequence

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox,
                               QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..svn.client import SvnError

# ------------------------------------------------------------------ estados
STATUS_LABELS = {
    "normal": "Sin cambios", "modified": "Modificado", "added": "Añadido", "deleted": "Eliminado",
    "unversioned": "Sin versionar", "missing": "Falta", "conflicted": "Conflicto",
    "replaced": "Reemplazado", "ignored": "Ignorado", "obstructed": "Obstruido",
    "external": "Externo", "incomplete": "Incompleto", "merged": "Fusionado", "none": "",
}

STATUS_COLORS = {
    "modified": "#1f6fd1", "added": "#2e9d3a", "deleted": "#c62828", "missing": "#c62828",
    "conflicted": "#e65100", "replaced": "#8e24aa", "unversioned": "#808080",
    "ignored": "#a0a0a0", "obstructed": "#e65100", "external": "#00838f",
    "incomplete": "#e65100", "normal": "#43a047", "locked": "#6d4c41",
}

STATUS_LETTER = {
    "modified": "M", "added": "A", "deleted": "D", "missing": "!", "conflicted": "C",
    "replaced": "R", "unversioned": "?", "ignored": "I", "obstructed": "~", "external": "X",
    "incomplete": "!", "normal": "", "none": "",
}

LOG_ACTIONS = {"A": "Añadido", "M": "Modificado", "D": "Eliminado", "R": "Reemplazado"}

DEPTHS = [("infinity", "Recursivo completo"), ("immediates", "Hijos inmediatos (incluye carpetas)"),
          ("files", "Solo ficheros hijos"), ("empty", "Solo este elemento")]

ACCEPT_OPTIONS = [
    ("postpone", "Posponer (marcar conflicto)"),
    ("working", "Usar la versión de trabajo"),
    ("base", "Usar la versión base"),
    ("mine-conflict", "Mía en las zonas en conflicto"),
    ("theirs-conflict", "Suya en las zonas en conflicto"),
    ("mine-full", "Mía completa"),
    ("theirs-full", "Suya completa"),
]

_icon_cache: dict = {}

# Extensiones que xdg-open podría ejecutar en lugar de mostrar
RISKY_EXTENSIONS = {".desktop", ".sh", ".bash", ".run", ".bin", ".appimage", ".exe", ".msi", ".bat", ".cmd",
                    ".jar", ".py", ".pl", ".rb", ".js", ".vbs", ".ps1", ".deb", ".rpm", ".flatpakref", ".command"}

_temp_dirs: list = []


def esc(text) -> str:
    """Escapa texto externo (autor, mensajes, URLs, stderr) antes de meterlo en un QLabel HTML."""
    return html.escape(str(text if text is not None else ""), quote=True)


def safe_filename(name: str, fallback: str = "fichero") -> str:
    """Nombre de fichero sin separadores ni componentes '..' (nombres que vienen del repositorio)."""
    from urllib.parse import unquote
    name = os.path.basename(unquote(name or "").replace("\\", "/").rstrip("/"))
    name = name.replace("\0", "").strip()
    if name in ("", ".", ".."):
        return fallback
    return name[:200]


def private_temp_dir() -> str:
    """Directorio temporal 0700 que se elimina al cerrar la aplicación."""
    d = tempfile.mkdtemp(prefix="rabbitsvn-")
    _temp_dirs.append(d)
    return d


def write_private_temp(data: bytes, name: str) -> str:
    d = private_temp_dir()
    path = os.path.join(d, safe_filename(name))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def save_bytes(parent, path: str, data: bytes) -> bool:
    """Guarda de forma atómica y avisa si falla."""
    tmp = f"{path}.{os.getpid()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        QMessageBox.warning(parent, "Guardar", f"No se pudo guardar {path}:\n{exc}")
        return False


@atexit.register
def cleanup_temp_dirs():
    for d in _temp_dirs:
        shutil.rmtree(d, ignore_errors=True)
    _temp_dirs.clear()


def status_key(entry) -> str:
    """Devuelve el estado 'efectivo' para pintar (contenido o propiedades)."""
    if entry is None:
        return "normal"
    if entry.is_conflicted:
        return "conflicted"
    if entry.item in ("normal", "none") and entry.props not in ("none", "normal"):
        return "modified"
    return entry.item


def status_icon(key: str, folder: bool = False) -> QIcon:
    ck = (key, folder)
    if ck in _icon_cache:
        return _icon_cache[ck]
    style = QApplication.style()
    base = style.standardIcon(QStyle.SP_DirIcon if folder else QStyle.SP_FileIcon).pixmap(20, 20)
    pm = QPixmap(22, 22)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, base)
    color = STATUS_COLORS.get(key)
    if color and key not in ("ignored",):
        p.setBrush(QColor(color))
        p.setPen(QColor("white"))
        p.drawEllipse(11, 11, 10, 10)
    p.end()
    icon = QIcon(pm)
    _icon_cache[ck] = icon
    return icon


def color_for(key: str) -> Optional[QColor]:
    c = STATUS_COLORS.get(key)
    return QColor(c) if c and key != "normal" else None


def fmt_date(dt, config=None) -> str:
    if dt is None:
        return ""
    fmt = (config.get("general", "datetime_format", "") if config else "") or "%d/%m/%Y %H:%M"
    try:
        return dt.strftime(fmt)
    except ValueError:
        return dt.isoformat(sep=" ", timespec="minutes")


def human_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ------------------------------------------------------------------ hilos
class _Signals(QObject):
    done = Signal(object)
    error = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self.fn = fn
        self.signals = signals

    @Slot()
    def run(self):
        try:
            res = self.fn()
        except Exception as exc:  # noqa: BLE001
            exc._tb = traceback.format_exc()  # type: ignore[attr-defined]
            self.signals.error.emit(exc)
        else:
            self.signals.done.emit(res)


_live_signals: set = set()


def run_async(fn: Callable, on_done: Callable = None, on_error: Callable = None, parent: QWidget = None):
    """Ejecuta `fn` en un hilo del pool y llama a los callbacks en el hilo de la UI."""
    sig = _Signals()
    _live_signals.add(sig)

    def finish(cb, value):
        _live_signals.discard(sig)
        # Si la ventana que lanzó la tarea ya se cerró, se descarta el resultado
        if parent is not None:
            try:
                import shiboken6
                if not shiboken6.isValid(parent):
                    return
            except ImportError:
                pass
        if cb:
            try:
                cb(value)
            except RuntimeError as exc:  # widget destruido mientras tanto
                if "already deleted" not in str(exc):
                    raise
    sig.done.connect(lambda r: finish(on_done, r))
    sig.error.connect(lambda e: finish(on_error or (lambda ex: show_error(parent, ex)), e))
    QThreadPool.globalInstance().start(_Task(fn, sig))


def show_error(parent, exc, title: str = "Error de SVN"):
    if isinstance(exc, SvnError):
        text = exc.stderr or str(exc)
    else:
        text = str(exc) or exc.__class__.__name__
    box = QMessageBox(QMessageBox.Critical, title, text[:2000], QMessageBox.Ok, parent)
    box.setTextFormat(Qt.PlainText)
    tb = getattr(exc, "_tb", None)
    if isinstance(exc, SvnError):
        box.setDetailedText(" ".join(a for a in exc.argv if a) + "\n\n" + exc.stderr)
    elif tb:
        box.setDetailedText(tb)
    box.exec()


def busy(widget: QWidget, on: bool):
    if on:
        QApplication.setOverrideCursor(Qt.WaitCursor)
    else:
        QApplication.restoreOverrideCursor()


def confirm(parent, text: str, title: str = "Confirmar") -> bool:
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) == QMessageBox.Yes


def open_path(path: str, config=None, untrusted: bool = False):
    """Abre con la aplicación asociada. `untrusted`: el fichero viene del repositorio."""
    opener = (config.get("external", "file_opener", "xdg-open") if config else "xdg-open") or "xdg-open"
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    if os.path.isfile(path) and (ext in RISKY_EXTENSIONS or (untrusted and os.access(path, os.X_OK))):
        if QMessageBox.warning(None, "Abrir fichero",
                               f"«{os.path.basename(path)}» es un tipo de fichero que podría ejecutarse "
                               "al abrirlo.\n¿Abrirlo igualmente?",
                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
    try:
        subprocess.Popen([opener, path], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        QMessageBox.warning(None, "Abrir", f"No se pudo abrir {path}:\n{exc}")


def launch_tool(argv: Sequence[str]) -> bool:
    try:
        subprocess.Popen(list(argv), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError as exc:
        QMessageBox.warning(None, "Herramienta externa", f"No se pudo lanzar {argv[0]}:\n{exc}")
        return False


def mono_font() -> QFont:
    f = QFont("Monospace")
    f.setStyleHint(QFont.TypeWriter)
    return f


# ------------------------------------------------------------------ widgets
class PathEdit(QWidget):
    """Campo de texto + botón examinar (carpeta o fichero)."""

    def __init__(self, mode: str = "dir", text: str = "", parent=None, filter_: str = ""):
        super().__init__(parent)
        self.mode = mode
        self.filter = filter_
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(text)
        btn = QPushButton("Examinar…")
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def _browse(self):
        start = self.edit.text() or os.path.expanduser("~")
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta", start)
        elif self.mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, "Guardar como", start, self.filter)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Seleccionar fichero", start, self.filter)
        if path:
            self.edit.setText(path)

    def text(self) -> str:
        return os.path.expanduser(self.edit.text().strip())

    def setText(self, t: str):
        self.edit.setText(t)


class UrlCombo(QComboBox):
    """Combo editable con las URLs recientes."""

    def __init__(self, config, text: str = "", parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.addItems(config.get("cache", "recent_urls", []) or [])
        self.setCurrentText(text)
        self.setMinimumWidth(420)

    def text(self) -> str:
        return self.currentText().strip().rstrip("/")

    def setText(self, t: str):
        self.setCurrentText(t)


class RevisionWidget(QWidget):
    """Selector de revisión: HEAD / número / fecha / (opcional) BASE o copia de trabajo."""

    def __init__(self, allow_working: bool = False, allow_base: bool = False, parent=None,
                 show_log_cb: Optional[Callable] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.head = QRadioButton("HEAD")
        self.head.setChecked(True)
        lay.addWidget(self.head)
        self.working = QRadioButton("Copia de trabajo") if allow_working else None
        if self.working:
            lay.addWidget(self.working)
        self.base = QRadioButton("BASE") if allow_base else None
        if self.base:
            lay.addWidget(self.base)
        self.number = QRadioButton("Revisión:")
        lay.addWidget(self.number)
        self.spin = QSpinBox()
        self.spin.setRange(0, 2_000_000_000)
        self.spin.setEnabled(False)
        self.number.toggled.connect(self.spin.setEnabled)
        lay.addWidget(self.spin)
        self.date = QRadioButton("Fecha:")
        lay.addWidget(self.date)
        self.date_edit = QLineEdit()
        self.date_edit.setPlaceholderText("AAAA-MM-DD [HH:MM]")
        self.date_edit.setEnabled(False)
        self.date.toggled.connect(self.date_edit.setEnabled)
        lay.addWidget(self.date_edit)
        if show_log_cb:
            b = QPushButton("Ver log…")
            b.clicked.connect(show_log_cb)
            lay.addWidget(b)
        lay.addStretch()

    def set_revision(self, rev):
        if rev is None or rev == "HEAD":
            self.head.setChecked(True)
        else:
            self.number.setChecked(True)
            self.spin.setValue(int(rev))

    def value(self) -> Optional[str]:
        """None significa 'copia de trabajo' (sin -r)."""
        if self.working and self.working.isChecked():
            return None
        if self.base and self.base.isChecked():
            return "BASE"
        if self.number.isChecked():
            return str(self.spin.value())
        if self.date.isChecked() and self.date_edit.text().strip():
            return "{" + self.date_edit.text().strip() + "}"
        return "HEAD"


def combo_from(options, current: Optional[str] = None) -> QComboBox:
    cb = QComboBox()
    for value, label in options:
        cb.addItem(label, value)
    if current is not None:
        idx = cb.findData(current)
        if idx >= 0:
            cb.setCurrentIndex(idx)
    return cb


class MessageEdit(QWidget):
    """Mensaje de commit/log con selector de mensajes recientes."""

    def __init__(self, config, text: str = "", parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(QLabel("Mensaje:"))
        top.addStretch()
        self.recent = QComboBox()
        self.recent.addItem("Mensajes anteriores…")
        for m in config.get("cache", "recent_messages", []) or []:
            self.recent.addItem(m.splitlines()[0][:80] if m else "", m)
        self.recent.activated.connect(self._pick)
        top.addWidget(self.recent)
        lay.addLayout(top)
        self.edit = QPlainTextEdit(text or config.get("general", "default_commit_message", ""))
        self.edit.setFont(mono_font())
        lay.addWidget(self.edit)

    def _pick(self, idx):
        if idx > 0:
            self.edit.setPlainText(self.recent.itemData(idx))

    def text(self) -> str:
        return self.edit.toPlainText().strip()


class FileTable(QTreeWidget):
    """Lista de ficheros con casilla, estado y extensión (diálogos commit/add/revert...)."""

    COLS = ["Ruta", "Extensión", "Estado", "Propiedades"]

    def __init__(self, base_dir: str, parent=None, checkable: bool = True):
        super().__init__(parent)
        self.base_dir = base_dir
        self.checkable = checkable
        self.setColumnCount(len(self.COLS))
        self.setHeaderLabels(self.COLS)
        self.setRootIsDecorated(False)
        self.setSortingEnabled(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.entries: dict = {}

    def rel(self, path: str) -> str:
        try:
            r = os.path.relpath(path, self.base_dir)
        except ValueError:
            return path
        return path if r.startswith("..") else r

    def set_entries(self, entries, checked: Callable = lambda e: True):
        self.clear()
        self.entries = {}
        for e in entries:
            key = status_key(e)
            it = QTreeWidgetItem([self.rel(e.path) or ".", os.path.splitext(e.path)[1],
                                  STATUS_LABELS.get(key, key),
                                  STATUS_LABELS.get(e.props, e.props) if e.props not in ("none", "normal") else ""])
            it.setData(0, Qt.UserRole, e.path)
            it.setIcon(0, status_icon(key, os.path.isdir(e.path)))
            col = color_for(key)
            if col:
                for c in range(len(self.COLS)):
                    it.setForeground(c, col)
            if self.checkable:
                it.setCheckState(0, Qt.Checked if checked(e) else Qt.Unchecked)
            self.addTopLevelItem(it)
            self.entries[e.path] = e
        self.sortItems(0, Qt.AscendingOrder)
        self.resizeColumnToContents(2)

    def items(self):
        for i in range(self.topLevelItemCount()):
            yield self.topLevelItem(i)

    def checked_paths(self) -> list:
        return [it.data(0, Qt.UserRole) for it in self.items() if it.checkState(0) == Qt.Checked]

    def selected_paths(self) -> list:
        return [it.data(0, Qt.UserRole) for it in self.selectedItems()]

    def set_all(self, state: bool):
        for it in self.items():
            it.setCheckState(0, Qt.Checked if state else Qt.Unchecked)


def check_all_row(table: FileTable) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    cb = QCheckBox("Seleccionar / deseleccionar todo")
    cb.setTristate(False)
    cb.setChecked(True)
    cb.toggled.connect(table.set_all)
    lay.addWidget(cb)
    lay.addStretch()
    w.check = cb  # type: ignore[attr-defined]
    return w


def std_buttons(dialog: QDialog, ok_text: str = "Aceptar") -> QDialogButtonBox:
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Ok).setText(ok_text)
    bb.button(QDialogButtonBox.Cancel).setText("Cancelar")
    bb.accepted.connect(dialog.accept)
    bb.rejected.connect(dialog.reject)
    return bb
