"""Cliente SVN basado en el binario `svn` (salida --xml).

Cada operación se construye como una lista de argumentos (`build`) para poder
ejecutarla de forma síncrona (`run`) o en streaming desde la interfaz (QProcess).
La contraseña nunca va en la línea de órdenes: se pasa por stdin con
`--password-from-stdin` (requiere svn >= 1.10).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .models import (BlameLine, DiffSummaryEntry, InfoEntry, ListEntry, LockInfo,
                     LogEntry, LogPath, StatusEntry, parse_svn_date)

TRUST_FAILURES = ["unknown-ca", "cn-mismatch", "expired", "not-yet-valid", "other"]

# Autorización fallida / sin más credenciales. (E170013 es "no se puede conectar":
# acompaña a muchos errores de red y NO implica credenciales incorrectas.)
AUTH_ERROR_CODES = ("E170001", "E215004")

ALLOWED_SCHEMES = ("file", "svn", "svn+ssh", "http", "https")
_REV_RE = re.compile(r"^(HEAD|BASE|COMMITTED|PREV|\d+|\{\d{4}-\d{2}-\d{2}( \d{1,2}:\d{2}(:\d{2})?)?\})$")
_REV_RANGE_RE = re.compile(r"^(-?\d+|\d+[-:]\d+)$")
MAX_XML_BYTES = 512 * 1024 * 1024


class ValidationError(ValueError):
    """Dato de entrada rechazado antes de llegar a svn."""


def validate_url(url: str) -> str:
    """Comprueba esquema permitido y que no lleve contraseña embebida (user:pass@)."""
    url = (url or "").strip()
    if not url:
        raise ValidationError("La URL está vacía.")
    if any(c in url for c in "\n\r\0"):
        raise ValidationError("La URL contiene caracteres no válidos.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValidationError(f"Esquema no permitido: «{parts.scheme or '(ninguno)'}». "
                              f"Usa {', '.join(s + '://' for s in ALLOWED_SCHEMES)}")
    if parts.password is not None:
        raise ValidationError("No incluyas la contraseña en la URL; usa los campos de usuario y contraseña.")
    if parts.scheme != "file" and not parts.hostname:
        raise ValidationError("La URL no tiene servidor.")
    return url


def strip_url_credentials(url: str) -> str:
    """Elimina 'usuario:contraseña@' de una URL (para guardarla en el historial)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def validate_revision(rev: Optional[str]) -> Optional[str]:
    if rev is None:
        return None
    rev = str(rev).strip()
    if not _REV_RE.match(rev) and not re.match(r"^(\d+|HEAD|BASE|PREV|COMMITTED):(\d+|HEAD|BASE|PREV|COMMITTED)$", rev):
        raise ValidationError(f"Revisión no válida: «{rev}»")
    return rev


def validate_path(path: str) -> str:
    if not path or "\0" in path:
        raise ValidationError("Ruta vacía o no válida.")
    return path

class SvnError(Exception):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str, stdout: str = ""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        self.stdout = stdout or ""
        super().__init__(self.stderr or f"svn terminó con código {returncode}")

    @property
    def is_auth_error(self) -> bool:
        low = self.stderr.lower()
        return (any(c in self.stderr for c in AUTH_ERROR_CODES)
                or "authentication failed" in low or "authorization failed" in low
                or "autenticación" in low or "autorización" in low)

    @property
    def is_cert_error(self) -> bool:
        return "E230001" in self.stderr or "certificate" in self.stderr.lower()


@dataclass
class Credentials:
    username: str = ""
    password: str = ""
    trust_failures: list = field(default_factory=list)
    no_auth_cache: bool = False


@dataclass
class Command:
    argv: list
    stdin: Optional[str] = None
    cwd: Optional[str] = None
    temp_files: list = field(default_factory=list)

    def cleanup(self):
        for f in self.temp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        self.temp_files.clear()

    def display(self) -> str:
        """Representación legible. La contraseña va por stdin, nunca en argv."""
        import shlex
        return " ".join(shlex.quote(a) for a in self.argv)

    def __del__(self):  # red de seguridad: no dejar ficheros temporales
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001
            pass


def svn_env() -> dict:
    env = dict(os.environ)
    # svn nunca debe abrir un editor ni pedir datos por terminal
    for var in ("SVN_EDITOR", "VISUAL", "EDITOR"):
        env.pop(var, None)
    # svn necesita un locale UTF-8 para manejar rutas no ASCII
    if not env.get("LC_ALL") and not env.get("LC_CTYPE") and "UTF-8" not in env.get("LANG", "").upper():
        env["LC_CTYPE"] = "C.UTF-8"
    return env


def _text(el: Optional[ET.Element], tag: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(tag)
    return found.text or default if found is not None and found.text is not None else default


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lock(el: Optional[ET.Element]) -> Optional[LockInfo]:
    if el is None:
        return None
    return LockInfo(token=_text(el, "token"), owner=_text(el, "owner"),
                    comment=_text(el, "comment"), created=parse_svn_date(_text(el, "created")))


def peg(target: str, rev: Optional[str]) -> str:
    """Añade revisión peg (url@rev). Escapa '@' existentes en rutas locales."""
    if rev:
        return f"{target}@{rev}"
    if "@" in target and not target.endswith("@"):
        return target + "@"
    return target


class SvnClient:
    def __init__(self, credentials: Optional[Credentials] = None, svn_bin: str = "svn",
                 svnadmin_bin: str = "svnadmin", config_dir: str = "", timeout: Optional[int] = None):
        self.creds = credentials or Credentials()
        self.svn_bin = svn_bin or "svn"
        self.svnadmin_bin = svnadmin_bin or "svnadmin"
        self.config_dir = config_dir
        self.timeout = timeout

    # ------------------------------------------------------------------ base
    def available(self) -> bool:
        return shutil.which(self.svn_bin) is not None

    def version(self) -> str:
        try:
            res = subprocess.run([self.svn_bin, "--version", "--quiet"], capture_output=True, text=True,
                                 timeout=15, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return res.stdout.strip()

    def build(self, *args: str, cwd: Optional[str] = None, auth: bool = True) -> Command:
        argv = [self.svn_bin, args[0], "--non-interactive"]
        stdin = None
        if self.config_dir:
            argv += ["--config-dir", self.config_dir]
        if auth:
            if self.creds.username:
                argv += ["--username", self.creds.username]
            if self.creds.password:
                argv.append("--password-from-stdin")
                stdin = self.creds.password + "\n"
            if self.creds.no_auth_cache:
                argv.append("--no-auth-cache")
            if self.creds.trust_failures:
                argv.append("--trust-server-cert-failures=" + ",".join(self.creds.trust_failures))
        argv += [a for a in args[1:] if a is not None]
        for a in argv:
            if "\0" in a:
                raise ValidationError("Argumento con carácter nulo.")
        return Command(argv=argv, stdin=stdin, cwd=cwd)

    def execute(self, cmd: Command, binary: bool = False):
        try:
            res = subprocess.run(cmd.argv, input=(cmd.stdin.encode() if cmd.stdin else None),
                                 stdin=None if cmd.stdin else subprocess.DEVNULL,
                                 capture_output=True, cwd=cmd.cwd, env=svn_env(), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise SvnError(cmd.argv, -1, f"La operación superó el tiempo máximo ({self.timeout} s). "
                                         "Ajústalo en Ajustes → Subversion.") from None
        except OSError as exc:
            raise SvnError(cmd.argv, -1, f"No se pudo ejecutar {cmd.argv[0]}: {exc}") from None
        finally:
            cmd.cleanup()
        if res.returncode != 0:
            raise SvnError(cmd.argv, res.returncode,
                           res.stderr.decode("utf-8", "replace"), res.stdout.decode("utf-8", "replace"))
        return res.stdout if binary else res.stdout.decode("utf-8", "replace")

    def run(self, *args: str, cwd: Optional[str] = None) -> str:
        return self.execute(self.build(*args, cwd=cwd))

    def _xml(self, *args: str, cwd: Optional[str] = None) -> ET.Element:
        args = list(args)
        if "--" in args:  # --xml debe ir antes del separador de posicionales
            args.insert(args.index("--"), "--xml")
        else:
            args.append("--xml")
        out = self.run(*args, cwd=cwd)
        if len(out) > MAX_XML_BYTES:
            raise SvnError(args, -1, "Respuesta XML demasiado grande.")
        # svn nunca genera DTD; rechazarla evita expansión de entidades (billion laughs).
        if "<!DOCTYPE" in out[:4096] or "<!ENTITY" in out:
            raise SvnError(args, -1, "Respuesta XML inesperada (contiene DTD/entidades).")
        try:
            return ET.fromstring(out)
        except ET.ParseError as exc:
            raise SvnError(args, -1, f"No se pudo interpretar la salida XML de svn: {exc}") from None

    @staticmethod
    def _temp(content: str, suffix: str = ".txt") -> str:
        fd, path = tempfile.mkstemp(prefix="rabbitsvn-", suffix=suffix)  # 0600
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _msg_args(self, message: Optional[str]):
        """Mensaje de log vía fichero temporal (-F): no aparece en `ps` ni en el registro."""
        if message is None:
            return [], []
        mf = self._temp(message)
        return ["-F", mf, "--encoding", "UTF-8"], [mf]

    def _with_targets(self, cmd: Command, paths: Iterable[str]) -> Command:
        """Pasa muchas rutas mediante --targets para no superar ARG_MAX."""
        paths = [validate_path(p) for p in paths]
        if not paths:
            raise ValidationError("No hay elementos seleccionados.")
        if len(paths) > 50 and not any("\n" in p or "\r" in p for p in paths):
            tf = self._temp("\n".join(peg(p, None) for p in paths))
            cmd.temp_files.append(tf)
            cmd.argv += ["--targets", tf]
        else:
            cmd.argv += ["--"] + [peg(p, None) for p in paths]
        return cmd

    # ------------------------------------------------------------------ consultas
    def info(self, target: str, revision: Optional[str] = None, depth: str = "empty") -> list:
        args = ["info", "--depth", depth]
        if revision:
            args += ["-r", validate_revision(revision)]
        root = self._xml(*args, "--", peg(target, None))
        out = []
        for e in root.findall("entry"):
            repo = e.find("repository")
            wc = e.find("wc-info")
            commit = e.find("commit")
            conflict_files = {}
            if wc is not None:
                for c in wc.findall("conflict"):
                    for tag in ("prev-base-file", "prev-wc-file", "cur-base-file"):
                        v = _text(c, tag)
                        if v:
                            conflict_files[tag] = v
            out.append(InfoEntry(
                path=e.get("path", ""), kind=e.get("kind", ""), url=_text(e, "url"),
                relative_url=_text(e, "relative-url"), repo_root=_text(repo, "root"),
                uuid=_text(repo, "uuid"), revision=_int(e.get("revision")),
                last_changed_rev=_int(commit.get("revision")) if commit is not None else None,
                last_changed_author=_text(commit, "author"),
                last_changed_date=parse_svn_date(_text(commit, "date")),
                wc_root=_text(wc, "wcroot-abspath"), schedule=_text(wc, "schedule"),
                depth=_text(wc, "depth"), lock=_lock(e.find("lock")),
                conflict_files=conflict_files))
        return out

    def is_working_copy(self, path: str) -> bool:
        try:
            return bool(self.info(path))
        except SvnError:
            return False

    def status(self, paths: Sequence[str], show_updates: bool = False, depth: str = "infinity",
               verbose: bool = False, no_ignore: bool = False, ignore_externals: bool = False) -> list:
        args = ["status", "--depth", depth]
        if show_updates:
            args.append("-u")
        if verbose:
            args.append("-v")
        if no_ignore:
            args.append("--no-ignore")
        if ignore_externals:
            args.append("--ignore-externals")
        root = self._xml(*args, "--", *[peg(validate_path(p), None) for p in paths])
        out = []

        def parse_entry(e, changelist=None):
            wc = e.find("wc-status")
            rs = e.find("repos-status")
            commit = wc.find("commit") if wc is not None else None
            out.append(StatusEntry(
                path=e.get("path", ""),
                item=wc.get("item", "normal") if wc is not None else "normal",
                props=wc.get("props", "none") if wc is not None else "none",
                revision=_int(wc.get("revision")) if wc is not None else None,
                changed_rev=_int(commit.get("revision")) if commit is not None else None,
                author=_text(commit, "author"), date=parse_svn_date(_text(commit, "date")),
                copied=wc is not None and wc.get("copied") == "true",
                switched=wc is not None and wc.get("switched") == "true",
                tree_conflicted=wc is not None and wc.get("tree-conflicted") == "true",
                wc_locked=wc is not None and wc.get("wc-locked") == "true",
                lock=_lock(wc.find("lock")) if wc is not None else None,
                repos_item=rs.get("item") if rs is not None else None,
                repos_props=rs.get("props") if rs is not None else None,
                repos_lock=_lock(rs.find("lock")) if rs is not None else None,
                changelist=changelist))

        for target in root.findall("target"):
            for e in target.findall("entry"):
                parse_entry(e)
        for cl in root.findall("changelist"):
            for e in cl.findall("entry"):
                parse_entry(e, cl.get("name"))
        return out

    def head_revision(self, target: str) -> Optional[int]:
        infos = self.info(target, revision="HEAD")
        return infos[0].revision if infos else None

    def log(self, target: str, limit: Optional[int] = 100, revision: Optional[str] = None,
            verbose: bool = True, stop_on_copy: bool = False, search: str = "",
            use_merge_history: bool = False) -> list:
        args = ["log"]
        if verbose:
            args.append("-v")
        if limit:
            args += ["-l", str(limit)]
        if revision:
            args += ["-r", validate_revision(revision)]
        if stop_on_copy:
            args.append("--stop-on-copy")
        if search:
            args += ["--search", search]
        if use_merge_history:
            args.append("-g")
        root = self._xml(*args, "--", peg(target, None))
        out = []
        for e in root.findall("logentry"):
            paths = []
            pe = e.find("paths")
            if pe is not None:
                for p in pe.findall("path"):
                    paths.append(LogPath(
                        path=p.text or "", action=p.get("action", ""), kind=p.get("kind", ""),
                        copyfrom_path=p.get("copyfrom-path", ""), copyfrom_rev=_int(p.get("copyfrom-rev")),
                        text_mods=p.get("text-mods") == "true", prop_mods=p.get("prop-mods") == "true"))
            out.append(LogEntry(revision=int(e.get("revision", 0)), author=_text(e, "author"),
                                date=parse_svn_date(_text(e, "date")), message=_text(e, "msg"),
                                paths=paths))
        return out

    def list(self, url: str, revision: Optional[str] = None, depth: str = "immediates") -> list:
        args = ["list", "--depth", depth]
        if revision:
            args += ["-r", validate_revision(revision)]
        root = self._xml(*args, "--", peg(url, None))
        out = []
        for lst in root.findall("list"):
            for e in lst.findall("entry"):
                commit = e.find("commit")
                out.append(ListEntry(
                    name=_text(e, "name"), kind=e.get("kind", ""), size=_int(_text(e, "size", None)),
                    revision=_int(commit.get("revision")) if commit is not None else None,
                    author=_text(commit, "author"), date=parse_svn_date(_text(commit, "date")),
                    lock=_lock(e.find("lock"))))
        return out

    def cat(self, target: str, revision: Optional[str] = None) -> bytes:
        args = ["cat"]
        if revision:
            args += ["-r", validate_revision(revision)]
        return self.execute(self.build(*args, "--", peg(target, None)), binary=True)

    def blame(self, target: str, revision: Optional[str] = None, use_merge_history: bool = False) -> list:
        args = ["blame"]
        if revision:
            args += ["-r", validate_revision(revision)]
        if use_merge_history:
            args.append("-g")
        root = self._xml(*args, "--", peg(target, None))
        if not revision and os.path.isfile(target):
            with open(target, "rb") as fh:
                raw = fh.read()
        else:
            raw = self.cat(target, revision)
        content = raw.decode("utf-8", "replace").splitlines()
        out = []
        for t in root.findall("target"):
            for e in t.findall("entry"):
                n = int(e.get("line-number", 0))
                commit = e.find("commit")
                out.append(BlameLine(
                    line_no=n,
                    revision=_int(commit.get("revision")) if commit is not None else None,
                    author=_text(commit, "author"), date=parse_svn_date(_text(commit, "date")),
                    text=content[n - 1] if 0 < n <= len(content) else ""))
        return out

    def diff(self, targets: Sequence[str] = (), old: Optional[str] = None, new: Optional[str] = None,
             revision: Optional[str] = None, change: Optional[int] = None,
             ignore_whitespace: bool = False, cwd: Optional[str] = None,
             depth: Optional[str] = None, git_format: bool = False) -> str:
        args = ["diff"]
        if revision:
            args += ["-r", validate_revision(revision)]
        if change is not None:
            args += ["-c", str(change)]
        if old:
            args += ["--old", old]
        if new:
            args += ["--new", new]
        if ignore_whitespace:
            args += ["-x", "-w"]
        if depth:
            args += ["--depth", depth]
        if git_format:
            args.append("--git")
        args.append("--internal-diff")
        if targets:
            args += ["--"] + [peg(t, None) for t in targets]
        return self.run(*args, cwd=cwd)

    def diff_summarize(self, old: str, new: str) -> list:
        root = self._xml("diff", "--summarize", "--old", old, "--new", new)
        out = []
        for p in root.iter("path"):
            out.append(DiffSummaryEntry(path=p.text or "", item=p.get("item", ""),
                                        props=p.get("props", ""), kind=p.get("kind", "")))
        return out

    def proplist(self, target: str, revision: Optional[str] = None, revprop: bool = False) -> dict:
        args = ["proplist", "-v"]
        if revprop:
            args.append("--revprop")
        if revision:
            args += ["-r", validate_revision(revision)]
        root = self._xml(*args, "--", peg(target, None))
        props = {}
        for p in root.iter("property"):
            props[p.get("name")] = p.text or ""
        return props

    def propget(self, name: str, target: str) -> str:
        try:
            return self.run("propget", "--no-newline", "--", name, peg(target, None))
        except SvnError as e:
            if "W200017" in e.stderr or "E200017" in e.stderr:
                return ""
            raise

    def propset(self, name: str, value: str, target: str, recursive: bool = False,
                revprop: bool = False, revision: Optional[str] = None) -> str:
        tf = self._temp(value)
        args = ["propset", "-F", tf, "--encoding", "UTF-8"] if name in ("svn:log",) else ["propset", "-F", tf]
        if recursive:
            args += ["--depth", "infinity"]
        if revprop:
            args += ["--revprop", "-r", validate_revision(str(revision))]
        cmd = self.build(*args, "--", name, peg(target, None))
        cmd.temp_files.append(tf)
        return self.execute(cmd)

    def propdel(self, name: str, target: str, recursive: bool = False,
                revprop: bool = False, revision: Optional[str] = None) -> str:
        args = ["propdel"]
        if recursive:
            args += ["--depth", "infinity"]
        if revprop:
            args += ["--revprop", "-r", validate_revision(str(revision))]
        return self.run(*args, "--", name, peg(target, None))

    def auth_cache(self) -> str:
        return self.run("auth")

    # ------------------------------------------------------------------ operaciones (Command)
    def cmd_checkout(self, url: str, path: str, revision: Optional[str] = None,
                     depth: str = "infinity", ignore_externals: bool = False) -> Command:
        args = ["checkout", "--depth", depth]
        if revision:
            args += ["-r", validate_revision(revision)]
        if ignore_externals:
            args.append("--ignore-externals")
        return self.build(*args, "--", peg(validate_url(url), None), validate_path(path))

    def cmd_update(self, paths: Sequence[str], revision: Optional[str] = None,
                   depth: Optional[str] = None, set_depth: Optional[str] = None,
                   ignore_externals: bool = False, accept: str = "postpone") -> Command:
        args = ["update", "--accept", accept]
        if revision:
            args += ["-r", validate_revision(revision)]
        if depth:
            args += ["--depth", depth]
        if set_depth:
            args += ["--set-depth", set_depth]
        if ignore_externals:
            args.append("--ignore-externals")
        return self._with_targets(self.build(*args), paths)

    def cmd_commit(self, paths: Sequence[str], message: str, keep_locks: bool = False,
                   depth: str = "empty", include_externals: bool = False) -> Command:
        margs, tmp = self._msg_args(message)
        args = ["commit", *margs, "--depth", depth]
        if keep_locks:
            args.append("--no-unlock")
        if include_externals:
            args.append("--include-externals")
        cmd = self.build(*args)
        cmd.temp_files += tmp
        return self._with_targets(cmd, paths)

    def cmd_add(self, paths: Sequence[str], recursive: bool = True, force: bool = False,
                no_ignore: bool = False, parents: bool = True) -> Command:
        args = ["add", "--depth", "infinity" if recursive else "empty"]
        if force:
            args.append("--force")
        if no_ignore:
            args.append("--no-ignore")
        if parents:
            args.append("--parents")
        return self._with_targets(self.build(*args, auth=False), paths)

    def cmd_delete(self, paths: Sequence[str], keep_local: bool = False, force: bool = False,
                   message: Optional[str] = None) -> Command:
        args = ["delete"]
        if keep_local:
            args.append("--keep-local")
        if force:
            args.append("--force")
        margs, tmp = self._msg_args(message)
        cmd = self.build(*args, *margs, auth=message is not None)
        cmd.temp_files += tmp
        return self._with_targets(cmd, paths)

    def cmd_revert(self, paths: Sequence[str], recursive: bool = False,
                   remove_added: bool = False) -> Command:
        args = ["revert", "--depth", "infinity" if recursive else "empty"]
        if remove_added:
            args.append("--remove-added")
        return self._with_targets(self.build(*args, auth=False), paths)

    def cmd_move(self, src: str, dst: str, message: Optional[str] = None, parents: bool = True,
                 force: bool = False) -> Command:
        args = ["move"]
        if parents:
            args.append("--parents")
        if force:
            args.append("--force")
        margs, tmp = self._msg_args(message)
        cmd = self.build(*args, *margs, "--", peg(validate_path(src), None), validate_path(dst))
        cmd.temp_files += tmp
        return cmd

    def cmd_copy(self, src: str, dst: str, message: str, revision: Optional[str] = None,
                 parents: bool = True, pin_externals: bool = False) -> Command:
        margs, tmp = self._msg_args(message)
        args = ["copy", *margs]
        if parents:
            args.append("--parents")
        if pin_externals:
            args.append("--pin-externals")
        if revision:
            args += ["-r", validate_revision(revision)]
        if "://" in dst:
            validate_url(dst)
        cmd = self.build(*args, "--", peg(validate_path(src), None), validate_path(dst))
        cmd.temp_files += tmp
        return cmd

    def cmd_mkdir(self, urls: Sequence[str], message: Optional[str] = None, parents: bool = True) -> Command:
        args = ["mkdir"]
        if parents:
            args.append("--parents")
        margs, tmp = self._msg_args(message)
        cmd = self.build(*args, *margs, "--", *[validate_path(u) for u in urls])
        cmd.temp_files += tmp
        return cmd

    def cmd_switch(self, url: str, path: str, revision: Optional[str] = None,
                   depth: Optional[str] = None, ignore_ancestry: bool = False,
                   ignore_externals: bool = False, accept: str = "postpone") -> Command:
        args = ["switch", "--accept", accept]
        if revision:
            args += ["-r", validate_revision(revision)]
        if depth:
            args += ["--set-depth", depth]
        if ignore_ancestry:
            args.append("--ignore-ancestry")
        if ignore_externals:
            args.append("--ignore-externals")
        return self.build(*args, "--", peg(validate_url(url), None), validate_path(path))

    def cmd_merge(self, target: str, source: Optional[str] = None, revisions: Sequence[str] = (),
                  source2: Optional[str] = None, dry_run: bool = False, record_only: bool = False,
                  ignore_ancestry: bool = False, accept: str = "postpone",
                  depth: Optional[str] = None, allow_mixed: bool = False) -> Command:
        """Fusión automática (sin revisiones), por rangos (-r/-c) o entre dos árboles (source2)."""
        args = ["merge", "--accept", accept]
        if dry_run:
            args.append("--dry-run")
        if record_only:
            args.append("--record-only")
        if ignore_ancestry:
            args.append("--ignore-ancestry")
        if allow_mixed:
            args.append("--allow-mixed-revisions")
        if depth:
            args += ["--depth", depth]
        for r in revisions:
            r = r.strip()
            if not r:
                continue
            if not _REV_RANGE_RE.match(r):
                raise ValidationError(f"Revisión o rango no válido: «{r}» (ejemplos: 12, 15-18, -20)")
            if "-" in r.lstrip("-") or ":" in r:
                a, b = (int(x) for x in r.replace(":", "-").split("-", 1))
                if a < 1 or b < a:
                    raise ValidationError(f"Rango no válido: «{r}»")
                args += ["-r", f"{a - 1}:{b}"]
            else:
                if int(r) == 0:
                    raise ValidationError("La revisión 0 no se puede fusionar.")
                args += ["-c", r]
        if not source:
            raise ValidationError("Falta la URL de origen.")
        for src in (source, source2):
            if src and "://" in src:
                validate_url(src.rsplit("@", 1)[0] if "@" in src.rsplit("/", 1)[-1] else src)
        if source2:
            args += ["--", source, source2, validate_path(target)]
        else:
            args += ["--", source, validate_path(target)]
        return self.build(*args)

    def cmd_import(self, path: str, url: str, message: str, no_ignore: bool = False,
                   depth: str = "infinity") -> Command:
        margs, tmp = self._msg_args(message)
        args = ["import", *margs, "--depth", depth]
        if no_ignore:
            args.append("--no-ignore")
        cmd = self.build(*args, "--", validate_path(path), validate_url(url))
        cmd.temp_files += tmp
        return cmd

    def cmd_export(self, source: str, dest: str, revision: Optional[str] = None, force: bool = False,
                   ignore_externals: bool = False, native_eol: Optional[str] = None,
                   depth: str = "infinity") -> Command:
        args = ["export", "--depth", depth]
        if revision:
            args += ["-r", validate_revision(revision)]
        if force:
            args.append("--force")
        if ignore_externals:
            args.append("--ignore-externals")
        if native_eol:
            args += ["--native-eol", native_eol]
        return self.build(*args, "--", peg(validate_path(source), None), validate_path(dest))

    def cmd_relocate(self, path: str, to_url: str, from_url: Optional[str] = None) -> Command:
        validate_url(to_url)
        if from_url:
            return self.build("relocate", "--", from_url, to_url, validate_path(path))
        return self.build("relocate", "--", to_url, validate_path(path))

    def cmd_lock(self, paths: Sequence[str], message: str = "", force: bool = False) -> Command:
        margs, tmp = self._msg_args(message or None)
        args = ["lock", *margs]
        if force:
            args.append("--force")
        cmd = self.build(*args)
        cmd.temp_files += tmp
        return self._with_targets(cmd, paths)

    def cmd_unlock(self, paths: Sequence[str], force: bool = False) -> Command:
        args = ["unlock"]
        if force:
            args.append("--force")
        return self._with_targets(self.build(*args), paths)

    def cmd_cleanup(self, path: str, remove_unversioned: bool = False, remove_ignored: bool = False,
                    vacuum_pristines: bool = False, include_externals: bool = False) -> Command:
        args = ["cleanup"]
        if remove_unversioned:
            args.append("--remove-unversioned")
        if remove_ignored:
            args.append("--remove-ignored")
        if vacuum_pristines:
            args.append("--vacuum-pristines")
        if include_externals:
            args.append("--include-externals")
        return self.build(*args, "--", validate_path(path), auth=False)

    def cmd_upgrade(self, path: str) -> Command:
        return self.build("upgrade", "--", validate_path(path), auth=False)

    def cmd_resolve(self, paths: Sequence[str], accept: str = "working", recursive: bool = False) -> Command:
        args = ["resolve", "--accept", accept, "--depth", "infinity" if recursive else "empty"]
        return self._with_targets(self.build(*args, auth=False), paths)

    def cmd_patch(self, patch_file: str, wc_path: str, dry_run: bool = False, reverse: bool = False,
                  strip: int = 0) -> Command:
        args = ["patch"]
        if dry_run:
            args.append("--dry-run")
        if reverse:
            args.append("--reverse-diff")
        if strip:
            args += ["--strip", str(strip)]
        return self.build(*args, "--", validate_path(patch_file), validate_path(wc_path), auth=False)

    def cmd_changelist(self, paths: Sequence[str], name: Optional[str]) -> Command:
        if name:
            if name.startswith("-") or any(c in name for c in "\n\r\0"):
                raise ValidationError("Nombre de changelist no válido.")
            cmd = self.build("changelist", name, auth=False)
        else:
            cmd = self.build("changelist", "--remove", auth=False)
        return self._with_targets(cmd, paths)

    def cmd_create_repo(self, path: str, fs_type: str = "fsfs") -> Command:
        if fs_type not in ("fsfs", "bdb", "fsx"):
            raise ValidationError("Tipo de repositorio no válido.")
        return Command(argv=[self.svnadmin_bin, "create", "--fs-type", fs_type, "--", validate_path(path)])


def wc_root_of(path: str, client: Optional[SvnClient] = None) -> Optional[str]:
    """Devuelve la raíz de la working copy que contiene `path`, o None."""
    client = client or SvnClient()
    try:
        infos = client.info(path)
    except SvnError:
        return None
    return infos[0].wc_root if infos else None
