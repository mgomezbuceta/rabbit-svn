"""Configuración persistente (~/.config/rabbit-svn/config.json) y proyectos.

Medidas de seguridad/robustez:
- Directorio 0700 y ficheros 0600 (configuración y registro).
- Escritura atómica (fichero temporal + fsync + rename) bajo bloqueo (fcntl) para que
  dos instancias abiertas no se pisen la lista de proyectos.
- Un fichero corrupto se renombra a config.json.corrupto-<fecha> en vez de sobrescribirse.
- Las contraseñas nunca se escriben aquí: van al llavero del sistema o solo a memoria.
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

from .svn.client import TRUST_FAILURES, Credentials, SvnClient, strip_url_credentials

try:
    import fcntl
except ImportError:  # pragma: no cover (no Linux)
    fcntl = None

APP_NAME = "rabbit-svn"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP_NAME)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "rabbit-svn.log")

KEYRING_SERVICE = "rabbit-svn"
# Backends de keyring que cifran de verdad. Se excluyen los de fichero en claro (keyrings.alt).
SAFE_KEYRING_MODULES = ("keyring.backends.SecretService", "keyring.backends.libsecret",
                        "keyring.backends.kwallet", "keyring.backends.macOS", "keyring.backends.Windows")

DEFAULTS = {
    "general": {
        "show_unversioned_files": True,
        "show_ignored_files": False,
        "enable_recursive": True,
        "enable_colorize": True,
        "enable_highlighting": True,
        "datetime_format": "%d/%m/%Y %H:%M",
        "default_commit_message": "",
        "switch_after_branch": True,
        "log_limit": 100,
        "auto_refresh_on_focus": True,
        "confirm_revert": True,
        "check_updates": True,
        "skip_version": "",
    },
    "external": {
        "diff_tool": "/usr/bin/meld" if os.path.exists("/usr/bin/meld") else "",
        "diff_tool_swap": False,
        "merge_tool": "/usr/bin/meld" if os.path.exists("/usr/bin/meld") else "",
        "file_opener": "xdg-open",
    },
    "svn": {
        "svn_binary": "svn",
        "svnadmin_binary": "svnadmin",
        "config_dir": "",
        "timeout": 300,
    },
    "cache": {
        "number_repositories": 30,
        "number_messages": 30,
        "recent_urls": [],
        "recent_messages": [],
    },
    "logging": {
        "type": "Archivo",       # Ninguno, Archivo, Consola, Ambos
        "level": "Error",        # Debug, Info, Warning, Error, Critical
    },
    "ui": {
        "last_project": "",
        "geometry": "",
        "state": "",
        "only_changes": False,
    },
    "projects": [],
}

# Secciones que pueden modificar varias instancias a la vez: se fusionan desde disco al guardar.
SHARED_KEYS = (("projects", None), ("cache", "recent_urls"), ("cache", "recent_messages"))


@dataclass
class Project:
    name: str
    wc_path: str = ""
    url: str = ""
    repo_root: str = ""
    username: str = ""
    remember_password: bool = False
    trust_failures: list = field(default_factory=list)
    no_auth_cache: bool = True
    notes: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @classmethod
    def from_dict(cls, d) -> Optional["Project"]:
        """Construye un proyecto tolerando campos ausentes o de tipo incorrecto."""
        if not isinstance(d, dict):
            return None
        defaults = cls(name="")
        kwargs = {}
        for name, f in cls.__dataclass_fields__.items():
            default = getattr(defaults, name)
            value = d.get(name, default)
            if not isinstance(value, type(default)):
                value = default
            kwargs[name] = value
        kwargs["trust_failures"] = [t for t in kwargs["trust_failures"] if t in TRUST_FAILURES]
        if not kwargs["id"] or not str(kwargs["id"]).isalnum():
            return None
        if not kwargs["name"]:
            kwargs["name"] = os.path.basename(kwargs["wc_path"].rstrip("/")) or "(sin nombre)"
        return cls(**kwargs)


# --------------------------------------------------------------- contraseñas
class PasswordStore:
    """Usa el llavero del sistema (Secret Service/KWallet) si está disponible y es seguro.

    Si no lo está, las contraseñas solo se guardan en memoria durante la sesión.
    """

    def __init__(self):
        self._session: dict = {}
        self._keyring = None
        self.backend_name = "ninguno"
        try:
            import keyring  # type: ignore
            kr = keyring.get_keyring()
            backends = getattr(kr, "backends", None) or [kr]
            safe = [b for b in backends if type(b).__module__.startswith(SAFE_KEYRING_MODULES)]
            if safe:
                self._keyring = safe[0]
                self.backend_name = f"{type(safe[0]).__module__}"
        except Exception:  # noqa: BLE001
            self._keyring = None

    @property
    def persistent(self) -> bool:
        return self._keyring is not None

    def get(self, project_id: str) -> str:
        if project_id in self._session:
            return self._session[project_id]
        if self._keyring:
            try:
                return self._keyring.get_password(KEYRING_SERVICE, project_id) or ""
            except Exception:  # noqa: BLE001
                logging.getLogger(APP_NAME).warning("No se pudo leer del llavero")
        return ""

    def set(self, project_id: str, password: str, persist: bool):
        self._session[project_id] = password
        if self._keyring:
            try:
                if persist and password:
                    self._keyring.set_password(KEYRING_SERVICE, project_id, password)
                else:
                    self.delete(project_id, session=False)
            except Exception:  # noqa: BLE001
                logging.getLogger(APP_NAME).warning("No se pudo escribir en el llavero")

    def forget_session(self, project_id: Optional[str] = None):
        if project_id is None:
            self._session.clear()
        else:
            self._session.pop(project_id, None)

    def delete(self, project_id: str, session: bool = True):
        if session:
            self._session.pop(project_id, None)
        if self._keyring:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, project_id)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------- utilidades de fichero
def ensure_private_dir(path: str):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        pass


def write_private_file(path: str, data: str):
    """Escritura atómica con permisos 0600 desde el primer byte."""
    ensure_private_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)


@contextlib.contextmanager
def file_lock(path: str):
    ensure_private_dir(os.path.dirname(path))
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --------------------------------------------------------------- configuración
class Config:
    def __init__(self, path: str = CONFIG_FILE):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        self.passwords = PasswordStore()
        self.load_warning = ""
        with file_lock(self.path):
            self._apply(self._read_disk(initial=True))

    # ---- lectura
    def _read_disk(self, initial: bool = False) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                stored = json.load(fh)
            if not isinstance(stored, dict):
                raise ValueError("la raíz no es un objeto")
            return stored
        except (OSError, ValueError) as exc:
            if initial:
                backup = f"{self.path}.corrupto-{time.strftime('%Y%m%d-%H%M%S')}"
                with contextlib.suppress(OSError):
                    os.replace(self.path, backup)
                self.load_warning = (f"La configuración estaba dañada ({exc}). Se ha guardado una copia en "
                                     f"{backup} y se usan valores por defecto.")
                logging.getLogger(APP_NAME).error(self.load_warning)
            return {}

    def _apply(self, stored: dict):
        """Mezcla lo leído con los valores por defecto, validando tipos."""
        for section, defaults in DEFAULTS.items():
            value = stored.get(section)
            if section == "projects":
                if isinstance(value, list):
                    projects = [Project.from_dict(p) for p in value]
                    self.data["projects"] = [asdict(p) for p in projects if p]
                continue
            if not isinstance(value, dict):
                continue
            for key, default in defaults.items():
                if key not in value:
                    continue
                v = value[key]
                if isinstance(default, bool):
                    ok = isinstance(v, bool)
                elif isinstance(default, int):
                    ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
                elif isinstance(default, list):
                    ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
                else:
                    ok = isinstance(v, str)
                if ok:
                    self.data[section][key] = v

    def load(self):
        with file_lock(self.path):
            self._apply(self._read_disk())

    # ---- escritura
    def save(self):
        """Guarda la configuración. Proyectos e historiales se toman de disco (pueden haber
        cambiado en otra instancia) salvo que la operación los modifique vía _mutate()."""
        with file_lock(self.path):
            disk = self._read_disk()
            merged = copy.deepcopy(self.data)
            if disk:
                tmp = Config.__new__(Config)
                tmp.data = copy.deepcopy(DEFAULTS)
                tmp._apply(disk)
                for section, key in SHARED_KEYS:
                    if key is None:
                        merged[section] = tmp.data[section]
                    else:
                        merged[section][key] = tmp.data[section][key]
                self.data.update({k: merged[k] for k in ("projects",)})
                self.data["cache"]["recent_urls"] = merged["cache"]["recent_urls"]
                self.data["cache"]["recent_messages"] = merged["cache"]["recent_messages"]
            write_private_file(self.path, json.dumps(merged, indent=2, ensure_ascii=False))

    def _mutate(self, fn):
        """Aplica fn(data) sobre la versión más reciente en disco, de forma atómica."""
        with file_lock(self.path):
            disk = self._read_disk()
            if disk:
                tmp = Config.__new__(Config)
                tmp.data = copy.deepcopy(DEFAULTS)
                tmp._apply(disk)
                for section, key in SHARED_KEYS:
                    if key is None:
                        self.data[section] = tmp.data[section]
                    else:
                        self.data[section][key] = tmp.data[section][key]
            fn(self.data)
            write_private_file(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))

    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value):
        self.data.setdefault(section, {})[key] = value

    # ---- proyectos
    @property
    def projects(self) -> list:
        return [p for p in (Project.from_dict(d) for d in self.data.get("projects", [])) if p]

    def project(self, project_id: str) -> Optional[Project]:
        for p in self.projects:
            if p.id == project_id:
                return p
        return None

    def save_project(self, project: Project):
        project.url = strip_url_credentials(project.url)
        project.repo_root = strip_url_credentials(project.repo_root)

        def fn(data):
            items = data.setdefault("projects", [])
            for i, p in enumerate(items):
                if p.get("id") == project.id:
                    items[i] = asdict(project)
                    break
            else:
                items.append(asdict(project))
        self._mutate(fn)

    def remove_project(self, project_id: str):
        self._mutate(lambda data: data.__setitem__(
            "projects", [p for p in data.get("projects", []) if p.get("id") != project_id]))
        self.passwords.delete(project_id)

    def project_for_path(self, path: str) -> Optional[Project]:
        path = os.path.realpath(path)
        best = None
        for p in self.projects:
            wc = os.path.realpath(p.wc_path) if p.wc_path else ""
            if wc and (path == wc or path.startswith(wc + os.sep)):
                if best is None or len(wc) > len(best.wc_path):
                    best = p
        return best

    # ---- historial
    def remember(self, key: str, value: str, limit_key: str):
        if not value:
            return
        limit = int(self.get("cache", limit_key, 30) or 30)

        def fn(data):
            items = [v for v in data["cache"].get(key, []) if v != value]
            items.insert(0, value)
            data["cache"][key] = items[:limit]
        self._mutate(fn)

    def remember_url(self, url: str):
        self.remember("recent_urls", strip_url_credentials(url.strip()), "number_repositories")

    def remember_message(self, msg: str):
        self.remember("recent_messages", msg, "number_messages")

    def clear_history(self):
        def fn(data):
            data["cache"]["recent_urls"] = []
            data["cache"]["recent_messages"] = []
        self._mutate(fn)

    # ---- cliente svn
    def client(self, project: Optional[Project] = None, password: Optional[str] = None) -> SvnClient:
        creds = Credentials()
        if project:
            creds.username = project.username
            if project.username:
                creds.password = password if password is not None else self.passwords.get(project.id)
            creds.trust_failures = [t for t in project.trust_failures if t in TRUST_FAILURES]
            creds.no_auth_cache = project.no_auth_cache
        timeout = int(self.get("svn", "timeout", 0) or 0) or None
        return SvnClient(creds, svn_bin=self.get("svn", "svn_binary", "svn"),
                         svnadmin_bin=self.get("svn", "svnadmin_binary", "svnadmin"),
                         config_dir=self.get("svn", "config_dir", ""), timeout=timeout)


def setup_logging(config: Config):
    logger = logging.getLogger(APP_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    level = getattr(logging, str(config.get("logging", "level", "Error")).upper(), logging.ERROR)
    logger.setLevel(level)
    kind = config.get("logging", "type", "Archivo")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if kind in ("Archivo", "Ambos"):
        try:
            ensure_private_dir(CONFIG_DIR)
            fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
            os.chmod(LOG_FILE, 0o600)
            # Rotación sencilla: evita que el registro crezca sin límite
            if os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".1")
                os.close(os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT, 0o600))
            fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass
    if kind in ("Consola", "Ambos"):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger
