from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from database import (
    database_path,
    flowstate_path,
    initialize_database,
    is_initialized,
    verify_schema,
)
from engine import FlowStateEngine, FlowStateError
from watcher import run_watch_loop, start_watch_background


def _is_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json_output", False))


def _emit(
    args: argparse.Namespace,
    payload: dict[str, Any],
    text_renderer: Optional[Callable[[], None]] = None,
) -> None:
    """Emit JSON when --json is requested; otherwise call the text renderer."""

    if _is_json(args):
        payload.setdefault("ok", True)
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    if text_renderer is not None:
        text_renderer()


def _serialize_version(version: dict[str, Any]) -> dict[str, Any]:
    """Normalize an engine version dict for JSON output."""

    out = dict(version)
    for key in ("id", "parent1_id", "parent2_id"):
        value = out.get(key)
        if value is not None:
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
    if "name" in out and out["name"] is not None:
        out["name"] = str(out["name"])
    if "message" in out and out["message"] is None:
        out["message"] = ""
    if isinstance(out.get("conflicts"), list):
        out["conflicts"] = [str(p) for p in out["conflicts"]]
    return out


def _engine_or_exit(project_root: Path) -> FlowStateEngine:
    ok, error = verify_schema(project_root)
    if not ok:
        raise FlowStateError(
            f"{error or 'FlowState is not initialized.'} "
            f"Run `flow init {project_root}` first."
        )
    return FlowStateEngine(project_root)


def cmd_init(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    already = is_initialized(project_root)
    db_path = initialize_database(project_root)

    def text() -> None:
        if already:
            print(f"FlowState already initialized at {project_root}")
        else:
            print(f"Initialized FlowState at {project_root}")
        print(f"Database: {db_path}")

    _emit(
        args,
        {
            "project_root": str(project_root),
            "db_path": str(db_path),
            "created": not already,
        },
        text,
    )


def cmd_is_init(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    ok, error = verify_schema(project_root)

    def text() -> None:
        if ok:
            print("FlowState repository: OK")
            print(f"Database: {database_path(project_root)}")
        else:
            print(error or "FlowState is not initialized.")

    _emit(
        args,
        {
            "initialized": ok,
            "project_root": str(project_root),
            "db_path": str(database_path(project_root)),
            "error": error,
        },
        text,
    )


def cmd_save(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.take_snapshot(args.message or "")

    def text() -> None:
        if version is None:
            print("No changes detected. Snapshot skipped.")
            return
        print(f"Created {version['name']} (id={version['id']})")
        print(f"Message: {version['message']}")

    _emit(
        args,
        {
            "created": version is not None,
            "version": _serialize_version(version) if version else None,
        },
        text,
    )


def cmd_merge(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.smart_merge(args.version_a, args.version_b, args.message or "")

    def text() -> None:
        print(f"Created merge version {version['name']} (id={version['id']})")
        if version["conflicts"]:
            print("Conflicts handled for:")
            for path in version["conflicts"]:
                print(f" - {path}")
        else:
            print("Merged without conflicts.")

    _emit(args, {"version": _serialize_version(version)}, text)


def cmd_checkout(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)

    target_row_dict: dict[str, Any]
    target_is_temp: bool
    with engine._conn() as conn:
        target_row = engine._version_row(conn, args.version)
        target_row_dict = dict(target_row)
        target_is_temp = engine._is_temp_version_name(str(target_row["name"]))

    create_temp = False
    if engine.is_dirty() and not target_is_temp:
        if args.save_temp:
            create_temp = True
        elif args.no_temp:
            create_temp = False
        elif _is_json(args):
            create_temp = False
        elif sys.stdin.isatty():
            answer = input(
                "Unsaved changes detected. Create temporary snapshot 'temp' before checkout? [y/N]: "
            ).strip().lower()
            create_temp = answer in {"y", "yes"}

    temp_version: Optional[dict[str, Any]] = None
    if create_temp:
        temp = engine.take_snapshot(
            message=args.temp_message or "Temporary save before checkout",
            name_prefix="temp",
        )
        if temp is not None:
            temp_version = temp

    version = engine.checkout(args.version, create_safety=False)

    def text() -> None:
        if temp_version is not None:
            print(
                f"Temporary snapshot saved as {temp_version['name']} "
                f"(id={temp_version['id']})"
            )
        print(f"Checked out {version['name']} (id={version['id']})")

    _emit(
        args,
        {
            "version": _serialize_version(version),
            "target": _serialize_version(target_row_dict),
            "temp_created": _serialize_version(temp_version) if temp_version else None,
        },
        text,
    )


def cmd_history(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    history = engine.get_history()

    def text() -> None:
        if not history:
            print("No versions found.")
            return

        use_color = sys.stdout.isatty()
        color_manual = "\033[96m" if use_color else ""
        color_auto = "\033[93m" if use_color else ""
        color_temp = "\033[95m" if use_color else ""
        color_reset = "\033[0m" if use_color else ""

        manual_versions = [v for v in history if v.get("kind") == "manual"]
        temp_versions = [v for v in history if v.get("kind") == "temp"]
        auto_versions = [v for v in history if v.get("is_auto")]

        def _format_saved(ts: str) -> str:
            try:
                return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return ts

        def _print_block(title: str, rows: list[dict], color: str, label: str) -> None:
            if not rows:
                return
            print(title)
            for version in rows:
                parents = [
                    p for p in [version["parent1_id"], version["parent2_id"]] if p is not None
                ]
                parent_text = ",".join(str(p) for p in parents) if parents else "-"
                saved_at = _format_saved(version["timestamp"])
                label_text = f"{color}[{label}]{color_reset}"
                print(
                    f" {version['id']:>3} {label_text} {version['name']} "
                    f"(Saved: {saved_at}) parents=[{parent_text}] {version['message'] or ''}"
                )

        _print_block("Versions:", manual_versions, color_manual, "VERSION")
        _print_block("Temp Saves:", temp_versions, color_temp, "TEMP")
        _print_block("Auto-Saves:", auto_versions, color_auto, "AUTO")

    _emit(
        args,
        {"history": [_serialize_version(item) for item in history]},
        text,
    )


def cmd_status(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)

    current_id = engine._get_current_version_id()
    current_version: Optional[dict[str, Any]] = None
    latest_version: Optional[dict[str, Any]] = None
    last_temp_id = engine.get_last_temp_save_id()

    with engine._conn() as conn:
        if current_id is not None:
            row = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (current_id,)
            ).fetchone()
            if row is not None:
                current_version = dict(row)
        latest_row = engine._latest_version_row(conn)
        if latest_row is not None:
            latest_version = dict(latest_row)

    dirty = engine.is_dirty()

    def text() -> None:
        if current_version is not None:
            print(
                f"Current: {current_version['name']} (id={current_version['id']})"
            )
        elif latest_version is not None:
            print(
                f"Current: latest = {latest_version['name']} (id={latest_version['id']})"
            )
        else:
            print("Current: no versions yet")
        print(f"Dirty: {'yes' if dirty else 'no'}")
        if last_temp_id is not None:
            print(f"Last temp id: {last_temp_id}")

    _emit(
        args,
        {
            "dirty": dirty,
            "current_version": _serialize_version(current_version) if current_version else None,
            "latest_version": _serialize_version(latest_version) if latest_version else None,
            "last_temp_save_id": last_temp_id,
        },
        text,
    )


def cmd_show(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    rel_path = args.file.replace("\\", "/")

    content_text: Optional[str] = None
    missing = False

    with engine._conn() as conn:
        version = engine._version_row(conn, args.version)
        files = engine._collect_files_from_tree(conn, version["root_tree_hash"])
        object_hash = files.get(rel_path)
        if object_hash is None:
            missing = True
        else:
            data = engine._get_object_content(conn, object_hash)
            content_text = data.decode("utf-8", errors="replace")

    def text() -> None:
        if missing:
            print(f"File not found in {args.version}: {rel_path}")
            return
        if content_text is not None:
            sys.stdout.write(content_text)
            if not content_text.endswith("\n"):
                sys.stdout.write("\n")

    _emit(
        args,
        {
            "missing": missing,
            "version_ref": args.version,
            "file": rel_path,
            "content": content_text,
        },
        text,
    )


def cmd_diff(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    diff = engine.describe_diff(args.from_version, args.to_version)

    def text() -> None:
        for section in ("added", "modified", "removed"):
            print(f"{section.capitalize()}:")
            rows = diff[section]
            if not rows:
                print("  -")
                continue
            for item in rows:
                print(f"  {item}")

    _emit(
        args,
        {
            "from_version": args.from_version,
            "to_version": args.to_version,
            "diff": diff,
        },
        text,
    )


def cmd_export(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    _engine_or_exit(project_root)
    flow_dir = flowstate_path(project_root)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if output.suffix.lower() == ".zip":
        base_name = output.with_suffix("")
    else:
        base_name = output
        output = output.with_suffix(".zip")

    if output.exists():
        output.unlink()
    archive_path = Path(
        shutil.make_archive(
            str(base_name),
            "zip",
            root_dir=str(project_root),
            base_dir=flow_dir.name,
        )
    )

    def text() -> None:
        print(f"Exported FlowState storage to {archive_path}")

    _emit(
        args,
        {
            "archive": str(archive_path),
            "project_root": str(project_root),
        },
        text,
    )


def cmd_import(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    archive = Path(args.archive).resolve()
    if not archive.exists():
        raise FlowStateError(f"Archive not found: {archive}")

    target = flowstate_path(project_root)
    if target.exists() and not args.force:
        raise FlowStateError(
            f"FlowState storage already exists at {target}. Use --force to replace it."
        )

    project_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="flowstate-import-") as tmp:
        tmp_path = Path(tmp)
        shutil.unpack_archive(str(archive), str(tmp_path))
        imported = tmp_path / target.name
        if not imported.exists() or not imported.is_dir():
            raise FlowStateError("Archive does not contain a .flowstate directory.")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(imported, target)

    ok, error = verify_schema(project_root)
    if not ok:
        raise FlowStateError(error or "Imported FlowState storage failed schema verification.")

    def text() -> None:
        print(f"Imported FlowState storage from {archive} into {project_root}")

    _emit(
        args,
        {
            "archive": str(archive),
            "project_root": str(project_root),
            "db_path": str(database_path(project_root)),
        },
        text,
    )


def cmd_back(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.back()

    def text() -> None:
        print(
            f"Returned to temporary snapshot {version['name']} (id={version['id']})"
        )

    _emit(args, {"version": _serialize_version(version)}, text)


def cmd_watch(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    _engine_or_exit(project_root)

    interval_minutes = max(1, int(args.interval_minutes))
    keep_auto = max(0, int(args.keep_auto))

    if args.foreground or args.once:
        # Foreground/once is a long-running text loop; JSON wrapping is not meaningful here.
        run_watch_loop(
            project_root=project_root,
            interval_seconds=interval_minutes * 60,
            keep_last_auto=keep_auto,
            run_once=bool(args.once),
        )
        return

    pid = start_watch_background(
        project_root=project_root,
        cli_path=Path(__file__),
        interval_minutes=interval_minutes,
        keep_last_auto=keep_auto,
    )

    def text() -> None:
        print(
            f"FlowState watcher started in background (pid={pid}, "
            f"interval={interval_minutes}m, keep_auto={keep_auto})."
        )

    _emit(
        args,
        {
            "pid": pid,
            "mode": "background",
            "interval_minutes": interval_minutes,
            "keep_auto": keep_auto,
        },
        text,
    )


def cmd_clean(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.clean_versions(all_versions=bool(args.all))
    deleted_ids = [int(v) for v in result["deleted_ids"]]

    def text() -> None:
        if deleted_ids:
            print(f"Deleted versions: {', '.join(str(v) for v in deleted_ids)}")
        else:
            print("No versions matched clean policy.")
        if result.get("skipped_protected") is not None:
            print(
                f"Skipped protected temp version id={result['skipped_protected']} "
                "(no new snapshot after checkout)."
            )
        print(
            f"GC removed DB objects: {result['gc_removed_db_objects']}, "
            f"disk objects: {result['gc_removed_disk_objects']}"
        )

    _emit(
        args,
        {
            "deleted_ids": deleted_ids,
            "skipped_protected": result.get("skipped_protected"),
            "pruned_tree_entries": result.get("pruned_tree_entries", 0),
            "gc_removed_db_objects": result["gc_removed_db_objects"],
            "gc_removed_disk_objects": result["gc_removed_disk_objects"],
        },
        text,
    )


def cmd_delete(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.delete_version(
        args.version,
        force=bool(args.force),
        recursive=bool(args.recursive),
    )
    deleted_ids = [int(v) for v in result["deleted_ids"]]

    def text() -> None:
        print(f"Deleted versions: {', '.join(str(v) for v in deleted_ids)}")
        print(
            f"Pruned tree entries: {result['pruned_tree_entries']}, "
            f"GC removed DB objects: {result['gc_removed_db_objects']}, "
            f"disk objects: {result['gc_removed_disk_objects']}"
        )

    _emit(
        args,
        {
            "deleted_ids": deleted_ids,
            "pruned_tree_entries": result["pruned_tree_entries"],
            "gc_removed_db_objects": result["gc_removed_db_objects"],
            "gc_removed_disk_objects": result["gc_removed_disk_objects"],
        },
        text,
    )


def cmd_gc(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.gc()

    def text() -> None:
        print(
            f"GC completed. Removed DB objects: {result['deleted_db_objects']}, "
            f"disk objects: {result['deleted_disk_objects']}"
        )

    _emit(
        args,
        {
            "deleted_db_objects": result["deleted_db_objects"],
            "deleted_disk_objects": result["deleted_disk_objects"],
        },
        text,
    )


def cmd_squash(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.squash_version(args.version)

    def text() -> None:
        print(
            f"Version {version['name']} (id={version['id']}) is now a root "
            "(parent1_id=NULL, parent2_id=NULL)."
        )

    _emit(args, {"version": _serialize_version(version)}, text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow", description="FlowState VCS CLI")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON output",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Initialize FlowState in a folder", parents=[common]
    )
    init_parser.add_argument("path", nargs="?", default=".", help="Project root path")
    init_parser.set_defaults(func=cmd_init)

    is_init_parser = subparsers.add_parser(
        "is-init",
        help="Check whether FlowState is initialized in the given folder",
        parents=[common],
    )
    is_init_parser.add_argument("--path", default=".", help="Project root path")
    is_init_parser.set_defaults(func=cmd_is_init)

    save_parser = subparsers.add_parser(
        "save", help="Create a new snapshot version", parents=[common]
    )
    save_parser.add_argument("-m", "--message", default="", help="Version message")
    save_parser.add_argument("--path", default=".", help="Project root path")
    save_parser.set_defaults(func=cmd_save)

    merge_parser = subparsers.add_parser(
        "merge", help="Merge two versions into a new one", parents=[common]
    )
    merge_parser.add_argument("version_a", help="First version id or name (e.g., 1 or v1)")
    merge_parser.add_argument("version_b", help="Second version id or name (e.g., 2 or v2)")
    merge_parser.add_argument("-m", "--message", default="", help="Merge message")
    merge_parser.add_argument("--path", default=".", help="Project root path")
    merge_parser.set_defaults(func=cmd_merge)

    checkout_parser = subparsers.add_parser(
        "checkout", help="Checkout a version to disk", parents=[common]
    )
    checkout_parser.add_argument("version", help="Version id or name")
    checkout_parser.add_argument("--path", default=".", help="Project root path")
    temp_group = checkout_parser.add_mutually_exclusive_group()
    temp_group.add_argument(
        "--save-temp",
        action="store_true",
        help="Create/update temporary snapshot `temp` before checkout if dirty",
    )
    temp_group.add_argument(
        "--no-temp",
        action="store_true",
        help="Do not create temporary snapshot before checkout",
    )
    checkout_parser.add_argument(
        "--temp-message",
        default="Temporary save before checkout",
        help="Message for the temporary snapshot",
    )
    checkout_parser.set_defaults(func=cmd_checkout)

    back_parser = subparsers.add_parser(
        "back",
        help="Checkout the current temporary snapshot `temp`",
        parents=[common],
    )
    back_parser.add_argument("--path", default=".", help="Project root path")
    back_parser.set_defaults(func=cmd_back)

    history_parser = subparsers.add_parser(
        "history", help="Show version graph metadata", parents=[common]
    )
    history_parser.add_argument("--path", default=".", help="Project root path")
    history_parser.set_defaults(func=cmd_history)

    status_parser = subparsers.add_parser(
        "status",
        help="Show current version, latest version and dirty flag",
        parents=[common],
    )
    status_parser.add_argument("--path", default=".", help="Project root path")
    status_parser.set_defaults(func=cmd_status)

    show_parser = subparsers.add_parser(
        "show",
        help="Print the contents of a file from a specific version",
        parents=[common],
    )
    show_parser.add_argument("version", help="Version id or name")
    show_parser.add_argument("file", help="Workspace-relative file path")
    show_parser.add_argument("--path", default=".", help="Project root path")
    show_parser.set_defaults(func=cmd_show)

    diff_parser = subparsers.add_parser(
        "diff",
        help="Compare two versions and show added/modified/removed files",
        parents=[common],
    )
    diff_parser.add_argument("from_version", help="Base version id or name")
    diff_parser.add_argument("to_version", help="Target version id or name")
    diff_parser.add_argument("--path", default=".", help="Project root path")
    diff_parser.set_defaults(func=cmd_diff)

    export_parser = subparsers.add_parser(
        "export",
        help="Export .flowstate storage to a zip archive",
        parents=[common],
    )
    export_parser.add_argument("output", help="Output archive path (.zip)")
    export_parser.add_argument("--path", default=".", help="Project root path")
    export_parser.set_defaults(func=cmd_export)

    import_parser = subparsers.add_parser(
        "import",
        help="Import .flowstate storage from a zip archive",
        parents=[common],
    )
    import_parser.add_argument("archive", help="Input archive path")
    import_parser.add_argument("--path", default=".", help="Project root path")
    import_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing .flowstate directory",
    )
    import_parser.set_defaults(func=cmd_import)

    watch_parser = subparsers.add_parser(
        "watch",
        help="Start auto-save watcher (15-minute interval by default)",
        parents=[common],
    )
    watch_parser.add_argument("--path", default=".", help="Project root path")
    watch_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="Auto-save interval in minutes",
    )
    watch_parser.add_argument(
        "--keep-auto",
        type=int,
        default=5,
        help="Number of recent auto-saves to keep",
    )
    watch_parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run watcher in the current process",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one watch cycle and exit (useful for testing)",
    )
    watch_parser.set_defaults(func=cmd_watch)

    clean_parser = subparsers.add_parser(
        "clean",
        help="Delete stale temp/auto versions (24h default), with --all for aggressive cleanup",
        parents=[common],
    )
    clean_parser.add_argument("--path", default=".", help="Project root path")
    clean_parser.add_argument(
        "--all",
        action="store_true",
        help="Aggressive cleanup mode",
    )
    clean_parser.set_defaults(func=cmd_clean)

    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete a version by id or name",
        parents=[common],
    )
    delete_parser.add_argument("version", help="Version id or name")
    delete_parser.add_argument("--path", default=".", help="Project root path")
    delete_parser.add_argument(
        "--force",
        action="store_true",
        help="Detach children from target version before deleting",
    )
    delete_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Delete target and removable ancestors",
    )
    delete_parser.set_defaults(func=cmd_delete)

    gc_parser = subparsers.add_parser(
        "gc", help="Run garbage collection", parents=[common]
    )
    gc_parser.add_argument("--path", default=".", help="Project root path")
    gc_parser.set_defaults(func=cmd_gc)

    squash_parser = subparsers.add_parser(
        "squash",
        help="Turn selected version into a new root by clearing its parents",
        parents=[common],
    )
    squash_parser.add_argument("version", help="Version id or name")
    squash_parser.add_argument("--path", default=".", help="Project root path")
    squash_parser.set_defaults(func=cmd_squash)

    return parser


def _print_safe(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write(
            message.encode(encoding, errors="backslashreplace") + b"\n"
        )


def _ensure_utf8_stdio() -> None:
    """Make stdout/stderr robust to non-ASCII text on legacy Windows consoles."""

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main() -> int:
    _ensure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except FlowStateError as exc:
        if _is_json(args):
            sys.stdout.write(
                json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
            )
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            _print_safe(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
