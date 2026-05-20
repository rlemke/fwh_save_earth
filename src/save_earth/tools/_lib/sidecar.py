"""Per-entry sidecar metadata for cached artifacts.

Replaces the legacy one-manifest-per-cache-type design. Each cached
artifact has a sibling ``<artifact>.meta.json`` file that records the
same information the old manifest entry did (size, sha256, source,
tool, lineage, extras). Because metadata is per-entry, N writers on N
different keys never contend — the only lock needed is a per-entry
lock for the rare case of two writers replacing the same key.

Layout (see ``agent-spec/cache-layout.agent-spec.yaml``)::

    AFL_DATA_ROOT/
      cache/<namespace>/<cache_type>/<relative_path>
      cache/<namespace>/<cache_type>/<relative_path>.meta.json
      staging/<namespace>/<cache_type>/<relative_path>.stage-<tag>
      locks/<namespace>/<cache_type>/<relative_path>.lock

For directory artifacts, the sidecar is a **sibling** of the directory,
not a file inside it::

    cache/osm/graphhopper/north-america/us/california/        # the dir
    cache/osm/graphhopper/north-america/us/california.meta.json

One rule, no special cases. This keeps build tools that scan the
artifact directory (GraphHopper, Valhalla, etc.) pure-payload.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from _lib.storage import (
    LocalStorage,
    Storage,
    cache_root,
    locks_root,
    staging_root,
)

SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".meta.json"


def _storage(storage: Storage | None) -> Storage:
    return storage if storage is not None else LocalStorage()


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Path derivation.
# ---------------------------------------------------------------------------


def cache_dir(namespace: str, cache_type: str, storage: Storage | None = None) -> str:
    """Return ``<cache_root>/<namespace>/<cache_type>``."""
    s = _storage(storage)
    return Storage.join(cache_root(s.name), namespace, cache_type)


def staging_dir(namespace: str, cache_type: str, storage: Storage | None = None) -> str:
    """Return ``<staging_root>/<namespace>/<cache_type>``."""
    s = _storage(storage)
    return Storage.join(staging_root(s.name), namespace, cache_type)


def locks_dir(namespace: str, cache_type: str, storage: Storage | None = None) -> str:
    """Return ``<locks_root>/<namespace>/<cache_type>``."""
    s = _storage(storage)
    return Storage.join(locks_root(s.name), namespace, cache_type)


def cache_path(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> str:
    """Absolute cache path for an artifact."""
    return Storage.join(cache_dir(namespace, cache_type, storage), relative_path)


def sidecar_path(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> str:
    """Absolute path to the sidecar for an artifact.

    Sidecar = ``<artifact>.meta.json`` whether the artifact is a file or
    a directory. For directories, the sidecar lives **next to** the dir.
    """
    return cache_path(namespace, cache_type, relative_path, storage) + SIDECAR_SUFFIX


def staging_path(
    namespace: str,
    cache_type: str,
    relative_path: str,
    *,
    tag: str | None = None,
    storage: Storage | None = None,
) -> str:
    """Staging path for an in-flight write.

    A short random suffix (``.stage-<hex>``) makes concurrent writers on
    the same key produce distinct staging files. Only one wins the final
    rename; the others' staging files are orphaned and cleaned up by the
    respective writers.
    """
    if tag is None:
        tag = secrets.token_hex(4)
    base = Storage.join(staging_dir(namespace, cache_type, storage), relative_path)
    return f"{base}.stage-{tag}"


def lock_path(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> str:
    """Per-entry lock file path."""
    return Storage.join(locks_dir(namespace, cache_type, storage), relative_path) + ".lock"


# ---------------------------------------------------------------------------
# Sidecar read / write.
# ---------------------------------------------------------------------------


def read_sidecar(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> dict[str, Any] | None:
    """Return the sidecar dict for an entry, or ``None`` if missing.

    No lock is acquired; readers are unbounded.
    """
    s = _storage(storage)
    path = sidecar_path(namespace, cache_type, relative_path, s)
    if not s.exists(path):
        return None
    data = json.loads(s.read_text(path))
    if not isinstance(data, dict):
        raise ValueError(f"Malformed sidecar at {path}: not an object")
    return data


def write_sidecar(
    namespace: str,
    cache_type: str,
    relative_path: str,
    *,
    kind: str,
    size_bytes: int,
    sha256: str,
    source: dict[str, Any] | None = None,
    tool: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    generated_at: str | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Build and atomically write the sidecar for an entry.

    The artifact itself must already be at its final cache path before
    this is called. Readers treat a missing sidecar as "entry not present",
    so the rename-artifact-first / write-sidecar-second order is critical.
    """
    if kind not in ("file", "directory"):
        raise ValueError(f"kind must be 'file' or 'directory', got {kind!r}")
    s = _storage(storage)
    data: dict[str, Any] = {
        "version": SIDECAR_VERSION,
        "namespace": namespace,
        "cache_type": cache_type,
        "relative_path": relative_path,
        "kind": kind,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "generated_at": generated_at or utcnow_iso(),
    }
    if source is not None:
        data["source"] = source
    if tool is not None:
        data["tool"] = tool
    if extra is not None:
        data["extra"] = extra
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    path = sidecar_path(namespace, cache_type, relative_path, s)
    s.mkdir_p(Storage.dirname(path))
    s.write_text_atomic(path, text)
    return data


# ---------------------------------------------------------------------------
# Presence and validity.
# ---------------------------------------------------------------------------


def exists_and_valid(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> bool:
    """Return True iff both the sidecar and the artifact exist with matching size.

    SHA-256 is NOT re-verified here — that would require reading the full
    artifact and is prohibitively expensive. Callers that need full
    verification should compute it explicitly.
    """
    s = _storage(storage)
    side = read_sidecar(namespace, cache_type, relative_path, s)
    if side is None:
        return False
    art = cache_path(namespace, cache_type, relative_path, s)
    if not s.exists(art):
        return False
    if side.get("kind") == "file":
        return s.size(art) == side.get("size_bytes", -1)
    return True


def delete_entry(
    namespace: str,
    cache_type: str,
    relative_path: str,
    storage: Storage | None = None,
) -> None:
    """Delete both the sidecar and the artifact, if they exist."""
    s = _storage(storage)
    side_path = sidecar_path(namespace, cache_type, relative_path, s)
    art_path = cache_path(namespace, cache_type, relative_path, s)
    s.unlink(side_path)
    if s.exists(art_path):
        side = read_sidecar(namespace, cache_type, relative_path, s)
        kind = side.get("kind") if side else None
        if kind == "directory" or (kind is None and _is_dir(s, art_path)):
            _rmtree(s, art_path)
        else:
            s.unlink(art_path)


def _is_dir(storage: Storage, path: str) -> bool:
    """Best-effort is-dir check (backends don't expose isdir uniformly)."""
    if isinstance(storage, LocalStorage):
        import os as _os

        return _os.path.isdir(path)
    return False


def _rmtree(storage: Storage, path: str) -> None:
    if isinstance(storage, LocalStorage):
        import shutil as _shutil

        _shutil.rmtree(path, ignore_errors=True)
    else:
        storage.unlink(path)


# ---------------------------------------------------------------------------
# Per-entry lock.
# ---------------------------------------------------------------------------


@contextmanager
def entry_lock(
    namespace: str,
    cache_type: str,
    relative_path: str,
    *,
    exclusive: bool = True,
    storage: Storage | None = None,
) -> Iterator[None]:
    """Acquire a per-entry advisory lock. No-op on HDFS.

    Intended for the rare case of two writers replacing the same key
    concurrently. New-entry writes do not need this lock — the staging +
    rename protocol alone gives last-writer-wins semantics safely.
    """
    s = _storage(storage)
    path = lock_path(namespace, cache_type, relative_path, s)
    s.mkdir_p(Storage.dirname(path))
    with s.lock(path, exclusive=exclusive):
        yield


# ---------------------------------------------------------------------------
# Listing.
# ---------------------------------------------------------------------------


def list_entries(
    namespace: str,
    cache_type: str,
    storage: Storage | None = None,
) -> list[dict[str, Any]]:
    """Walk the cache_type subtree and return every sidecar's contents.

    O(N) on the number of entries. For large cache types, prefer
    ``cache_index.read_index`` (which rebuilds lazily from this same walk).
    Returns sidecars sorted by ``relative_path``.
    """
    s = _storage(storage)
    root = cache_dir(namespace, cache_type, s)
    out: list[dict[str, Any]] = []
    for abs_path in _walk_sidecars(s, root):
        data = json.loads(s.read_text(abs_path))
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda d: d.get("relative_path", ""))
    return out


def _walk_sidecars(storage: Storage, root: str) -> Iterator[str]:
    """Yield absolute paths of every ``*.meta.json`` under ``root``."""
    if isinstance(storage, LocalStorage):
        import os as _os

        if not _os.path.isdir(root):
            return
        for dirpath, _dirs, files in _os.walk(root):
            for fn in files:
                if fn.endswith(SIDECAR_SUFFIX):
                    yield _os.path.join(dirpath, fn)
        return
    # HDFS walk — best-effort using the backend's list_* method if available.
    try:
        entries = storage._backend.walk(root)  # type: ignore[attr-defined]
    except Exception:
        return
    for dirpath, _dirs, files in entries:
        for fn in files:
            if fn.endswith(SIDECAR_SUFFIX):
                yield Storage.join(dirpath, fn)


def list_relative_paths(
    namespace: str,
    cache_type: str,
    *,
    under: str | None = None,
    storage: Storage | None = None,
) -> list[str]:
    """Return just the ``relative_path`` of every entry, optionally prefix-filtered."""
    entries = list_entries(namespace, cache_type, storage)
    paths = [e.get("relative_path", "") for e in entries if e.get("relative_path")]
    if under:
        u = under.strip().strip("/")
        pref = u + "/"
        paths = [p for p in paths if p == u or p.startswith(pref)]
    return paths
