from __future__ import annotations

from pathlib import Path

from database import (
    database_path,
    initialize_database,
    is_initialized,
    verify_schema,
)


def test_is_initialized_false_for_empty_dir(tmp_path: Path) -> None:
    assert is_initialized(tmp_path) is False


def test_initialize_creates_db_and_schema(tmp_path: Path) -> None:
    db = initialize_database(tmp_path)
    assert db.exists()
    assert is_initialized(tmp_path) is True
    ok, error = verify_schema(tmp_path)
    assert ok is True, error


def test_verify_schema_detects_corrupted_db(tmp_path: Path) -> None:
    initialize_database(tmp_path)
    db = database_path(tmp_path)
    db.write_bytes(b"not a sqlite database")
    ok, error = verify_schema(tmp_path)
    assert ok is False
    assert error is not None
    assert "corrupted" in error.lower() or "incomplete" in error.lower()
