# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The copy engine of the handover and of the move (treesync.py,
docs/adr/AD-008-consegna-e-spostamento.md AD-010): the manifests and their
order, root's reading and writing of a filestore (no link followed, every
name checked, a file of the filestore checked against its name), the rounds,
a copy killed halfway that goes on from its batch, the second round that
copies what changed only, a file resumed from its part. On temporary
folders, without root and without a network."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from omniacli import treesync, workspacehelper
from omniacli.composehelper import HelperError
from omniacli.treesync import TreeError


def put(top: Path, rel: str, data: bytes) -> str:
    path = top / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def stored(top: Path, data: bytes) -> str:
    """A file of Odoo's filestore: its name the SHA-1 of its content."""
    digest = hashlib.sha1(data).hexdigest()
    return put(top, f"{digest[:2]}/{digest}", data)


def listing(top: Path) -> list[dict]:
    out = io.StringIO()
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        treesync.list_tree(fd, out)
    finally:
        os.close(fd)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def same(a: Path, b: Path) -> bool:
    """The two trees hold the same files (their mtime to the second: a tar keeps no more)."""
    def strip(top):
        return [{k: v for k, v in e.items() if k != "m"} for e in listing(top)]
    return strip(a) == strip(b)


def tar_of(top: Path, paths) -> bytes:
    out = io.BytesIO()
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        treesync.read_tree(fd, paths, out)
    finally:
        os.close(fd)
    return out.getvalue()


def write(top: Path, data: bytes) -> dict:
    top.mkdir(parents=True, exist_ok=True)
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return treesync.write_tree(fd, io.BytesIO(data), os.getuid(), os.getgid())
    finally:
        os.close(fd)


# --- names and manifests ---------------------------------------------------------------

def test_a_path_under_a_tree_never_leads_out_of_it():
    assert treesync.safe_rel("ab/" + "a" * 40) == "ab/" + "a" * 40
    for bad in ("", "/etc/passwd", "../x", "a/../../x", "a//b", "a/./b", "a\\b", "a/b\n", "x" * 1100,
                "a/b.omniacli-part", None):
        with pytest.raises(TreeError):
            treesync.safe_rel(bad)


def test_the_manifest_is_in_the_order_of_its_paths_and_a_filestore_file_is_not_read(tmp_path):
    top = tmp_path / "fs"
    a = stored(top, b"first attachment")
    put(top, "a.b", b"x")
    put(top, "a/z", b"y")
    put(top, "checklist/ab/" + "c" * 40, b"")
    (top / "link").symlink_to("/etc/passwd")
    (top / "dirlink").symlink_to(tmp_path)
    found = listing(top)
    paths = [e["p"] for e in found]
    # the order Python compares the whole paths in: "a.b" before "a/z", a folder walked in its place
    assert paths == sorted(paths) and "a.b" in paths and paths.index("a.b") < paths.index("a/z")
    # links are never listed, nor followed
    assert not any("link" in p for p in paths) and len(paths) == 4
    by = {e["p"]: e for e in found}
    assert by[a]["h"] == f"sha1:{a.split('/')[1]}"
    assert by["a.b"]["h"] == "sha256:" + hashlib.sha256(b"x").hexdigest() and by["a.b"]["s"] == 1
    # an empty mark of Odoo's garbage collection is no file of the filestore: hashed
    assert by["checklist/ab/" + "c" * 40]["h"].startswith("sha256:")


def test_two_manifests_say_what_is_new_changed_and_more(tmp_path):
    def e(p, s=1, h="sha256:" + "0" * 64):
        return treesync.entry(p, s, h, 0)
    source = [e("a"), e("b", 2), e("c"), e("e")]
    sink = [e("a"), e("b", 3), e("d"), e("e", h="sha256:" + "1" * 64)]
    assert [(k, i["p"]) for k, i in treesync.diff(source, sink)] == [
        ("changed", "b"), ("new", "c"), ("extra", "d"), ("changed", "e")]
    with pytest.raises(TreeError, match="not in order"):
        list(treesync.diff([e("b"), e("a")], []))
    # batches of a size: a file larger than the limit goes alone
    changes = [("new", e("1", 60)), ("new", e("2", 50)), ("extra", e("x")), ("new", e("3", 200)), ("new", e("4", 10))]
    assert [([i["p"] for i in b], n) for b, n in treesync.plan(changes, limit=100)] == [
        (["1"], 60), (["2"], 50), (["3"], 200), (["4"], 10)]
    assert [len(b) for b, _ in treesync.plan(changes, limit=1000, most=2)] == [2, 2]


# --- root's reading and writing ----------------------------------------------------------

def test_a_tar_of_the_filestore_is_written_whole_and_given_to_its_owner(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    names = [stored(src, os.urandom(3000)) for _ in range(5)] + [put(src, "checklist/x", b"")]
    data = tar_of(src, names + ["gone/" + "f" * 40])
    report = write(dst, data)
    assert report["complete"] and report["written"] == 6 and report["refused"] == []
    assert same(dst, src)
    # nothing half written stays beside them
    assert not list(dst.rglob("*" + treesync.PART))
    assert (dst / names[0]).stat().st_mode & 0o777 == 0o644


def test_a_file_that_is_not_its_name_a_link_and_a_path_out_are_refused(tmp_path):
    dst = tmp_path / "dst"
    out = io.BytesIO()
    good = hashlib.sha1(b"good").hexdigest()
    altered = hashlib.sha1(b"original").hexdigest()
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for name, data in ((f"{good[:2]}/{good}", b"good"), (f"{altered[:2]}/{altered}", b"altered on the way"),
                           ("../escaped", b"x"), ("/etc/evil", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("ab/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
        tar.addfile(link)
        hard = tarfile.TarInfo("ab/hard")
        hard.type, hard.linkname = tarfile.LNKTYPE, "../../etc/shadow"
        tar.addfile(hard)
    report = write(dst, out.getvalue())
    refused = {r["p"]: r["why"] for r in report["refused"]}
    assert report["written"] == 1 and (dst / good[:2] / good).read_bytes() == b"good"
    assert "SHA-1" in refused[f"{altered[:2]}/{altered}"] and not (dst / altered[:2] / altered).exists()
    assert "not a path under the tree" in refused["../escaped"] and "not a path under the tree" in refused["/etc/evil"]
    assert "a link or a special file" in refused["ab/link"] and "a link or a special file" in refused["ab/hard"]
    assert not (tmp_path / "escaped").exists() and not (dst / "ab" / "link").exists()


def test_a_link_put_in_the_receivers_filestore_is_never_followed(tmp_path):
    dst, outside = tmp_path / "dst", tmp_path / "outside"
    outside.mkdir()
    dst.mkdir()
    data = b"an attachment"
    digest = hashlib.sha1(data).hexdigest()
    # the receiver's Odoo made its folder a link out of the filestore
    (dst / digest[:2]).symlink_to(outside)
    src = tmp_path / "src"
    stored(src, data)
    report = write(dst, tar_of(src, [f"{digest[:2]}/{digest}"]))
    assert report["written"] == 0 and "folder" in report["refused"][0]["why"]
    assert list(outside.iterdir()) == []
    # nor read through, at the source
    (src / "zz").symlink_to(outside)
    (outside / ("z" * 40)).write_bytes(b"secret")
    assert tarfile.open(fileobj=io.BytesIO(tar_of(src, ["zz/" + "z" * 40]))).getmembers() == []


def test_a_stream_cut_halfway_keeps_what_was_whole_and_drops_the_rest(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    names = [stored(src, os.urandom(200_000)) for _ in range(3)]
    data = tar_of(src, names)
    report = write(dst, data[:len(data) // 2])
    assert report["complete"] is False and report["written"] >= 1 and report["error"]
    whole = [n for n in names if (dst / n).exists()]
    assert len(whole) == report["written"]
    # what was written is whole, and nothing half written stays
    assert all(hashlib.sha1((dst / n).read_bytes()).hexdigest() == n.split("/")[1] for n in whole)
    assert not list(dst.rglob("*" + treesync.PART))


def test_prune_deletes_what_the_source_no_longer_has(tmp_path):
    top = tmp_path / "t"
    for name in ("a", "b/c", "b/d", "e"):
        put(top, name, b"x")
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert treesync.prune_tree(fd, ["a", "b/d"]) == {"removed": 2, "bytes": 2}
    finally:
        os.close(fd)
    assert sorted(p.relative_to(top).as_posix() for p in top.rglob("*") if p.is_file()) == ["a", "b/d"]


# --- the copy: rounds, resumption, delta ---------------------------------------------------

class InProcess(treesync.Source, treesync.Sink):
    """A root tree read and written by treesync's own functions in this
    process (what the root helper runs), counting the bytes it sent."""
    kind = "root"

    def __init__(self, top: Path):
        self.top, self.sent, self.writes = top, 0, 0
        top.mkdir(parents=True, exist_ok=True)

    async def listing(self, into: Path) -> dict:
        found = listing(self.top)
        into.write_text("".join(treesync.line_of(e) for e in found))
        return {"files": len(found), "bytes": sum(e["s"] for e in found)}

    @contextlib.asynccontextmanager
    async def read(self, paths):
        data = tar_of(self.top, paths)
        self.sent += len(data)

        async def body():
            for i in range(0, len(data), 65536):
                yield data[i:i + 65536]
        yield body()

    async def write(self, chunks) -> dict:
        self.writes += 1
        return write(self.top, b"".join([c async for c in chunks]))

    async def prune(self, keep: Path) -> dict:
        fd = os.open(self.top, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return treesync.prune_tree(fd, (e["p"] for e in treesync.read_lines(keep)))
        finally:
            os.close(fd)


@pytest.fixture
def filestore(tmp_path, monkeypatch):
    monkeypatch.setattr(treesync, "BATCH_BYTES", 100_000)
    src = InProcess(tmp_path / "src")
    for _ in range(12):
        stored(src.top, os.urandom(40_000))
    return src


def test_a_copy_killed_halfway_goes_on_from_its_batch(filestore, tmp_path):
    dst, work = InProcess(tmp_path / "dst"), tmp_path / "work"
    copy = treesync.Copy(work)
    said = []
    first = asyncio.run(copy.run(filestore, dst, said.append, flows=1, stop_after=2))
    state = copy.state()
    # 12 files of 40 kB in batches of 100 kB: 6 batches, 2 done, the state written after each
    assert first["stopped"] and state["phase"] == "copying" and state["batches"] == 6 and state["done"] == [0, 1]
    assert len(listing(dst.top)) == 4 and dst.writes == 2
    # the job killed: a new one goes on from the third batch, nothing copied twice
    again = asyncio.run(treesync.Copy(work).run(filestore, dst, said.append, flows=3))
    assert again["phase"] == "done" and again["round"] == 0 and dst.writes == 6
    assert same(dst.top, filestore.top) and again["copied_files"] == 12
    assert any("goes on: 2 of 6 batches were done" in line for line in said)
    assert treesync.in_step(treesync.Copy(work).state())


def test_a_copy_cancelled_keeps_its_batches_done(filestore, tmp_path):
    dst, work = InProcess(tmp_path / "dst"), tmp_path / "work"
    original = dst.write

    async def slow_after_two(chunks):
        if dst.writes >= 2:
            await asyncio.sleep(10)
        return await original(chunks)
    dst.write = slow_after_two

    async def run_and_cancel():
        task = asyncio.ensure_future(treesync.Copy(work).run(filestore, dst, flows=1))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(treesync.Copy(work).state().get("done") or []) == 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run_and_cancel())
    assert treesync.Copy(work).state()["done"] == [0, 1]
    dst.write = original
    done = asyncio.run(treesync.Copy(work).run(filestore, dst))
    assert done["phase"] == "done" and same(dst.top, filestore.top)


def test_the_second_round_copies_only_what_changed_and_the_last_prunes(filestore, tmp_path):
    dst, work = InProcess(tmp_path / "dst"), tmp_path / "work"
    asyncio.run(treesync.Copy(work).run(filestore, dst))
    sent = filestore.sent
    new = [stored(filestore.top, os.urandom(30_000)) for _ in range(2)]
    gone = sorted(p["p"] for p in listing(filestore.top) if p["p"] not in new)[0]
    (filestore.top / gone).unlink()
    second = asyncio.run(treesync.Copy(work).run(filestore, dst))
    # the delta: 2 new files, one batch, about 60 kB and the tar's own; the one the source no longer has stays until the last round
    assert second["round"] == 1 and second["copied_files"] == 2 and second["batches"] == 1 and second["extra"] == 1
    assert filestore.sent - sent < 80_000 and (dst.top / gone).exists()
    last = asyncio.run(treesync.Copy(work).run(filestore, dst, prune=True))
    assert last["round"] == 2 and last["copied_files"] == 0 and last["pruned"]["removed"] == 1
    assert same(dst.top, filestore.top)
    # the listings of the rounds before are not kept
    assert sorted(p.name for p in work.glob("*.ndjson")) == ["plan-2.ndjson", "sink-2.ndjson", "source-2.ndjson"]


# --- the files OmniaCLI reads itself: a part resumed, its SHA-256 ------------------------

def test_a_backup_goes_on_from_its_part_and_is_checked_whole(tmp_path):
    src, dst = tmp_path / "backups-src", tmp_path / "backups-dst"
    data = os.urandom(3 * treesync.CHUNK + 123)
    put(src, "erp_20261123_0200.zip", data)
    put(src, ".omniacli-backups.json", b"{}")
    source = treesync.LocalFiles(src, tmp_path / "hashes.json")
    sink = treesync.LocalFiles(dst, tmp_path / "hashes-dst.json")
    item = asyncio.run(_listed(source, tmp_path))["erp_20261123_0200.zip"]
    # a first try that broke after 2 MB: its part stays
    with pytest.raises(TreeError, match="arrived cut"):
        asyncio.run(_cut(source, sink, item, 2 * treesync.CHUNK))
    assert treesync.part_size(dst, item["p"]) == 2 * treesync.CHUNK
    copied = asyncio.run(treesync._copy_file(source, sink, item))
    # the rest only, from where the part ends
    assert copied["bytes"] == len(data) - 2 * treesync.CHUNK and (dst / item["p"]).read_bytes() == data
    assert (dst / item["p"]).stat().st_mode & 0o777 == 0o600 and treesync.part_size(dst, item["p"]) == 0
    # one that arrives different is dropped, never kept
    with pytest.raises(TreeError, match="arrived different"):
        treesync.put_part(dst, "other.zip", 0, [b"abc"], 3, "sha256:" + "0" * 64)
    assert not (dst / "other.zip").exists() and treesync.part_size(dst, "other.zip") == 0
    # an offset that is not where the part ends is refused
    with pytest.raises(TreeError, match="bytes are here, not"):
        treesync.put_part(dst, "third.zip", 5, [b"abc"], 8, "sha256:" + "0" * 64)


async def _listed(source, tmp_path):
    into = tmp_path / "l.ndjson"
    await source.listing(into)
    return {e["p"]: e for e in treesync.read_lines(into)}


async def _cut(source, sink, item, upto):
    async with source.fetch(item["p"], 0) as stream:
        async def cut():
            sent = 0
            async for chunk in stream:
                if sent >= upto:
                    return
                yield chunk
                sent += len(chunk)
        done = await sink.put(item, 0, cut())
    if not done["done"]:
        raise TreeError("arrived cut")


def test_a_backup_is_hashed_once(tmp_path, monkeypatch):
    src = tmp_path / "b"
    put(src, "a.zip", b"abc")
    cache = tmp_path / "hashes.json"
    calls = []
    real = treesync._sha256_at
    monkeypatch.setattr(treesync, "_sha256_at", lambda fd, name: calls.append(name) or real(fd, name))
    for _ in range(3):
        asyncio.run(_listed(treesync.LocalFiles(src, cache), tmp_path))
    assert calls == ["a.zip"] and json.loads(cache.read_text())["a.zip"][3].startswith("sha256:")
    put(src, "a.zip", b"abcd")
    asyncio.run(_listed(treesync.LocalFiles(src, cache), tmp_path))
    assert calls == ["a.zip", "a.zip"]


def test_a_files_tree_copied_in_rounds(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    put(src, "one.zip", os.urandom(5000))
    put(src, "two.zip", os.urandom(7000))
    source, sink = treesync.LocalFiles(src, tmp_path / "h1.json"), treesync.LocalFiles(dst, tmp_path / "h2.json")
    first = asyncio.run(treesync.Copy(tmp_path / "w").run(source, sink))
    assert first["copied_files"] == 2 and first["copied_bytes"] == 12000
    put(src, "three.zip", os.urandom(100))
    second = asyncio.run(treesync.Copy(tmp_path / "w").run(source, sink))
    assert second["copied_files"] == 1 and second["copied_bytes"] == 100
    assert sorted(p.name for p in dst.iterdir()) == ["one.zip", "three.zip", "two.zip"]


# --- the root helper's actions ----------------------------------------------------------------

@pytest.fixture
def helper_tree(tmp_path, monkeypatch):
    """workspace-helper's view of tsx-dst: its data folder, the filestore of its database below it."""
    data = tmp_path / "project" / "data"
    (data / "filestore" / "filestore").mkdir(parents=True)
    runs = {"odoo": False}
    monkeypatch.setattr(workspacehelper, "_filestore", lambda *args: (
        tmp_path / "project", data, ["filestore", "filestore", "dst"], (os.getuid(), os.getgid())))
    monkeypatch.setattr(workspacehelper, "_odoo_runs", lambda project: runs["odoo"])
    monkeypatch.setattr(workspacehelper.os, "geteuid", lambda: 0)
    return data / "filestore" / "filestore" / "dst", runs


def test_the_helper_writes_a_filestore_only_while_its_odoo_is_stopped(helper_tree, tmp_path, monkeypatch):
    top, runs = helper_tree
    src = tmp_path / "src"
    name = stored(src, b"an attachment")
    monkeypatch.setattr(workspacehelper.sys, "stdin", type("In", (), {"buffer": io.BytesIO(tar_of(src, [name]))})())
    runs["odoo"] = True
    with pytest.raises(HelperError, match="runs: its filestore is written only while it is stopped"):
        workspacehelper.tree_write("tsx", "default", "dst", "production")
    with pytest.raises(HelperError, match="written only while it is stopped"):
        workspacehelper.tree_prune("tsx", "default", "dst", "production")
    assert not top.exists()
    runs["odoo"] = False
    done = workspacehelper.tree_write("tsx", "default", "dst", "production")
    assert done["written"] == 1 and (top / name).read_bytes() == b"an attachment"


def test_the_helper_streams_a_manifest_and_a_tar_and_nothing_after(helper_tree, monkeypatch, capsys):
    top, _runs = helper_tree
    # no filestore yet: an empty manifest, not an error
    assert workspacehelper.main(["tree-list", "tsx", "default", "dst", "production"]) == 0
    assert capsys.readouterr().out == ""
    name = stored(top, b"an attachment")
    assert workspacehelper.main(["tree-list", "tsx", "default", "dst", "production"]) == 0
    assert [json.loads(line)["p"] for line in capsys.readouterr().out.splitlines()] == [name]
    out = io.BytesIO()
    monkeypatch.setattr(workspacehelper.sys, "stdin", io.StringIO(name + "\n"))
    monkeypatch.setattr(workspacehelper.sys, "stdout", type("Out", (), {"buffer": out, "flush": lambda self: None,
                                                                        "write": lambda self, text: None})())
    assert workspacehelper.main(["tree-read", "tsx", "default", "dst", "production"]) == 0
    assert tarfile.open(fileobj=io.BytesIO(out.getvalue())).getnames() == [name]
    assert workspacehelper.STREAMS == {"tree-list", "tree-read", "db-dump"}


def test_the_purge_takes_what_the_copies_kept():
    """The state of the copies (/var/lib/omniacli/treesync) and the firewall's list of the databases that
    arrive go with the package's purge: only the backups stay."""
    postrm = (Path(treesync.__file__).parent.parent / "packaging" / "debian" / "files" / "postrm").read_text()
    purge = postrm.split("    purge)", 1)[1]
    assert "rm -rf /var/lib/omniacli/treesync" in purge and "rm -f /etc/omniacli/db-arrivals.json" in purge
