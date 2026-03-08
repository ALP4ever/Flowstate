from __future__ import annotations

import argparse
from datetime import datetime
import sys
from pathlib import Path

from database import initialize_database, is_initialized
from engine import FlowStateEngine, FlowStateError
from watcher import run_watch_loop, start_watch_background


def _engine_or_exit(project_root: Path) -> FlowStateEngine:
    if not is_initialized(project_root):
        raise FlowStateError(
            f"FlowState is not initialized at {project_root}. Run `flow init` first."
        )
    return FlowStateEngine(project_root)


def cmd_init(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    db_path = initialize_database(project_root)
    print(f"Initialized FlowState at {project_root}")
    print(f"Database: {db_path}")


def cmd_save(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.take_snapshot(args.message or "")
    if version is None:
        print("No changes detected. Snapshot skipped.")
        return
    print(f"Created {version['name']} (id={version['id']})")
    print(f"Message: {version['message']}")


def cmd_merge(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.smart_merge(args.version_a, args.version_b, args.message or "")
    print(f"Created merge version {version['name']} (id={version['id']})")
    if version["conflicts"]:
        print("Conflicts handled for:")
        for path in version["conflicts"]:
            print(f" - {path}")
    else:
        print("Merged without conflicts.")


def cmd_checkout(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)

    with engine._conn() as conn:
        target_row = engine._version_row(conn, args.version)
        target_is_temp = engine._is_temp_version_name(str(target_row["name"]))

    create_temp = False
    if engine.is_dirty() and not target_is_temp:
        if args.save_temp:
            create_temp = True
        elif args.no_temp:
            create_temp = False
        elif sys.stdin.isatty():
            answer = input(
                "Unsaved changes detected. Create temporary snapshot 'temp' before checkout? [y/N]: "
            ).strip().lower()
            create_temp = answer in {"y", "yes"}

    if create_temp:
        temp = engine.take_snapshot(
            message=args.temp_message or "Temporary save before checkout",
            name_prefix="temp",
        )
        if temp is not None:
            print(f"Temporary snapshot saved as {temp['name']} (id={temp['id']})")

    version = engine.checkout(args.version, create_safety=False)
    print(f"Checked out {version['name']} (id={version['id']})")


def cmd_history(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    history = engine.get_history()
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
                f" {version['id']:>3} {label_text} {version['name']} (Saved: {saved_at}) "
                f"parents=[{parent_text}] {version['message'] or ''}"
            )

    _print_block("Versions:", manual_versions, color_manual, "VERSION")
    _print_block("Temp Saves:", temp_versions, color_temp, "TEMP")
    _print_block("Auto-Saves:", auto_versions, color_auto, "AUTO")


def cmd_back(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.back()
    print(f"Returned to temporary snapshot {version['name']} (id={version['id']})")


def cmd_watch(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    _engine_or_exit(project_root)

    interval_minutes = max(1, int(args.interval_minutes))
    keep_auto = max(0, int(args.keep_auto))

    if args.foreground or args.once:
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
    print(
        f"FlowState watcher started in background (pid={pid}, "
        f"interval={interval_minutes}m, keep_auto={keep_auto})."
    )


def cmd_clean(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.clean_versions(all_versions=bool(args.all))
    deleted_ids = result["deleted_ids"]
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


def cmd_delete(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.delete_version(
        args.version,
        force=bool(args.force),
        recursive=bool(args.recursive),
    )
    print(f"Deleted versions: {', '.join(str(v) for v in result['deleted_ids'])}")
    print(
        f"Pruned tree entries: {result['pruned_tree_entries']}, "
        f"GC removed DB objects: {result['gc_removed_db_objects']}, "
        f"disk objects: {result['gc_removed_disk_objects']}"
    )


def cmd_gc(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    result = engine.gc()
    print(
        f"GC completed. Removed DB objects: {result['deleted_db_objects']}, "
        f"disk objects: {result['deleted_disk_objects']}"
    )


def cmd_squash(args: argparse.Namespace) -> None:
    project_root = Path(args.path).resolve()
    engine = _engine_or_exit(project_root)
    version = engine.squash_version(args.version)
    print(
        f"Version {version['name']} (id={version['id']}) is now a root "
        "(parent1_id=NULL, parent2_id=NULL)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow", description="FlowState VCS CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize FlowState in a folder")
    init_parser.add_argument("path", nargs="?", default=".", help="Project root path")
    init_parser.set_defaults(func=cmd_init)

    save_parser = subparsers.add_parser("save", help="Create a new snapshot version")
    save_parser.add_argument("-m", "--message", default="", help="Version message")
    save_parser.add_argument("--path", default=".", help="Project root path")
    save_parser.set_defaults(func=cmd_save)

    merge_parser = subparsers.add_parser("merge", help="Merge two versions into a new one")
    merge_parser.add_argument("version_a", help="First version id or name (e.g., 1 or v1)")
    merge_parser.add_argument("version_b", help="Second version id or name (e.g., 2 or v2)")
    merge_parser.add_argument("-m", "--message", default="", help="Merge message")
    merge_parser.add_argument("--path", default=".", help="Project root path")
    merge_parser.set_defaults(func=cmd_merge)

    checkout_parser = subparsers.add_parser("checkout", help="Checkout a version to disk")
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
    )
    back_parser.add_argument("--path", default=".", help="Project root path")
    back_parser.set_defaults(func=cmd_back)

    history_parser = subparsers.add_parser("history", help="Show version graph metadata")
    history_parser.add_argument("--path", default=".", help="Project root path")
    history_parser.set_defaults(func=cmd_history)

    watch_parser = subparsers.add_parser(
        "watch", help="Start auto-save watcher (15-minute interval by default)"
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

    gc_parser = subparsers.add_parser("gc", help="Run garbage collection")
    gc_parser.add_argument("--path", default=".", help="Project root path")
    gc_parser.set_defaults(func=cmd_gc)

    squash_parser = subparsers.add_parser(
        "squash",
        help="Turn selected version into a new root by clearing its parents",
    )
    squash_parser.add_argument("version", help="Version id or name")
    squash_parser.add_argument("--path", default=".", help="Project root path")
    squash_parser.set_defaults(func=cmd_squash)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except FlowStateError as exc:
        message = f"Error: {exc}"
        try:
            print(message)
        except UnicodeEncodeError:
            encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.buffer.write(message.encode(encoding, errors="backslashreplace") + b"\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
