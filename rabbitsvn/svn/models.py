"""Estructuras de datos devueltas por el cliente SVN."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def parse_svn_date(value: Optional[str]) -> Optional[datetime]:
    """Convierte '2026-10-06T10:00:00.123456Z' a datetime local."""
    if not value:
        return None
    value = value.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1]
        if "." in value:
            base, frac = value.split(".", 1)
            value = f"{base}.{frac[:6]}"
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
        else:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return None


@dataclass
class LockInfo:
    token: str = ""
    owner: str = ""
    comment: str = ""
    created: Optional[datetime] = None


@dataclass
class StatusEntry:
    path: str
    item: str = "normal"            # normal, modified, added, deleted, unversioned, missing, ...
    props: str = "none"
    revision: Optional[int] = None
    changed_rev: Optional[int] = None
    author: str = ""
    date: Optional[datetime] = None
    copied: bool = False
    switched: bool = False
    tree_conflicted: bool = False
    wc_locked: bool = False
    lock: Optional[LockInfo] = None          # lock en la working copy (token local)
    repos_item: Optional[str] = None         # con --show-updates
    repos_props: Optional[str] = None
    repos_lock: Optional[LockInfo] = None
    changelist: Optional[str] = None
    kind: str = ""                           # se rellena externamente si se conoce

    @property
    def is_versioned(self) -> bool:
        return self.item not in ("unversioned", "ignored", "external")

    @property
    def is_changed(self) -> bool:
        return (self.item not in ("normal", "none", "ignored", "external")
                or self.props not in ("none", "normal")
                or self.tree_conflicted)

    @property
    def is_conflicted(self) -> bool:
        return self.item == "conflicted" or self.props == "conflicted" or self.tree_conflicted

    @property
    def has_remote_changes(self) -> bool:
        return (self.repos_item not in (None, "none", "normal")
                or self.repos_props not in (None, "none", "normal"))


@dataclass
class InfoEntry:
    path: str
    kind: str = ""
    url: str = ""
    relative_url: str = ""
    repo_root: str = ""
    uuid: str = ""
    revision: Optional[int] = None
    last_changed_rev: Optional[int] = None
    last_changed_author: str = ""
    last_changed_date: Optional[datetime] = None
    wc_root: str = ""
    schedule: str = ""
    depth: str = ""
    lock: Optional[LockInfo] = None
    conflict_files: dict = field(default_factory=dict)   # prev-base-file, prev-wc-file, cur-base-file


@dataclass
class LogPath:
    path: str
    action: str
    kind: str = ""
    copyfrom_path: str = ""
    copyfrom_rev: Optional[int] = None
    text_mods: bool = False
    prop_mods: bool = False


@dataclass
class LogEntry:
    revision: int
    author: str = ""
    date: Optional[datetime] = None
    message: str = ""
    paths: list = field(default_factory=list)    # list[LogPath]


@dataclass
class ListEntry:
    name: str
    kind: str
    size: Optional[int] = None
    revision: Optional[int] = None
    author: str = ""
    date: Optional[datetime] = None
    lock: Optional[LockInfo] = None


@dataclass
class BlameLine:
    line_no: int
    revision: Optional[int]
    author: str
    date: Optional[datetime]
    text: str


@dataclass
class DiffSummaryEntry:
    path: str
    item: str
    props: str
    kind: str
