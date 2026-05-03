from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from database import initialize_database
from engine import FlowStateEngine, FlowStateError


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_flowstateignore_is_supported(tmp_path: Path) -> None:
    initialize_database(tmp_path)
    engine = FlowStateEngine(tmp_path)

    (tmp_path / ".flowstateignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored v1\n", encoding="utf-8")
    engine.take_snapshot("first")

    (tmp_path / "ignored.txt").write_text("ignored v2\n", encoding="utf-8")
    assert engine.is_dirty() is False


def test_object_sha_verification_detects_corruption(tmp_path: Path) -> None:
    initialize_database(tmp_path)
    engine = FlowStateEngine(tmp_path)

    (tmp_path / "file.txt").write_text("original\n", encoding="utf-8")
    version = engine.take_snapshot("first")

    with engine._conn() as conn:
        files = engine._version_file_map(conn, int(version["id"]))
        object_hash = files["file.txt"]
        conn.execute(
            "UPDATE objects SET content = ? WHERE hash = ?",
            (sqlite3.Binary(b"corrupted"), object_hash),
        )

    object_path = engine._object_file_path(object_hash)
    object_path.unlink()

    with pytest.raises(FlowStateError, match="SHA-256"):
        with engine._conn() as conn:
            engine._get_object_content(conn, object_hash)


def test_first_snapshot_is_v1(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "hello")
    version = engine.take_snapshot("first")
    assert version is not None
    assert version["name"] == "v1"
    assert version["parent1_id"] is None
    assert version["parent2_id"] is None


def test_only_if_changed_skips_identical_snapshot(
    engine: FlowStateEngine, project_root: Path
) -> None:
    _write(project_root / "a.txt", "hello")
    engine.take_snapshot("first")
    same = engine.take_snapshot("noop", only_if_changed=True)
    assert same is None


def test_history_is_chronological(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "hello")
    engine.take_snapshot("v1")
    _write(project_root / "a.txt", "hello world")
    engine.take_snapshot("v2")

    history = engine.get_history()
    names = [item["name"] for item in history]
    assert names == ["v1", "v2"]


def test_checkout_restores_files(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "first")
    engine.take_snapshot("v1")
    _write(project_root / "a.txt", "second")
    engine.take_snapshot("v2")

    engine.checkout("v1", create_safety=False)
    assert (project_root / "a.txt").read_text(encoding="utf-8") == "first"


def test_checkout_removes_files_added_after_target(
    engine: FlowStateEngine, project_root: Path
) -> None:
    _write(project_root / "a.txt", "alpha")
    engine.take_snapshot("v1")
    _write(project_root / "b.txt", "beta")
    engine.take_snapshot("v2")

    engine.checkout("v1", create_safety=False)
    assert (project_root / "a.txt").exists()
    assert not (project_root / "b.txt").exists()


def test_merge_no_conflict_when_disjoint_changes(
    engine: FlowStateEngine, project_root: Path
) -> None:
    _write(project_root / "a.txt", "base")
    engine.take_snapshot("v1")
    _write(project_root / "b.txt", "only-in-v2")
    engine.take_snapshot("v2")

    merged = engine.smart_merge("v1", "v2", "merge")
    assert merged["conflicts"] == []
    assert merged["parent1_id"] == 1
    assert merged["parent2_id"] == 2


def test_merge_text_conflict_creates_markers(
    engine: FlowStateEngine, project_root: Path
) -> None:
    _write(project_root / "a.txt", "base")
    engine.take_snapshot("base")
    _write(project_root / "a.txt", "from-A")
    engine.take_snapshot("vA")

    engine.checkout("v1", create_safety=False)
    _write(project_root / "a.txt", "from-B")
    engine.take_snapshot("vB")

    merged = engine.smart_merge("v2", "v3", "merge")
    assert "a.txt" in merged["conflicts"]

    engine.checkout(merged["id"], create_safety=False)
    merged_text = (project_root / "a.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" in merged_text and ">>>>>>>" in merged_text


def test_describe_diff(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "alpha")
    engine.take_snapshot("v1")
    _write(project_root / "a.txt", "alpha-mod")
    _write(project_root / "b.txt", "beta")
    engine.take_snapshot("v2")

    diff = engine.describe_diff("v1", "v2")
    assert diff["added"] == ["b.txt"]
    assert diff["modified"] == ["a.txt"]
    assert diff["removed"] == []


def test_dirty_flag(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "alpha")
    engine.take_snapshot("v1")
    assert engine.is_dirty() is False
    _write(project_root / "a.txt", "alpha-mod")
    assert engine.is_dirty() is True


def test_cas_dedupes_identical_files(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "same")
    _write(project_root / "b.txt", "same")
    engine.take_snapshot("v1")

    objects_dir = project_root / ".flowstate" / "objects"
    blob_files = [p for p in objects_dir.rglob("*") if p.is_file()]
    assert len(blob_files) == 1, "Identical contents must be stored only once."


def test_history_after_init_is_empty(engine: FlowStateEngine) -> None:
    assert engine.get_history() == []


def test_checkout_unknown_version_raises(engine: FlowStateEngine, project_root: Path) -> None:
    _write(project_root / "a.txt", "alpha")
    engine.take_snapshot("v1")
    with pytest.raises(FlowStateError):
        engine.checkout("does-not-exist")


def test_flowstateignore_alias_excludes_files(
    engine: FlowStateEngine, project_root: Path
) -> None:
    _write(project_root / "keep.txt", "ok")
    _write(project_root / "secret.log", "shh")
    _write(project_root / ".flowstateignore", "*.log\n")

    engine.take_snapshot("v1")
    history = engine.get_history()
    assert len(history) == 1

    engine.checkout("v1", create_safety=False)
    assert (project_root / "keep.txt").exists()
    assert not (project_root / "secret.log").exists()
