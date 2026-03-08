from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

from database import (
    FLOWSTATE_DIR,
    get_connection,
    initialize_database,
    is_initialized,
    objects_path,
)


class FlowStateError(Exception):
    pass


class FlowStateEngine:
    MANUAL_VERSION_RE = re.compile(r"^v(\d+)$")
    AUTO_PREFIX = "auto-"
    TEMP_NAME = "temp"
    TEMP_PREFIX = "temp-"

    def __init__(self, project_root: Path | str = ".", auto_init: bool = False) -> None:
        self.project_root = Path(project_root).resolve()
        if auto_init and not is_initialized(self.project_root):
            initialize_database(self.project_root)
        if not is_initialized(self.project_root):
            raise FlowStateError(
                f"FlowState repository not initialized at {self.project_root}. "
                "Run `flow init` first."
            )
        self.objects_dir = objects_path(self.project_root)
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    def _state_file_path(self) -> Path:
        return self.project_root / FLOWSTATE_DIR / "state.json"

    def _read_state(self) -> dict[str, Any]:
        state_path = self._state_file_path()
        if not state_path.exists():
            return {}
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        state_path = self._state_file_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")

    def _get_current_version_id(self) -> Optional[int]:
        state = self._read_state()
        value = state.get("current_version_id")
        return int(value) if isinstance(value, int) or str(value).isdigit() else None

    def _set_current_version_id(self, version_id: int) -> None:
        state = self._read_state()
        state["current_version_id"] = int(version_id)
        self._write_state(state)

    def get_last_temp_save_id(self) -> Optional[int]:
        state = self._read_state()
        value = state.get("last_temp_save_id")
        return int(value) if isinstance(value, int) or str(value).isdigit() else None

    def _set_last_temp_save_id(self, version_id: int) -> None:
        state = self._read_state()
        state["last_temp_save_id"] = int(version_id)
        self._write_state(state)

    def _set_last_temp_protected(self, protected: bool) -> None:
        _ = protected
        state = self._read_state()
        state["protect_last_temp_save"] = False
        self._write_state(state)

    def _is_last_temp_protected(self) -> bool:
        return False

    def _set_squash_root_id(self, version_id: Optional[int]) -> None:
        state = self._read_state()
        if version_id is None:
            state.pop("squash_root_id", None)
        else:
            state["squash_root_id"] = int(version_id)
        self._write_state(state)

    def _get_squash_root_id(self) -> Optional[int]:
        state = self._read_state()
        value = state.get("squash_root_id")
        return int(value) if isinstance(value, int) or str(value).isdigit() else None

    def _current_tree_hash(self) -> str:
        ignore_patterns = self._load_flowignore_patterns()
        return self._compute_tree_hash_from_disk(self.project_root, ignore_patterns)

    def back(self) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM versions
                WHERE name = ? OR name LIKE 'temp-%'
                ORDER BY id DESC
                LIMIT 1
                """,
                (self.TEMP_NAME,),
            ).fetchone()
        if row is None:
            raise FlowStateError("No temporary pre-checkout save found.")
        restored = self.checkout(int(row["id"]), create_safety=False)
        return restored

    @classmethod
    def _is_auto_version_name(cls, name: str) -> bool:
        return name.startswith(cls.AUTO_PREFIX)

    @classmethod
    def _is_manual_version_name(cls, name: str) -> bool:
        return cls.MANUAL_VERSION_RE.match(name) is not None

    @classmethod
    def _is_temp_version_name(cls, name: str) -> bool:
        return name == cls.TEMP_NAME or name.startswith(cls.TEMP_PREFIX)

    def _load_flowignore_patterns(self) -> list[str]:
        ignore_file = self.project_root / ".flowignore"
        if not ignore_file.exists():
            return []

        patterns: list[str] = []
        for raw_line in ignore_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line.replace("\\", "/"))
        return patterns

    def _is_ignored_path(
        self, rel_path: str, is_dir: bool, ignore_patterns: list[str]
    ) -> bool:
        # Always ignore internal storage to prevent recursive snapshots.
        if rel_path == FLOWSTATE_DIR or rel_path.startswith(f"{FLOWSTATE_DIR}/"):
            return True

        path_obj = PurePosixPath(rel_path)
        parts = path_obj.parts

        for raw_pattern in ignore_patterns:
            pattern = raw_pattern.strip().replace("\\", "/")
            if not pattern:
                continue

            if pattern.endswith("/"):
                dir_pattern = pattern.rstrip("/")
                if not dir_pattern:
                    continue

                if "/" in dir_pattern:
                    if rel_path == dir_pattern or rel_path.startswith(f"{dir_pattern}/"):
                        return True
                    if is_dir and fnmatch.fnmatch(rel_path, dir_pattern):
                        return True
                else:
                    if dir_pattern in parts:
                        return True
                continue

            if "/" in pattern:
                if fnmatch.fnmatch(rel_path, pattern):
                    return True
            else:
                if fnmatch.fnmatch(path_obj.name, pattern):
                    return True

        return False

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = get_connection(self.project_root)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _object_file_path(self, object_hash: str) -> Path:
        return self.objects_dir / object_hash[:2] / object_hash[2:]

    def _store_object(self, conn: sqlite3.Connection, content: bytes) -> str:
        object_hash = self._sha256(content)
        compressed = zlib.compress(content)
        conn.execute(
            "INSERT OR IGNORE INTO objects(hash, content) VALUES(?, ?)",
            (object_hash, compressed),
        )

        path = self._object_file_path(object_hash)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(compressed)

        return object_hash

    def _get_object_content(self, conn: sqlite3.Connection, object_hash: str) -> bytes:
        object_file = self._object_file_path(object_hash)
        if object_file.exists():
            data = object_file.read_bytes()
        else:
            row = conn.execute(
                "SELECT content FROM objects WHERE hash = ?", (object_hash,)
            ).fetchone()
            if row is None:
                raise FlowStateError(f"Object {object_hash} is missing from storage.")
            data = row["content"]

        try:
            return zlib.decompress(data)
        except zlib.error:
            return data

    @staticmethod
    def _hash_tree_entries(entries: list[tuple[str, str, bool]]) -> str:
        # Stable tree hash from sorted entries.
        lines = [
            f"{'d' if is_dir else 'f'} {name}\0{obj_hash}"
            for name, obj_hash, is_dir in entries
        ]
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def _persist_tree_entries(
        self,
        conn: sqlite3.Connection,
        tree_hash: str,
        entries: list[tuple[str, str, bool]],
    ) -> None:
        if not entries:
            return
        conn.executemany(
            """
            INSERT OR IGNORE INTO tree_entries(tree_hash, file_name, object_hash, is_dir)
            VALUES(?, ?, ?, ?)
            """,
            [
                (tree_hash, name, object_hash, 1 if is_dir else 0)
                for name, object_hash, is_dir in entries
            ],
        )

    def _build_tree_from_disk(
        self,
        conn: sqlite3.Connection,
        directory: Path,
        ignore_patterns: list[str],
    ) -> str:
        entries: list[tuple[str, str, bool]] = []

        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.is_symlink():
                # Keep behavior deterministic and avoid link traversal surprises.
                continue
            rel_path = child.relative_to(self.project_root).as_posix()
            if self._is_ignored_path(rel_path, child.is_dir(), ignore_patterns):
                continue
            if child.is_dir():
                subtree_hash = self._build_tree_from_disk(conn, child, ignore_patterns)
                entries.append((child.name, subtree_hash, True))
            elif child.is_file():
                object_hash = self._store_object(conn, child.read_bytes())
                entries.append((child.name, object_hash, False))

        entries.sort(key=lambda item: item[0])
        tree_hash = self._hash_tree_entries(entries)
        self._persist_tree_entries(conn, tree_hash, entries)
        return tree_hash

    def _compute_tree_hash_from_disk(
        self, directory: Path, ignore_patterns: list[str]
    ) -> str:
        entries: list[tuple[str, str, bool]] = []
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.is_symlink():
                continue
            rel_path = child.relative_to(self.project_root).as_posix()
            if self._is_ignored_path(rel_path, child.is_dir(), ignore_patterns):
                continue

            if child.is_dir():
                subtree_hash = self._compute_tree_hash_from_disk(child, ignore_patterns)
                entries.append((child.name, subtree_hash, True))
            elif child.is_file():
                entries.append((child.name, self._sha256(child.read_bytes()), False))

        entries.sort(key=lambda item: item[0])
        return self._hash_tree_entries(entries)

    def _persist_tree_node(self, conn: sqlite3.Connection, node: dict[str, Any]) -> str:
        entries: list[tuple[str, str, bool]] = []
        for name in sorted(node.keys()):
            value = node[name]
            if isinstance(value, dict):
                subtree_hash = self._persist_tree_node(conn, value)
                entries.append((name, subtree_hash, True))
            else:
                entries.append((name, value, False))

        tree_hash = self._hash_tree_entries(entries)
        self._persist_tree_entries(conn, tree_hash, entries)
        return tree_hash

    def _build_tree_from_file_map(
        self, conn: sqlite3.Connection, file_map: dict[str, str]
    ) -> str:
        root: dict[str, Any] = {}
        for rel_path, object_hash in sorted(file_map.items()):
            parts = PurePosixPath(rel_path).parts
            if not parts:
                continue
            node = root
            for part in parts[:-1]:
                existing = node.get(part)
                if existing is None:
                    existing = {}
                    node[part] = existing
                if not isinstance(existing, dict):
                    raise FlowStateError(f"Tree path conflict at {rel_path}.")
                node = existing
            node[parts[-1]] = object_hash

        return self._persist_tree_node(conn, root)

    def _collect_files_from_tree(
        self, conn: sqlite3.Connection, tree_hash: str, prefix: str = ""
    ) -> dict[str, str]:
        files: dict[str, str] = {}
        rows = conn.execute(
            """
            SELECT file_name, object_hash, is_dir
            FROM tree_entries
            WHERE tree_hash = ?
            ORDER BY file_name ASC
            """,
            (tree_hash,),
        ).fetchall()

        for row in rows:
            name = row["file_name"]
            child_hash = row["object_hash"]
            rel_path = f"{prefix}/{name}" if prefix else name
            if row["is_dir"]:
                files.update(self._collect_files_from_tree(conn, child_hash, rel_path))
            else:
                files[rel_path] = child_hash
        return files

    def _version_row(
        self, conn: sqlite3.Connection, version_ref: int | str
    ) -> sqlite3.Row:
        if isinstance(version_ref, int) or str(version_ref).isdigit():
            row = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (int(version_ref),)
            ).fetchone()
            if row is not None:
                return row

        row = conn.execute(
            "SELECT * FROM versions WHERE name = ?", (str(version_ref),)
        ).fetchone()
        if row is None:
            raise FlowStateError(f"Version '{version_ref}' does not exist.")
        return row

    def resolve_version_id(self, version_ref: int | str) -> int:
        with self._conn() as conn:
            return int(self._version_row(conn, version_ref)["id"])

    def _version_file_map(self, conn: sqlite3.Connection, version_id: int) -> dict[str, str]:
        row = self._version_row(conn, version_id)
        return self._collect_files_from_tree(conn, row["root_tree_hash"])

    def _latest_version_row(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        return conn.execute("SELECT * FROM versions ORDER BY id DESC LIMIT 1").fetchone()

    def _current_version_row(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        current_version_id = self._get_current_version_id()
        if current_version_id is not None:
            row = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (current_version_id,)
            ).fetchone()
            if row is not None:
                return row
        return self._latest_version_row(conn)

    def _latest_manual_version_row(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        rows = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
        for row in rows:
            if self._is_manual_version_name(row["name"]):
                return row
        return None

    def _latest_non_temp_version_row(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        rows = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
        for row in rows:
            if not self._is_temp_version_name(str(row["name"])):
                return row
        return None

    def _next_manual_version_number(self, conn: sqlite3.Connection) -> int:
        rows = conn.execute("SELECT name FROM versions").fetchall()
        max_number = 0
        for row in rows:
            match = self.MANUAL_VERSION_RE.match(row["name"])
            if match:
                max_number = max(max_number, int(match.group(1)))
        return max_number + 1

    def _next_auto_version_name(self, conn: sqlite3.Connection) -> str:
        base_name = f"{self.AUTO_PREFIX}{datetime.now().strftime('%Y%m%d-%H%M')}"
        candidate = base_name
        index = 1
        while conn.execute(
            "SELECT 1 FROM versions WHERE name = ? LIMIT 1", (candidate,)
        ).fetchone():
            candidate = f"{base_name}-{index:02d}"
            index += 1
        return candidate

    def _next_temp_version_name(
        self, conn: sqlite3.Connection, target_version_id: Optional[int]
    ) -> str:
        target_text = str(target_version_id) if target_version_id is not None else "unknown"
        base_name = f"temp-{target_text}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        candidate = base_name
        index = 1
        while conn.execute(
            "SELECT 1 FROM versions WHERE name = ? LIMIT 1", (candidate,)
        ).fetchone():
            candidate = f"{base_name}-{index:02d}"
            index += 1
        return candidate

    def _upsert_single_temp_version(
        self,
        conn: sqlite3.Connection,
        root_tree_hash: str,
        parent1_id: Optional[int],
        message: str,
    ) -> tuple[dict[str, Any], set[int]]:
        rows = conn.execute(
            """
            SELECT id
            FROM versions
            WHERE name = ? OR name LIKE 'temp-%'
            ORDER BY id DESC
            """,
            (self.TEMP_NAME,),
        ).fetchall()

        stale_ids: set[int] = set()
        if rows:
            keep_id = int(rows[0]["id"])
            stale_ids = {int(row["id"]) for row in rows[1:]}
            for stale_id in sorted(stale_ids):
                conn.execute(
                    """
                    UPDATE versions
                    SET parent1_id = CASE WHEN parent1_id = ? THEN NULL ELSE parent1_id END,
                        parent2_id = CASE WHEN parent2_id = ? THEN NULL ELSE parent2_id END
                    WHERE parent1_id = ? OR parent2_id = ?
                    """,
                    (stale_id, stale_id, stale_id, stale_id),
                )
            if stale_ids:
                conn.executemany(
                    "DELETE FROM versions WHERE id = ?",
                    [(stale_id,) for stale_id in sorted(stale_ids)],
                )

            saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                UPDATE versions
                SET name = ?,
                    parent1_id = ?,
                    parent2_id = NULL,
                    root_tree_hash = ?,
                    message = ?,
                    timestamp = ?
                WHERE id = ?
                """,
                (self.TEMP_NAME, parent1_id, root_tree_hash, message.strip(), saved_at, keep_id),
            )
            row = conn.execute("SELECT * FROM versions WHERE id = ?", (keep_id,)).fetchone()
            return dict(row), stale_ids

        created = self._create_version(
            conn=conn,
            root_tree_hash=root_tree_hash,
            parent1_id=parent1_id,
            parent2_id=None,
            message=message.strip(),
            version_name=self.TEMP_NAME,
        )
        return created, stale_ids

    def _create_version(
        self,
        conn: sqlite3.Connection,
        root_tree_hash: str,
        parent1_id: Optional[int],
        parent2_id: Optional[int],
        message: str,
        version_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if version_name is None:
            version_name = f"v{self._next_manual_version_number(conn)}"
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            """
            INSERT INTO versions(name, parent1_id, parent2_id, root_tree_hash, message, timestamp)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                version_name,
                parent1_id,
                parent2_id,
                root_tree_hash,
                message.strip(),
                saved_at,
            ),
        )
        version_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return dict(row)

    def take_snapshot(
        self,
        message: str = "",
        auto: bool = False,
        only_if_changed: bool = False,
        name_prefix: Optional[str] = None,
        target_version_id: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        deleted_temp_ids: set[int] = set()
        with self._conn() as conn:
            ignore_patterns = self._load_flowignore_patterns()
            current_tree_hash = self._compute_tree_hash_from_disk(
                self.project_root, ignore_patterns
            )

            head = self._current_version_row(conn)
            if (
                only_if_changed
                and head is not None
                and head["root_tree_hash"] == current_tree_hash
            ):
                return None

            root_tree_hash = self._build_tree_from_disk(conn, self.project_root, ignore_patterns)
            parent_source = self._current_version_row(conn)
            parent1_id = int(parent_source["id"]) if parent_source else None
            is_temp_snapshot = bool(name_prefix) and name_prefix.startswith("temp")

            snapshot_message = message or (
                "Temporary save before checkout"
                if is_temp_snapshot
                else ("Auto-save" if auto else "Snapshot")
            )

            if is_temp_snapshot:
                created, deleted_temp_ids = self._upsert_single_temp_version(
                    conn=conn,
                    root_tree_hash=root_tree_hash,
                    parent1_id=parent1_id,
                    message=snapshot_message,
                )
                if deleted_temp_ids:
                    self._prune_orphan_tree_entries(conn)
            else:
                if auto:
                    version_name = self._next_auto_version_name(conn)
                elif name_prefix:
                    version_name = f"{name_prefix}{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                else:
                    version_name = None

                created = self._create_version(
                    conn=conn,
                    root_tree_hash=root_tree_hash,
                    parent1_id=parent1_id,
                    parent2_id=None,
                    message=snapshot_message,
                    version_name=version_name,
                )

        created_id = int(created["id"])
        self._set_current_version_id(created_id)
        if self._is_temp_version_name(str(created["name"])):
            self._set_last_temp_save_id(created_id)
            self._set_last_temp_protected(False)
        else:
            self._set_last_temp_protected(False)

        if deleted_temp_ids:
            self._clear_state_references(deleted_temp_ids)

        return created

    def is_dirty(self, baseline: str = "current") -> bool:
        with self._conn() as conn:
            if baseline == "latest_non_temp":
                baseline_row = self._latest_non_temp_version_row(conn)
            else:
                baseline_row = self._current_version_row(conn)

            if baseline_row is None:
                return False

            current_tree_hash = self._current_tree_hash()
            return current_tree_hash != baseline_row["root_tree_hash"]

    def checkout(self, version_id: int | str, create_safety: bool = True) -> dict[str, Any]:
        _ = create_safety  # Backward-compatible argument; checkout itself is now non-interactive.
        with self._conn() as conn:
            version = self._version_row(conn, version_id)
            files = self._collect_files_from_tree(conn, version["root_tree_hash"])
            for child in self.project_root.iterdir():
                if child.name == FLOWSTATE_DIR:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

            for rel_path, object_hash in files.items():
                target = self.project_root / Path(rel_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self._get_object_content(conn, object_hash))

            checked_out = dict(version)
            self._set_current_version_id(int(checked_out["id"]))
            return checked_out

    def _find_common_ancestor(
        self, conn: sqlite3.Connection, version_a_id: int, version_b_id: int
    ) -> Optional[int]:
        rows = conn.execute("SELECT id, parent1_id, parent2_id FROM versions").fetchall()
        parent_map = {
            int(row["id"]): (
                int(row["parent1_id"]) if row["parent1_id"] is not None else None,
                int(row["parent2_id"]) if row["parent2_id"] is not None else None,
            )
            for row in rows
        }

        def ancestor_depths(start_id: int) -> dict[int, int]:
            depths: dict[int, int] = {}
            queue: list[tuple[int, int]] = [(start_id, 0)]
            while queue:
                current_id, depth = queue.pop(0)
                if current_id in depths and depth >= depths[current_id]:
                    continue
                depths[current_id] = depth
                parents = parent_map.get(current_id, (None, None))
                for parent_id in parents:
                    if parent_id is not None:
                        queue.append((parent_id, depth + 1))
            return depths

        a_depths = ancestor_depths(version_a_id)
        b_depths = ancestor_depths(version_b_id)
        common = set(a_depths.keys()) & set(b_depths.keys())
        if not common:
            return None

        return min(common, key=lambda vid: (a_depths[vid] + b_depths[vid], -vid))

    @staticmethod
    def _is_text_blob(data: bytes) -> bool:
        if b"\x00" in data:
            return False
        try:
            data.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False

    def _resolve_conflict(
        self,
        conn: sqlite3.Connection,
        a_hash: Optional[str],
        b_hash: Optional[str],
        label_a: str,
        label_b: str,
        prefer_a: bool,
    ) -> Optional[str]:
        if a_hash is None or b_hash is None:
            return a_hash if prefer_a else b_hash

        a_bytes = self._get_object_content(conn, a_hash)
        b_bytes = self._get_object_content(conn, b_hash)
        if self._is_text_blob(a_bytes) and self._is_text_blob(b_bytes):
            merged = (
                f"<<<<<<< {label_a}\n"
                f"{a_bytes.decode('utf-8')}\n"
                "=======\n"
                f"{b_bytes.decode('utf-8')}\n"
                f">>>>>>> {label_b}\n"
            ).encode("utf-8")
            return self._store_object(conn, merged)

        return a_hash if prefer_a else b_hash

    def smart_merge(
        self, v_a_id: int | str, v_b_id: int | str, message: str = ""
    ) -> dict[str, Any]:
        with self._conn() as conn:
            version_a = self._version_row(conn, v_a_id)
            version_b = self._version_row(conn, v_b_id)
            a_id = int(version_a["id"])
            b_id = int(version_b["id"])
            if a_id == b_id:
                raise FlowStateError("Cannot merge a version into itself.")

            files_a = self._version_file_map(conn, a_id)
            files_b = self._version_file_map(conn, b_id)
            base_id = self._find_common_ancestor(conn, a_id, b_id)
            files_base = self._version_file_map(conn, base_id) if base_id else {}
            prefer_a = a_id > b_id

            merged_files: dict[str, str] = {}
            conflicts: list[str] = []
            all_paths = sorted(set(files_a.keys()) | set(files_b.keys()) | set(files_base.keys()))

            for path in all_paths:
                base_hash = files_base.get(path)
                a_hash = files_a.get(path)
                b_hash = files_b.get(path)

                if a_hash == b_hash:
                    chosen = a_hash
                elif base_hash == a_hash:
                    chosen = b_hash
                elif base_hash == b_hash:
                    chosen = a_hash
                else:
                    chosen = self._resolve_conflict(
                        conn=conn,
                        a_hash=a_hash,
                        b_hash=b_hash,
                        label_a=version_a["name"],
                        label_b=version_b["name"],
                        prefer_a=prefer_a,
                    )
                    conflicts.append(path)

                if chosen is not None:
                    merged_files[path] = chosen

            root_tree_hash = self._build_tree_from_file_map(conn, merged_files)
            merge_message = message or f"Merge {version_a['name']} + {version_b['name']}"
            merged_version = self._create_version(
                conn=conn,
                root_tree_hash=root_tree_hash,
                parent1_id=a_id,
                parent2_id=b_id,
                message=merge_message,
            )
            merged_version["conflicts"] = conflicts
            return merged_version

    def get_history(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, name, parent1_id, parent2_id, root_tree_hash, message, timestamp
                FROM versions
                ORDER BY id ASC
                """
            ).fetchall()
            history: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["is_auto"] = self._is_auto_version_name(item["name"])
                item["is_temp"] = self._is_temp_version_name(item["name"])
                if item["is_auto"]:
                    item["kind"] = "auto"
                elif item["is_temp"]:
                    item["kind"] = "temp"
                else:
                    item["kind"] = "manual"
                history.append(item)
            return history

    def prune_auto_saves(self, keep_last: int = 5) -> int:
        keep_last = max(0, keep_last)
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM versions
                WHERE name LIKE 'auto-%'
                ORDER BY id DESC
                """
            ).fetchall()
            if len(rows) <= keep_last:
                return 0

            candidate_ids = {int(row["id"]) for row in rows[keep_last:]}
            deleted_total = 0

            while candidate_ids:
                refs = conn.execute(
                    """
                    SELECT parent1_id, parent2_id
                    FROM versions
                    WHERE parent1_id IS NOT NULL OR parent2_id IS NOT NULL
                    """
                ).fetchall()
                referenced = set()
                for ref in refs:
                    p1 = ref["parent1_id"]
                    p2 = ref["parent2_id"]
                    if p1 is not None and int(p1) in candidate_ids:
                        referenced.add(int(p1))
                    if p2 is not None and int(p2) in candidate_ids:
                        referenced.add(int(p2))

                deletable = sorted(candidate_ids - referenced)
                if not deletable:
                    break

                conn.executemany(
                    "DELETE FROM versions WHERE id = ?",
                    [(version_id,) for version_id in deletable],
                )
                deleted_total += len(deletable)
                candidate_ids -= set(deletable)

            return deleted_total

    def _children_rows(self, conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id, name
            FROM versions
            WHERE parent1_id = ? OR parent2_id = ?
            ORDER BY id ASC
            """,
            (version_id, version_id),
        ).fetchall()

    def _reachable_tree_hashes(self, conn: sqlite3.Connection) -> set[str]:
        roots = [
            row["root_tree_hash"]
            for row in conn.execute("SELECT root_tree_hash FROM versions").fetchall()
        ]
        seen: set[str] = set()
        stack: list[str] = list(roots)
        while stack:
            tree_hash = stack.pop()
            if tree_hash in seen:
                continue
            seen.add(tree_hash)
            rows = conn.execute(
                "SELECT object_hash FROM tree_entries WHERE tree_hash = ? AND is_dir = 1",
                (tree_hash,),
            ).fetchall()
            stack.extend(row["object_hash"] for row in rows)
        return seen

    def _prune_orphan_tree_entries(self, conn: sqlite3.Connection) -> int:
        reachable = self._reachable_tree_hashes(conn)
        before = conn.execute("SELECT COUNT(*) AS c FROM tree_entries").fetchone()["c"]
        if reachable:
            placeholders = ",".join("?" for _ in reachable)
            conn.execute(
                f"DELETE FROM tree_entries WHERE tree_hash NOT IN ({placeholders})",
                tuple(reachable),
            )
        else:
            conn.execute("DELETE FROM tree_entries")
        after = conn.execute("SELECT COUNT(*) AS c FROM tree_entries").fetchone()["c"]
        return int(before) - int(after)

    def _clear_state_references(self, deleted_ids: set[int]) -> None:
        if not deleted_ids:
            return
        state = self._read_state()
        changed = False

        current_id = state.get("current_version_id")
        if isinstance(current_id, int) and current_id in deleted_ids:
            state.pop("current_version_id", None)
            changed = True

        last_temp_id = state.get("last_temp_save_id")
        if isinstance(last_temp_id, int) and last_temp_id in deleted_ids:
            state.pop("last_temp_save_id", None)
            state["protect_last_temp_save"] = False
            changed = True

        squash_root_id = state.get("squash_root_id")
        if isinstance(squash_root_id, int) and squash_root_id in deleted_ids:
            state.pop("squash_root_id", None)
            changed = True

        if changed:
            self._write_state(state)

    def _delete_version_ids(
        self, conn: sqlite3.Connection, version_ids: set[int]
    ) -> dict[str, Any]:
        if not version_ids:
            return {"deleted_ids": [], "pruned_tree_entries": 0}

        rows = conn.execute(
            "SELECT id, parent1_id, parent2_id FROM versions"
        ).fetchall()
        children_map: dict[int, set[int]] = {}
        for row in rows:
            child_id = int(row["id"])
            for parent_field in ("parent1_id", "parent2_id"):
                parent_id = row[parent_field]
                if parent_id is None:
                    continue
                children_map.setdefault(int(parent_id), set()).add(child_id)

        remaining = set(version_ids)
        deleted_order: list[int] = []
        while remaining:
            leaves = sorted(
                vid
                for vid in remaining
                if not (children_map.get(vid, set()) & remaining)
            )
            if not leaves:
                raise FlowStateError("Не удалось вычислить порядок удаления версий.")
            conn.executemany("DELETE FROM versions WHERE id = ?", [(vid,) for vid in leaves])
            remaining -= set(leaves)
            deleted_order.extend(leaves)

        pruned_tree_entries = self._prune_orphan_tree_entries(conn)
        return {"deleted_ids": deleted_order, "pruned_tree_entries": pruned_tree_entries}

    def _recursive_delete_set(self, conn: sqlite3.Connection, start_id: int) -> set[int]:
        rows = conn.execute(
            "SELECT id, parent1_id, parent2_id FROM versions"
        ).fetchall()
        parent_map: dict[int, tuple[Optional[int], Optional[int]]] = {}
        children_map: dict[int, set[int]] = {}
        for row in rows:
            vid = int(row["id"])
            p1 = int(row["parent1_id"]) if row["parent1_id"] is not None else None
            p2 = int(row["parent2_id"]) if row["parent2_id"] is not None else None
            parent_map[vid] = (p1, p2)
            for parent in (p1, p2):
                if parent is not None:
                    children_map.setdefault(parent, set()).add(vid)

        candidates: set[int] = set()
        stack: list[int] = [start_id]
        while stack:
            vid = stack.pop()
            if vid in candidates:
                continue
            candidates.add(vid)
            p1, p2 = parent_map.get(vid, (None, None))
            if p1 is not None:
                stack.append(p1)
            if p2 is not None:
                stack.append(p2)

        delete_set: set[int] = {start_id}
        changed = True
        while changed:
            changed = False
            for vid in sorted(candidates - delete_set):
                children = children_map.get(vid, set())
                if children and all(child in delete_set for child in children):
                    delete_set.add(vid)
                    changed = True
        return delete_set

    def _latest_temp_ids(self, conn: sqlite3.Connection) -> list[int]:
        rows = conn.execute(
            "SELECT id FROM versions WHERE name = ? OR name LIKE 'temp-%' ORDER BY id DESC",
            (self.TEMP_NAME,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def prune_temp_saves(
        self, keep_last: int = 3, keep_protected: bool = True
    ) -> dict[str, Any]:
        keep_last = max(0, keep_last)
        protected_id = self.get_last_temp_save_id() if keep_protected else None
        protected_required = keep_protected and self._is_last_temp_protected()

        with self._conn() as conn:
            temp_ids = self._latest_temp_ids(conn)
            if len(temp_ids) <= keep_last:
                return {
                    "deleted_ids": [],
                    "pruned_tree_entries": 0,
                    "gc_removed_db_objects": 0,
                    "gc_removed_disk_objects": 0,
                }

            candidates = set(temp_ids[keep_last:])
            if protected_required and protected_id is not None and protected_id in candidates:
                candidates.remove(protected_id)

            if not candidates:
                return {
                    "deleted_ids": [],
                    "pruned_tree_entries": 0,
                    "gc_removed_db_objects": 0,
                    "gc_removed_disk_objects": 0,
                }

            # Temp pruning is force-style: detach children then remove stale temp snapshots.
            for version_id in sorted(candidates):
                children = self._children_rows(conn, version_id)
                if children:
                    conn.execute(
                        """
                        UPDATE versions
                        SET parent1_id = CASE WHEN parent1_id = ? THEN NULL ELSE parent1_id END,
                            parent2_id = CASE WHEN parent2_id = ? THEN NULL ELSE parent2_id END
                        WHERE parent1_id = ? OR parent2_id = ?
                        """,
                        (version_id, version_id, version_id, version_id),
                    )

            delete_result = self._delete_version_ids(conn, candidates)

        deleted_ids = set(delete_result["deleted_ids"])
        self._clear_state_references(deleted_ids)
        gc_result = self.gc()
        return {
            "deleted_ids": sorted(deleted_ids),
            "pruned_tree_entries": delete_result["pruned_tree_entries"],
            "gc_removed_db_objects": gc_result["deleted_db_objects"],
            "gc_removed_disk_objects": gc_result["deleted_disk_objects"],
        }

    def delete_version(
        self, version_ref: int | str, force: bool = False, recursive: bool = False
    ) -> dict[str, Any]:
        with self._conn() as conn:
            version = self._version_row(conn, version_ref)
            version_id = int(version["id"])

            if recursive:
                delete_ids = self._recursive_delete_set(conn, version_id)
            else:
                children = self._children_rows(conn, version_id)
                if children and not force:
                    child_name = children[0]["name"]
                    raise FlowStateError(
                        f"Версия является предком для {child_name}. Сначала удалите потомков"
                    )
                if children and force:
                    conn.execute(
                        """
                        UPDATE versions
                        SET parent1_id = CASE WHEN parent1_id = ? THEN NULL ELSE parent1_id END,
                            parent2_id = CASE WHEN parent2_id = ? THEN NULL ELSE parent2_id END
                        WHERE parent1_id = ? OR parent2_id = ?
                        """,
                        (version_id, version_id, version_id, version_id),
                    )
                delete_ids = {version_id}

            delete_result = self._delete_version_ids(conn, delete_ids)

        deleted_ids = set(delete_result["deleted_ids"])
        self._clear_state_references(deleted_ids)
        gc_result = self.gc()
        return {
            "deleted_ids": sorted(deleted_ids),
            "pruned_tree_entries": delete_result["pruned_tree_entries"],
            "gc_removed_db_objects": gc_result["deleted_db_objects"],
            "gc_removed_disk_objects": gc_result["deleted_disk_objects"],
        }

    def clean_versions(self, all_versions: bool = False) -> dict[str, Any]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, name, timestamp FROM versions ORDER BY id ASC"
            ).fetchall()
            if not rows:
                return {
                    "deleted_ids": [],
                    "skipped_protected": None,
                    "pruned_tree_entries": 0,
                    "gc_removed_db_objects": 0,
                    "gc_removed_disk_objects": 0,
                }

            cutoff = datetime.now() - timedelta(hours=24)
            candidate_ids: set[int] = set()

            if all_versions:
                squash_root_id = self._get_squash_root_id()
                if squash_root_id is not None:
                    descendants: set[int] = set()
                    queue: list[int] = [squash_root_id]
                    while queue:
                        current = queue.pop(0)
                        if current in descendants:
                            continue
                        descendants.add(current)
                        child_rows = self._children_rows(conn, current)
                        queue.extend(int(row["id"]) for row in child_rows)

                    for row in rows:
                        vid = int(row["id"])
                        if vid not in descendants:
                            candidate_ids.add(vid)
                else:
                    for row in rows:
                        name = str(row["name"])
                        if name.startswith(self.AUTO_PREFIX) or self._is_temp_version_name(name):
                            candidate_ids.add(int(row["id"]))
            else:
                for row in rows:
                    name = str(row["name"])
                    if not (
                        name.startswith(self.AUTO_PREFIX)
                        or self._is_temp_version_name(name)
                    ):
                        continue
                    try:
                        ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if ts <= cutoff:
                        candidate_ids.add(int(row["id"]))

            skipped_protected: Optional[int] = None
            last_temp_id = self.get_last_temp_save_id()
            if (
                last_temp_id is not None
                and last_temp_id in candidate_ids
                and self._is_last_temp_protected()
            ):
                candidate_ids.remove(last_temp_id)
                skipped_protected = last_temp_id

            if not candidate_ids:
                return {
                    "deleted_ids": [],
                    "skipped_protected": skipped_protected,
                    "pruned_tree_entries": 0,
                    "gc_removed_db_objects": 0,
                    "gc_removed_disk_objects": 0,
                }

            # Clean operates in force mode: detach children and remove selected versions.
            for version_id in sorted(candidate_ids):
                children = self._children_rows(conn, version_id)
                if children:
                    conn.execute(
                        """
                        UPDATE versions
                        SET parent1_id = CASE WHEN parent1_id = ? THEN NULL ELSE parent1_id END,
                            parent2_id = CASE WHEN parent2_id = ? THEN NULL ELSE parent2_id END
                        WHERE parent1_id = ? OR parent2_id = ?
                        """,
                        (version_id, version_id, version_id, version_id),
                    )

            delete_result = self._delete_version_ids(conn, candidate_ids)

        deleted_ids = set(delete_result["deleted_ids"])
        self._clear_state_references(deleted_ids)
        gc_result = self.gc()
        return {
            "deleted_ids": sorted(deleted_ids),
            "skipped_protected": skipped_protected,
            "pruned_tree_entries": delete_result["pruned_tree_entries"],
            "gc_removed_db_objects": gc_result["deleted_db_objects"],
            "gc_removed_disk_objects": gc_result["deleted_disk_objects"],
        }

    def gc(self) -> dict[str, int]:
        with self._conn() as conn:
            reachable_trees = self._reachable_tree_hashes(conn)
            referenced_objects: set[str] = set()

            if reachable_trees:
                rows = conn.execute(
                    "SELECT tree_hash, object_hash, is_dir FROM tree_entries"
                ).fetchall()
                for row in rows:
                    if row["tree_hash"] in reachable_trees and not row["is_dir"]:
                        referenced_objects.add(row["object_hash"])

            db_rows = conn.execute("SELECT hash FROM objects").fetchall()
            db_hashes = {row["hash"] for row in db_rows}
            stale_db = sorted(db_hashes - referenced_objects)
            if stale_db:
                conn.executemany("DELETE FROM objects WHERE hash = ?", [(h,) for h in stale_db])

        deleted_disk = 0
        for path in self.objects_dir.glob("*/*"):
            if not path.is_file():
                continue
            object_hash = f"{path.parent.name}{path.name}"
            if object_hash not in referenced_objects:
                path.unlink(missing_ok=True)
                deleted_disk += 1

        for dir_path in sorted(self.objects_dir.glob("*"), reverse=True):
            if dir_path.is_dir() and not any(dir_path.iterdir()):
                dir_path.rmdir()

        return {
            "deleted_db_objects": len(stale_db),
            "deleted_disk_objects": deleted_disk,
        }

    def squash_version(self, version_ref: int | str) -> dict[str, Any]:
        with self._conn() as conn:
            version = self._version_row(conn, version_ref)
            version_id = int(version["id"])
            conn.execute(
                "UPDATE versions SET parent1_id = NULL, parent2_id = NULL WHERE id = ?",
                (version_id,),
            )
            updated = conn.execute(
                "SELECT * FROM versions WHERE id = ?",
                (version_id,),
            ).fetchone()

        self._set_squash_root_id(version_id)
        return dict(updated)

    def describe_diff(
        self, from_version_id: int | str, to_version_id: int | str
    ) -> dict[str, list[str]]:
        with self._conn() as conn:
            from_row = self._version_row(conn, from_version_id)
            to_row = self._version_row(conn, to_version_id)
            from_files = self._collect_files_from_tree(conn, from_row["root_tree_hash"])
            to_files = self._collect_files_from_tree(conn, to_row["root_tree_hash"])

            from_paths = set(from_files.keys())
            to_paths = set(to_files.keys())

            added = sorted(to_paths - from_paths)
            removed = sorted(from_paths - to_paths)
            modified = sorted(
                path for path in (from_paths & to_paths) if from_files[path] != to_files[path]
            )

            return {"added": added, "removed": removed, "modified": modified}
