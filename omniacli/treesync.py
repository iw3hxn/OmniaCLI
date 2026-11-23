# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One copy engine for the handover of a workspace (part A) and the move of
an instance between machines (part B): docs/adr/AD-008-consegna-e-spostamento.md,
AD-010. A tree of files on one machine copied onto another, in rounds that
resume.

- **A manifest.** Every tree is listed as one JSON line per regular file,
  {"p": its path under the tree, "s": size, "h": "sha1:<hex>" or
  "sha256:<hex>", "m": mtime in ns}, sorted by path (the order Python
  compares strings in), written to a file and read back line by line: never
  a whole manifest in memory (a filestore of 500,000 files is about 25 MB of
  lines).
- **Rounds.** Round 0 copies everything; each round after copies what the
  source has that the receiver has not, or has different: the two listings
  compared in order (diff). What the receiver has more stays until the last
  round, at a stopped source, prunes it. "In step", for the files: the last
  round finished 10 minutes ago at most (in_step).
- **Two kinds of tree.**
  - "root": what only root reads, the filestore of Odoo
    (<project>/data/filestore/filestore/<database>). Listed by the root
    helper's tree-list, read in tar batches of at most 256 MiB by tree-read
    (paths on its input, a tar on its output: regular files under the tree
    only, never a link) and written by the receiver's tree-write (every name
    checked, a file of the filestore checked against its name, which is the
    SHA-1 of its content: no hash to compute at the source; given to the
    owner of the receiver's own project). A file half written is dropped and
    copied again.
  - "files": what OmniaCLI reads itself, the backups of an instance
    (<backup_dir>/<server>/<role>). One file at a time, from where the
    receiver's <name>.omniacli-part ends (an HTTP Range), its SHA-256 checked
    when whole (the source's in hashes.json, by path, size, mtime and inode:
    a backup is hashed once).
- **Resumption.** The state of a copy (its round, its plan of batches, the
  batches done) is written after every batch, in its work folder: a job
  killed halfway goes on from the next batch not done.
- **Two drivers.** pull: the receiver asks the source (part A, and the
  return of part B); push: the source sends to the receiver (the move of
  part B). The engine is the same: a Source and a Sink, each on this
  machine (Local*, through the root helper or OmniaCLI's own files) or on
  the machine of a link (Remote*, the routes /api/treesync/v1/...).

The functions of the "root" trees (list_tree, read_tree, write_tree,
prune_tree) work on a folder opened by root (open_tree): every folder below
it is opened without following a link (O_NOFOLLOW), so a link put in a
filestore by its Odoo never leads out of it."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import json
import os
import re
import stat
import tarfile
import time
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Iterable, Iterator

import httpx

# the trees the engine knows: what each is, and who reads it
TREES = {"filestore": "root", "backups": "files"}
ROLES = ("production", "test")
CHUNK = 1 << 20
BATCH_BYTES = 256 << 20
BATCH_FILES = 20000
# at most this many copy requests at a time, per copy (AD-010)
FLOWS = 4
# "in step": the last round of the files finished this many seconds ago at most
IN_STEP = 600
# a path under a tree: segments of printable characters, no "." or ".."
MAX_PATH = 1024
SEGMENT = re.compile(r"[^\x00-\x1f\x7f/\\]{1,255}")
# a file of Odoo's filestore: <2 hex>/<40 hex>, its name the SHA-1 of its content
FILESTORE_NAME = re.compile(r"([0-9a-f]{2})/(\1[0-9a-f]{38})")
DIGEST = re.compile(r"(sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})")
# what a receiver writes before a file is whole: never listed, never copied
PART = ".omniacli-part"
STATE_FILE = "state.json"
HASHES_FILE = "hashes.json"
_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class TreeError(Exception):
    """A tree, a path or a copy refused: said as it is."""


def safe_rel(value) -> str:
    """value as a path under a tree (a/b/c), or TreeError: no absolute path,
    no "." or "..", no empty segment, no control character, no backslash."""
    text = str(value or "")
    parts = text.split("/")
    if (not text or len(text) > MAX_PATH or any(p in ("", ".", "..") for p in parts)
            or not all(SEGMENT.fullmatch(p) for p in parts) or text.endswith(PART)):
        raise TreeError(f"{text[:120]!r} is not a path under the tree")
    return text


def tree_kind(tree: str) -> str:
    if tree not in TREES:
        raise TreeError(f"{tree!r} is not a tree OmniaCLI copies ({', '.join(TREES)})")
    return TREES[tree]


def self_digest(rel: str) -> str | None:
    """The digest a file of the filestore carries in its name, None for any other."""
    found = FILESTORE_NAME.fullmatch(rel)
    return f"sha1:{found.group(2)}" if found else None


def entry(rel: str, size: int, digest: str, mtime_ns: int) -> dict:
    return {"p": rel, "s": int(size), "h": digest, "m": int(mtime_ns)}


def line_of(item: dict) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"


def read_lines(path: Path) -> Iterator[dict]:
    """The entries of a manifest file, in its order, each checked."""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                yield checked_entry(json.loads(raw))


def checked_entry(item) -> dict:
    if not isinstance(item, dict):
        raise TreeError("a line of a manifest is not an object")
    rel = safe_rel(item.get("p"))
    size = item.get("s")
    digest = str(item.get("h") or "")
    if not isinstance(size, int) or size < 0 or not DIGEST.fullmatch(digest):
        raise TreeError(f"the line of {rel} in a manifest has no size or no digest")
    return entry(rel, size, digest, int(item.get("m") or 0))


def diff(source: Iterable[dict], sink: Iterable[dict]) -> Iterator[tuple[str, dict]]:
    """("new" | "changed", the source's entry) and ("extra", the sink's) of
    two manifests sorted by path, compared in order; a file is the same when
    its size and digest are."""
    a, b = iter(source), iter(sink)
    x, y = next(a, None), next(b, None)
    last = ""
    while x is not None or y is not None:
        if x is not None and last and x["p"] <= last:
            raise TreeError(f"the source's manifest is not in order at {x['p']}")
        if y is None or (x is not None and x["p"] < y["p"]):
            last = x["p"]
            yield "new", x
            x = next(a, None)
        elif x is None or y["p"] < x["p"]:
            yield "extra", y
            y = next(b, None)
        else:
            last = x["p"]
            if x["s"] != y["s"] or x["h"] != y["h"]:
                yield "changed", x
            x, y = next(a, None), next(b, None)


def plan(changes: Iterable[tuple[str, dict]], limit: int | None = None, most: int | None = None
         ) -> Iterator[tuple[list[dict], int]]:
    """The batches of what is new or changed: at most limit bytes and most
    files each (a file larger than limit goes alone); with how many extra."""
    limit, most = limit or BATCH_BYTES, most or BATCH_FILES
    batch, size = [], 0
    for kind, item in changes:
        if kind == "extra":
            continue
        if batch and (size + item["s"] > limit or len(batch) >= most):
            yield batch, size
            batch, size = [], 0
        batch.append(item)
        size += item["s"]
    if batch:
        yield batch, size


def in_step(state: dict, now: float | None = None) -> bool:
    """The last round finished IN_STEP seconds ago at most (AD-010: the files are "in step")."""
    done = state.get("finished") if state.get("phase") == "done" else None
    if not done:
        return False
    return (time.time() if now is None else now) - float(done) <= IN_STEP


# --- root's side: a tree opened without following links ------------------------------

def open_tree(base: Path, parts: list[str], create: bool = False, uid: int = -1, gid: int = -1) -> int:
    """A file descriptor of base/<parts>, every part opened without following
    a link; create: the folders that are missing made (0755, uid:gid)."""
    fd = os.open(base, _FOLDER)
    try:
        return open_at(fd, parts, create, uid, gid)
    finally:
        os.close(fd)


def open_at(fd: int, parts: list[str], create: bool = False, uid: int = -1, gid: int = -1) -> int:
    """A new file descriptor of the folder <parts> under the folder fd (fd
    itself stays open), as open_tree."""
    fd = os.dup(fd)
    try:
        for part in parts:
            try:
                nfd = os.open(part, _FOLDER, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise TreeError(f"{'/'.join(parts)} is not there") from None
                os.mkdir(part, 0o755, dir_fd=fd)
                os.chown(part, uid, gid, dir_fd=fd, follow_symlinks=False)
                nfd = os.open(part, _FOLDER, dir_fd=fd)
            except OSError:
                raise TreeError(f"{part} of {'/'.join(parts)} is not a folder (a link is never followed)") from None
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk(fd: int, prefix: str = "") -> Iterator[tuple[str, os.stat_result, int, str]]:
    """(path, stat, the folder's descriptor, name) of every regular file
    under fd, in the order of their paths; links and special files skipped."""
    items = []
    for name in os.listdir(fd):
        if name.endswith(PART) or not SEGMENT.fullmatch(name) or name in (".", ".."):
            continue
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(st.st_mode):
            items.append((name + "/", name, st))
        elif stat.S_ISREG(st.st_mode):
            items.append((name, name, st))
    items.sort()
    for key, name, st in items:
        if key.endswith("/"):
            try:
                sub = os.open(name, _FOLDER, dir_fd=fd)
            except OSError:
                continue
            try:
                yield from _walk(sub, f"{prefix}{name}/")
            finally:
                os.close(sub)
        else:
            yield f"{prefix}{name}", st, fd, name


def _sha256_at(fd: int, name: str) -> str:
    digest = hashlib.sha256()
    ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(ffd, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def list_tree(fd: int, out, cache: "HashCache | None" = None) -> dict:
    """The manifest of the tree at fd written to out (text): a file of the
    filestore by its name, any other by its SHA-256 (from cache when its
    path, size, mtime and inode did not change)."""
    files = size = 0
    for rel, st, dfd, name in _walk(fd):
        digest = self_digest(rel) or (cache.digest(rel, st, lambda: _sha256_at(dfd, name)) if cache
                                      else _sha256_at(dfd, name))
        out.write(line_of(entry(rel, st.st_size, digest, st.st_mtime_ns)))
        files += 1
        size += st.st_size
    return {"files": files, "bytes": size}


class _Exact:
    """A file read up to size bytes, and no further."""

    def __init__(self, fh: BinaryIO, size: int):
        self.fh, self.left = fh, size

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        data = self.fh.read(n)
        self.left -= len(data)
        return data


def read_tree(fd: int, paths: Iterable[str], out: BinaryIO) -> dict:
    """A tar of the regular files paths name under fd, written to out as it
    is read; a path that is not a regular file there (gone, a link, a
    folder) is left out and counted."""
    sent = missing = size = 0
    folder: tuple[str, int] | None = None
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        try:
            for raw in paths:
                rel = safe_rel(raw)
                parent, _, name = rel.rpartition("/")
                if folder is None or folder[0] != parent:
                    if folder is not None:
                        os.close(folder[1])
                        folder = None
                    try:
                        folder = (parent, open_at(fd, parent.split("/") if parent else []))
                    except TreeError:
                        missing += 1
                        continue
                try:
                    ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder[1])
                except OSError:
                    missing += 1
                    continue
                with os.fdopen(ffd, "rb") as fh:
                    st = os.fstat(fh.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        missing += 1
                        continue
                    info = tarfile.TarInfo(rel)
                    info.size, info.mtime, info.mode, info.type = st.st_size, st.st_mtime, 0o644, tarfile.REGTYPE
                    tar.addfile(info, _Exact(fh, st.st_size))
                sent += 1
                size += st.st_size
        finally:
            if folder is not None:
                os.close(folder[1])
    return {"sent": sent, "missing": missing, "bytes": size}


def _write_one(top: int, rel: str, source: BinaryIO, size: int, mtime: float, uid: int, gid: int) -> str:
    """One file written under top: into a hidden part beside its place, then
    renamed over it; "" when written, else why it is not."""
    parts = rel.split("/")
    try:
        dfd = open_at(top, parts[:-1], create=True, uid=uid, gid=gid)
    except (TreeError, OSError) as exc:
        return f"its folder cannot be made ({exc})"
    tmp = f".{parts[-1][:200]}{PART}"
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dfd)
        ffd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        expected = self_digest(rel)
        sha1 = hashlib.sha1() if expected else None
        written = 0
        try:
            with os.fdopen(ffd, "wb") as fh:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    fh.write(chunk)
                    written += len(chunk)
                    if sha1 is not None:
                        sha1.update(chunk)
                os.fchown(fh.fileno(), uid, gid)
                os.fchmod(fh.fileno(), 0o644)
        except BaseException:
            os.unlink(tmp, dir_fd=dfd)
            raise
        if written != size:
            os.unlink(tmp, dir_fd=dfd)
            raise TreeError(f"{rel} arrived with {written} bytes of {size}")
        if sha1 is not None and f"sha1:{sha1.hexdigest()}" != expected:
            os.unlink(tmp, dir_fd=dfd)
            return "its content is not what its name says (the SHA-1 of the filestore)"
        os.utime(tmp, ns=(int(mtime * 1e9),) * 2, dir_fd=dfd, follow_symlinks=False)
        try:
            os.rename(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as exc:
            os.unlink(tmp, dir_fd=dfd)
            return f"it cannot take its place ({exc.strerror})"
        return ""
    finally:
        os.close(dfd)


def write_tree(top: int, stream: BinaryIO, uid: int, gid: int) -> dict:
    """A tar read from stream written under top, file by file, each given to
    uid:gid: a name that is not a path under the tree, a link or a special
    file, a file of the filestore whose content is not its name: refused,
    and said; a stream that ends before its end: what was whole stays, the
    file it ended in is dropped ("complete": False)."""
    report = {"written": 0, "bytes": 0, "refused": [], "complete": False}
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                try:
                    rel = safe_rel(member.name)
                except TreeError as exc:
                    report["refused"].append({"p": member.name[:200], "why": str(exc)})
                    continue
                if member.isdir():
                    continue
                if not member.isreg():
                    report["refused"].append({"p": rel, "why": "a link or a special file is never written"})
                    continue
                why = _write_one(top, rel, tar.extractfile(member), member.size, member.mtime, uid, gid)
                if why:
                    report["refused"].append({"p": rel, "why": why})
                else:
                    report["written"] += 1
                    report["bytes"] += member.size
        report["complete"] = True
    except (tarfile.TarError, EOFError, OSError, TreeError) as exc:
        report["error"] = str(exc) or type(exc).__name__
    return report


def prune_tree(fd: int, keep: Iterable[str]) -> dict:
    """The regular files under fd that keep (sorted paths) does not name, deleted."""
    wanted = iter(keep)
    next_kept = next(wanted, None)
    removed = freed = 0
    for rel, st, dfd, name in _walk(fd):
        while next_kept is not None and next_kept < rel:
            next_kept = next(wanted, None)
        if next_kept == rel:
            continue
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=dfd)
            removed += 1
            freed += st.st_size
    return {"removed": removed, "bytes": freed}


# --- OmniaCLI's side: the hashes of the files it reads itself ---------------------------

class HashCache:
    """{path: [size, mtime_ns, inode, digest]} of a tree's files, kept in
    hashes.json: a file is hashed again only when one of the three changed."""

    def __init__(self, path: Path):
        self.path, self.changed = path, False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}
        self.seen: set[str] = set()

    def digest(self, rel: str, st: os.stat_result, compute) -> str:
        self.seen.add(rel)
        key = [st.st_size, st.st_mtime_ns, st.st_ino]
        known = self.data.get(rel)
        if isinstance(known, list) and len(known) == 4 and known[:3] == key and DIGEST.fullmatch(str(known[3])):
            return known[3]
        found = compute()
        self.data[rel] = [*key, found]
        self.changed = True
        return found

    def save(self) -> None:
        kept = {k: v for k, v in self.data.items() if k in self.seen}
        if kept != self.data or self.changed:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                json.dump(kept, fh, sort_keys=True)
            os.replace(tmp, self.path)
            self.data, self.changed = kept, False


def part_size(root: Path, rel: str) -> int:
    """How much of rel is here already (its .omniacli-part), 0 when nothing."""
    path = root / f"{rel}{PART}"
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def _private_folder(root: Path, rel: str) -> Path:
    folder = root
    for part in rel.split("/")[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise TreeError(f"{folder} is a link: nothing is written through it")
        folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def put_part(root: Path, rel: str, offset: int, chunks: Iterable[bytes], size: int, digest: str, mtime_ns: int = 0) -> dict:
    """The bytes of rel from offset appended to its part; once it has size
    bytes its SHA-256 is checked against digest and it takes its place (600).
    A part that is not offset bytes long is refused (the sender asks where it is)."""
    rel = safe_rel(rel)
    if not DIGEST.fullmatch(digest) or not digest.startswith("sha256:"):
        raise TreeError(f"{rel}: a file is sent with its SHA-256")
    folder = _private_folder(root, rel)
    path = folder / (rel.rsplit("/", 1)[-1] + PART)
    have = part_size(root, rel)
    if have != offset:
        raise TreeError(f"{rel}: {have} bytes are here, not {offset}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "ab") as fh:
        for chunk in chunks:
            fh.write(chunk)
            if fh.tell() > size:
                break
    have = part_size(root, rel)
    if have < size:
        return {"p": rel, "size": have, "done": False}
    found = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            found.update(chunk)
    if have != size or "sha256:" + found.hexdigest() != digest:
        path.unlink(missing_ok=True)
        raise TreeError(f"{rel} arrived different from its source: it is copied again")
    if mtime_ns:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    os.replace(path, folder / rel.rsplit("/", 1)[-1])
    return {"p": rel, "size": have, "done": True}


def list_files(root: Path, out, cache: HashCache | None = None) -> dict:
    """The manifest of a tree of OmniaCLI's own files (none: an empty tree)."""
    if not root.is_dir():
        return {"files": 0, "bytes": 0}
    fd = os.open(root, _FOLDER)
    try:
        return list_tree(fd, out, cache)
    finally:
        os.close(fd)


def prune_files(root: Path, keep: Iterable[str]) -> dict:
    if not root.is_dir():
        return {"removed": 0, "bytes": 0}
    fd = os.open(root, _FOLDER)
    try:
        return prune_tree(fd, keep)
    finally:
        os.close(fd)


def open_file(root: Path, rel: str) -> tuple[BinaryIO, os.stat_result]:
    """A file of a tree of OmniaCLI's own, opened to read (a link never followed)."""
    rel = safe_rel(rel)
    parent, _, name = rel.rpartition("/")
    try:
        dfd = open_tree(root, parent.split("/") if parent else [])
    except (TreeError, OSError):
        raise TreeError(f"{rel} is not in the tree") from None
    try:
        ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    except OSError:
        raise TreeError(f"{rel} is not in the tree") from None
    finally:
        os.close(dfd)
    fh = os.fdopen(ffd, "rb")
    st = os.fstat(ffd)
    if not stat.S_ISREG(st.st_mode):
        fh.close()
        raise TreeError(f"{rel} is not a regular file")
    return fh, st


# --- the ends of a copy -----------------------------------------------------------------

class Source:
    """Where a tree is copied from."""
    kind = "root"

    async def listing(self, into: Path) -> dict:
        raise NotImplementedError

    def read(self, paths: list[str]):
        """An async context manager: the tar of paths, as an async iterator of bytes."""
        raise NotImplementedError

    def fetch(self, rel: str, offset: int):
        """An async context manager: the bytes of rel from offset."""
        raise NotImplementedError


class Sink:
    """Where a tree is copied to."""
    kind = "root"

    async def listing(self, into: Path) -> dict:
        raise NotImplementedError

    async def write(self, chunks: AsyncIterator[bytes]) -> dict:
        raise NotImplementedError

    async def have(self, rel: str) -> int:
        raise NotImplementedError

    async def put(self, item: dict, offset: int, chunks: AsyncIterator[bytes]) -> dict:
        raise NotImplementedError

    async def prune(self, keep: Path) -> dict:
        raise NotImplementedError


async def _lines_into(chunks: AsyncIterator[bytes], into: Path) -> dict:
    """A manifest streamed into a file (600), each line checked; its counts."""
    files = size = 0
    tail = b""
    with open(os.open(into, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        async for chunk in chunks:
            tail += chunk
            *whole, tail = tail.split(b"\n")
            for raw in whole:
                if raw.strip():
                    item = checked_entry(json.loads(raw))
                    fh.write(line_of(item))
                    files += 1
                    size += item["s"]
        if tail.strip():
            item = checked_entry(json.loads(tail))
            fh.write(line_of(item))
            files += 1
            size += item["s"]
    return {"files": files, "bytes": size}


def _sorted_check(path: Path) -> None:
    last = None
    for item in read_lines(path):
        if last is not None and item["p"] <= last:
            raise TreeError(f"a listing is not in order at {item['p']}")
        last = item["p"]


class Process:
    """A program of the root helper with its input and output as streams
    (tree-read's tar, tree-write's tar): spawn is what starts it (tests)."""
    spawn = staticmethod(asyncio.create_subprocess_exec)

    def __init__(self, command: list[str]):
        self.command = command
        self.proc = None

    async def start(self, stdin: bool) -> "Process":
        self.proc = await self.spawn(*self.command, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                     start_new_session=True)
        return self

    async def output(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.proc.stdout.read(CHUNK)
            if not chunk:
                return
            yield chunk

    async def finish(self) -> str:
        """"" when the program ended well, else what it said."""
        err = await self.proc.stderr.read()
        code = await self.proc.wait()
        if code:
            return err.decode(errors="replace").strip()[-600:] or f"{self.command[4] if len(self.command) > 4 else 'it'} failed ({code})"
        return ""

    async def kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            with contextlib.suppress(Exception):
                await self.proc.wait()


class LocalTree(Source, Sink):
    """A root tree of an instance of this machine, through the root helper
    (workspace-helper tree-*): helper(*args) is its command."""
    kind = "root"

    def __init__(self, helper, workspace: str, storage: str, instance: str, role: str):
        self.helper, self.args = helper, (workspace, storage, instance, role)

    async def _run_into(self, action: str, into: Path) -> dict:
        proc = await Process(self.helper(action, *self.args)).start(stdin=False)
        try:
            counts = await _lines_into(proc.output(), into)
        except BaseException:
            await proc.kill()
            raise
        why = await proc.finish()
        if why:
            raise TreeError(why)
        return counts

    async def listing(self, into: Path) -> dict:
        return await self._run_into("tree-list", into)

    @contextlib.asynccontextmanager
    async def read(self, paths: list[str]):
        proc = await Process(self.helper("tree-read", *self.args)).start(stdin=True)

        async def feed():
            try:
                for rel in paths:
                    proc.proc.stdin.write(safe_rel(rel).encode() + b"\n")
                    await proc.proc.stdin.drain()
            finally:
                proc.proc.stdin.close()
        feeding = asyncio.ensure_future(feed())

        async def body():
            async for chunk in proc.output():
                yield chunk
            await feeding
            why = await proc.finish()
            if why:
                raise TreeError(why)
        try:
            yield body()
        finally:
            feeding.cancel()
            await proc.kill()

    async def write(self, chunks: AsyncIterator[bytes]) -> dict:
        proc = await Process(self.helper("tree-write", *self.args)).start(stdin=True)
        try:
            try:
                async for chunk in chunks:
                    proc.proc.stdin.write(chunk)
                    await proc.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    proc.proc.stdin.close()
            out = b"".join([c async for c in proc.output()])
        except BaseException:
            await proc.kill()
            raise
        why = await proc.finish()
        if why:
            raise TreeError(why)
        try:
            return json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise TreeError(f"tree-write answered no JSON: {out[-300:]!r}") from None

    async def prune(self, keep: Path) -> dict:
        proc = await Process(self.helper("tree-prune", *self.args)).start(stdin=True)
        try:
            for item in read_lines(keep):
                proc.proc.stdin.write(item["p"].encode() + b"\n")
                await proc.proc.stdin.drain()
            proc.proc.stdin.close()
            out = b"".join([c async for c in proc.output()])
        except BaseException:
            await proc.kill()
            raise
        why = await proc.finish()
        if why:
            raise TreeError(why)
        return json.loads(out.decode().strip().splitlines()[-1])


class LocalFiles(Source, Sink):
    """A tree of OmniaCLI's own files on this machine (the backups of an
    instance), with its hashes kept in cache (hashes.json)."""
    kind = "files"

    def __init__(self, root: Path, cache: Path | None = None):
        self.root, self.cache = root, cache

    async def listing(self, into: Path) -> dict:
        def run():
            cache = HashCache(self.cache) if self.cache else None
            with open(os.open(into, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
                counts = list_files(self.root, fh, cache)
            if cache:
                cache.save()
            return counts
        return await asyncio.to_thread(run)

    @contextlib.asynccontextmanager
    async def fetch(self, rel: str, offset: int):
        fh, st = await asyncio.to_thread(open_file, self.root, rel)

        async def body():
            await asyncio.to_thread(fh.seek, offset)
            while True:
                chunk = await asyncio.to_thread(fh.read, CHUNK)
                if not chunk:
                    return
                yield chunk
        try:
            yield body()
        finally:
            fh.close()

    async def have(self, rel: str) -> int:
        return part_size(self.root, safe_rel(rel))

    async def put(self, item: dict, offset: int, chunks: AsyncIterator[bytes]) -> dict:
        # written in a thread as it arrives: the chunks go through a queue
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        loop = asyncio.get_running_loop()

        def drained():
            while True:
                chunk = asyncio.run_coroutine_threadsafe(queue.get(), loop).result()
                if chunk is None:
                    return
                yield chunk
        missing, level = [], self.root
        while not level.exists():
            missing.append(level)
            level = level.parent
        for level in reversed(missing):
            # private, as every folder of backups (private.py): a server's folder made here is 700 too
            level.mkdir(mode=0o700, exist_ok=True)
        writing =asyncio.ensure_future(asyncio.to_thread(
            put_part, self.root, item["p"], offset, drained(), item["s"], item["h"], item.get("m") or 0))
        try:
            async for chunk in chunks:
                if writing.done():
                    break
                await queue.put(chunk)
        except BaseException:
            await self._end(queue, writing)
            with contextlib.suppress(Exception):
                await writing
            raise
        await self._end(queue, writing)
        return await writing

    @staticmethod
    async def _end(queue: asyncio.Queue, writing: asyncio.Future) -> None:
        """The end said to the thread that writes, unless it ended already."""
        while not writing.done():
            try:
                queue.put_nowait(None)
                return
            except asyncio.QueueFull:
                await asyncio.sleep(0.01)

    async def prune(self, keep: Path) -> dict:
        return await asyncio.to_thread(prune_files, self.root, (item["p"] for item in read_lines(keep)))


class Refused(TreeError):
    """The other machine refused: its status and what it said."""

    def __init__(self, status_code: int, said: str):
        super().__init__(said)
        self.status_code = status_code


def _said(resp: httpx.Response, body: bytes | None = None) -> str:
    try:
        data = json.loads(body if body is not None else resp.content)
    except (ValueError, httpx.ResponseNotRead):
        data = None
    if isinstance(data, dict) and isinstance(data.get("error") or data.get("detail"), str):
        return data.get("error") or data.get("detail")
    return f"HTTP {resp.status_code}"


# the waits of a request that streams a tree: a batch of 256 MiB takes minutes on a slow line
STREAM_TIMEOUT = httpx.Timeout(300.0, connect=30.0)
API = "/api/treesync/v1"


class RemoteTree(Source, Sink):
    """A tree of an instance of the machine of a link, through its routes
    /api/treesync/v1/... (that machine's own LocalTree or LocalFiles)."""

    def __init__(self, client: httpx.AsyncClient, headers: dict, server: str, role: str, tree: str):
        self.client, self.headers = client, headers
        self.spec = {"server": server, "role": role, "tree": tree}
        self.kind = tree_kind(tree)

    async def _stream_into(self, path: str, into: Path) -> dict:
        async with self.client.stream("POST", API + path, json=self.spec, headers=self.headers,
                                      timeout=STREAM_TIMEOUT) as resp:
            if resp.status_code != 200:
                raise Refused(resp.status_code, _said(resp, await resp.aread()))
            counts = await _lines_into(resp.aiter_bytes(), into)
            if resp.headers.get("x-treesync-error") is not None:
                raise TreeError(resp.headers["x-treesync-error"])
        await asyncio.to_thread(_sorted_check, into)
        return counts

    async def listing(self, into: Path) -> dict:
        return await self._stream_into("/list", into)

    @contextlib.asynccontextmanager
    async def read(self, paths: list[str]):
        async with self.client.stream("POST", API + "/read", json={**self.spec, "paths": paths},
                                      headers=self.headers, timeout=STREAM_TIMEOUT) as resp:
            if resp.status_code != 200:
                raise Refused(resp.status_code, _said(resp, await resp.aread()))
            yield resp.aiter_bytes()

    @contextlib.asynccontextmanager
    async def fetch(self, rel: str, offset: int):
        headers = {**self.headers, **({"Range": f"bytes={offset}-"} if offset else {})}
        async with self.client.stream("GET", API + "/file", params={**self.spec, "path": rel}, headers=headers,
                                      timeout=STREAM_TIMEOUT) as resp:
            if resp.status_code not in (200, 206) or (offset and resp.status_code != 206):
                raise Refused(resp.status_code, _said(resp, await resp.aread()))
            yield resp.aiter_bytes()

    async def _post_json(self, path: str, params: dict, content) -> dict:
        resp = await self.client.post(API + path, params={**self.spec, **params}, content=content,
                                      headers={**self.headers, "Content-Type": "application/octet-stream"},
                                      timeout=STREAM_TIMEOUT)
        if resp.status_code != 200:
            raise Refused(resp.status_code, _said(resp))
        data = resp.json()
        if not isinstance(data, dict):
            raise TreeError("an answer that is not an object")
        return data

    async def write(self, chunks: AsyncIterator[bytes]) -> dict:
        return await self._post_json("/write", {}, chunks)

    async def have(self, rel: str) -> int:
        resp = await self.client.get(API + "/part", params={**self.spec, "path": rel}, headers=self.headers)
        if resp.status_code != 200:
            raise Refused(resp.status_code, _said(resp))
        return int(resp.json().get("size") or 0)

    async def put(self, item: dict, offset: int, chunks: AsyncIterator[bytes]) -> dict:
        return await self._post_json("/put", {"path": item["p"], "offset": offset, "size": item["s"],
                                             "hash": item["h"], "mtime": item.get("m") or 0}, chunks)

    async def prune(self, keep: Path) -> dict:
        async def lines():
            for item in read_lines(keep):
                yield item["p"].encode() + b"\n"
        return await self._post_json("/prune", {}, lines())


# --- the copy --------------------------------------------------------------------------

def _now_text() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class Copy:
    """A copy of one tree, its rounds and their state in its work folder
    (state.json, the listings and the plan of each round)."""

    def __init__(self, work: Path):
        self.work = work

    @property
    def state_path(self) -> Path:
        return self.work / STATE_FILE

    def state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict) -> None:
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def _plan(self, n: int) -> Path:
        return self.work / f"plan-{n}.ndjson"

    async def _new_round(self, source: Source, sink: Sink, log, prune: bool = False) -> dict:
        before = self.state()
        n = int(before.get("round", -1)) + 1 if before.get("phase") == "done" else int(before.get("round") or 0)
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        theirs, ours = self.work / f"source-{n}.ndjson", self.work / f"sink-{n}.ndjson"
        log(f"--- round {n}: the source's listing and the receiver's")
        started = time.monotonic()
        listed, have = await asyncio.gather(source.listing(theirs), sink.listing(ours))
        log(f"The source has {listed['files']} files ({_mb(listed['bytes'])} MB), the receiver {have['files']} "
            f"({_mb(have['bytes'])} MB): listed in {time.monotonic() - started:.1f} s")
        batches = files = size = extra = 0
        with open(os.open(self._plan(n), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            changes = list_changes(theirs, ours)
            counter = {"extra": 0}

            def counted():
                for kind, item in changes:
                    if kind == "extra":
                        counter["extra"] += 1
                    yield kind, item
            if source.kind == "files":
                for kind, item in counted():
                    if kind != "extra":
                        fh.write(json.dumps({"files": [item], "bytes": item["s"]}) + "\n")
                        batches, files, size = batches + 1, files + 1, size + item["s"]
            else:
                for batch, bytes_ in plan(counted()):
                    fh.write(json.dumps({"files": batch, "bytes": bytes_}) + "\n")
                    batches, files, size = batches + 1, files + len(batch), size + bytes_
            extra = counter["extra"]
        state = {"round": n, "phase": "copying", "batches": batches, "done": [], "files": files, "bytes": size,
                 "extra": extra, "copied_files": 0, "copied_bytes": 0, "refused": [], "started_at": _now_text(),
                 "seconds": 0.0, "source_files": listed["files"], "source_bytes": listed["bytes"],
                 **{k: before[k] for k in ("finished", "finished_at", "last") if k in before}}
        self._save(state)
        log(f"Round {n}: {files} files to copy ({_mb(size)} MB) in {batches} batches; {extra} files the receiver has "
            + ("more are deleted at its end" if prune else "more stay until the last round"))
        return state

    async def run(self, source: Source, sink: Sink, log=lambda text: None, prune: bool = False,
                  flows: int = 2, stop_after: int | None = None) -> dict:
        """One round, or the rest of the one under way: its batches copied, the
        state written after each; prune: the files the receiver has more
        deleted at its end (the last round, at a stopped source). stop_after:
        at most that many batches in this run (the tests' interruption)."""
        if source.kind != sink.kind:
            raise TreeError("the two ends of a copy are not the same kind of tree")
        state = self.state()
        if state.get("phase") != "copying":
            state = await self._new_round(source, sink, log, prune)
        else:
            log(f"--- round {state['round']} goes on: {len(state['done'])} of {state['batches']} batches were done")
        n = state["round"]
        done = set(state["done"])
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, min(flows, FLOWS)))
        budget = {"left": stop_after}
        # the seconds of the round, counted with every batch: a run killed keeps those it took
        clock = {"mark": time.monotonic()}

        def tick(found: dict) -> None:
            now = time.monotonic()
            found["seconds"] = round(float(found.get("seconds") or 0) + now - clock["mark"], 1)
            clock["mark"] = now

        async def one(index: int, batch: dict):
            async with sem:
                if budget["left"] is not None:
                    if budget["left"] <= 0:
                        return
                    budget["left"] -= 1
                if source.kind == "files":
                    report = await _copy_file(source, sink, batch["files"][0])
                else:
                    async with source.read([item["p"] for item in batch["files"]]) as stream:
                        report = await sink.write(stream)
                    if not report.get("complete"):
                        raise TreeError(f"a batch arrived cut ({report.get('error') or 'no reason'}): it is "
                                        "copied again")
                async with lock:
                    done.add(index)
                    state["done"] = sorted(done)
                    state["copied_files"] += int(report.get("written") or 0)
                    state["copied_bytes"] += int(report.get("bytes") or 0)
                    for refused in report.get("refused") or []:
                        if len(state["refused"]) < 100:
                            state["refused"].append(refused)
                        log(f"Refused by the receiver: {refused.get('p')}: {refused.get('why')}")
                    tick(state)
                    self._save(state)
                    log(f"Batch {len(done)} of {state['batches']}: {int(report.get('written') or 0)} files, "
                        f"{_mb(int(report.get('bytes') or 0))} MB")

        tasks = []
        with open(self._plan(n), encoding="utf-8") as fh:
            for index, raw in enumerate(fh):
                if index not in done:
                    tasks.append(asyncio.ensure_future(one(index, json.loads(raw))))
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            state = self.state()
            tick(state)
            self._save(state)
            raise
        tick(state)
        if len(done) < state["batches"]:
            self._save(state)
            return {**state, "stopped": True}
        if prune:
            pruned = await sink.prune(self.work / f"source-{n}.ndjson")
            state["pruned"] = pruned
            log(f"The receiver deleted {pruned.get('removed', 0)} files the source no longer has "
                f"({_mb(int(pruned.get('bytes') or 0))} MB)")
        state.update(phase="done", finished=time.time(), finished_at=_now_text(),
                     last={"round": n, "files": state["copied_files"], "bytes": state["copied_bytes"],
                           "seconds": state["seconds"]})
        self._save(state)
        for old in self.work.glob("*.ndjson"):
            if not old.name.endswith(f"-{n}.ndjson"):
                old.unlink(missing_ok=True)
        rate = state["copied_bytes"] / state["seconds"] / (1 << 20) if state["seconds"] else 0.0
        log(f"Round {n} done: {state['copied_files']} files, {_mb(state['copied_bytes'])} MB in {state['seconds']} s "
            f"({rate:.1f} MB/s)")
        return {**state, "rate_mbs": round(rate, 1)}


def list_changes(theirs: Path, ours: Path) -> Iterator[tuple[str, dict]]:
    return diff(read_lines(theirs), read_lines(ours))


async def _copy_file(source: Source, sink: Sink, item: dict) -> dict:
    """One file of a "files" tree, from where the receiver's part ends."""
    offset = await sink.have(item["p"])
    if offset > item["s"]:
        offset = 0
    async with source.fetch(item["p"], offset) as stream:
        done = await sink.put(item, offset, stream)
    if not done.get("done"):
        raise TreeError(f"{item['p']} arrived cut at {done.get('size')} bytes of {item['s']}: it goes on from there")
    return {"written": 1, "bytes": item["s"] - offset}


def _mb(size: int) -> str:
    return f"{size / (1 << 20):.1f}"
