from __future__ import annotations

import sqlite3
from pathlib import Path

FLOWSTATE_DIR = ".flowstate"
DB_FILENAME = "flowstate.db"
OBJECTS_DIR = "objects"

SCHEMA_SQL = """
-- Core metadata for the project
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,           -- e.g., "v1", "v2", "v3"
    parent1_id INTEGER,           -- Link to first parent
    parent2_id INTEGER,           -- Link to second parent (for merges)
    root_tree_hash TEXT NOT NULL, -- Hash of the project root directory
    message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent1_id) REFERENCES versions(id),
    FOREIGN KEY (parent2_id) REFERENCES versions(id)
);

-- Maps hashes to actual file content (Content-Addressable Storage)
-- In a real app, blobs would be stored as files in .flowstate/objects/
CREATE TABLE IF NOT EXISTS objects (
    hash TEXT PRIMARY KEY,        -- SHA-256 hash of content
    content BLOB                  -- Compressed file data
);

-- Describes the file structure for each version
CREATE TABLE IF NOT EXISTS tree_entries (
    tree_hash TEXT NOT NULL,      -- Hash representing a specific folder state
    file_name TEXT NOT NULL,
    object_hash TEXT NOT NULL,    -- Link to objects table
    is_dir BOOLEAN DEFAULT 0,
    PRIMARY KEY (tree_hash, file_name)
);
"""


def flowstate_path(project_root: Path) -> Path:
    return Path(project_root).resolve() / FLOWSTATE_DIR


def database_path(project_root: Path) -> Path:
    return flowstate_path(project_root) / DB_FILENAME


def objects_path(project_root: Path) -> Path:
    return flowstate_path(project_root) / OBJECTS_DIR


def initialize_database(project_root: Path) -> Path:
    project_root = Path(project_root).resolve()
    flow_dir = flowstate_path(project_root)
    flow_dir.mkdir(parents=True, exist_ok=True)
    objects_path(project_root).mkdir(parents=True, exist_ok=True)

    db_path = database_path(project_root)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()

    return db_path


def is_initialized(project_root: Path) -> bool:
    return database_path(project_root).exists()


REQUIRED_TABLES = ("versions", "objects", "tree_entries")


def verify_schema(project_root: Path) -> tuple[bool, str | None]:
    """Return (ok, error). Checks DB exists and contains the expected tables."""

    db_path = database_path(project_root)
    if not db_path.exists():
        return False, f"FlowState database not found at {db_path}."

    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return False, f"FlowState database appears corrupted: {exc}"

    table_names = {row[0] for row in rows}
    missing = [t for t in REQUIRED_TABLES if t not in table_names]
    if missing:
        return False, f"FlowState schema is incomplete; missing tables: {', '.join(missing)}."

    return True, None


def get_connection(project_root: Path) -> sqlite3.Connection:
    db_path = database_path(project_root)
    if not db_path.exists():
        raise FileNotFoundError(
            f"FlowState database not found at {db_path}. Run `flow init` first."
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
