from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from engine import FlowStateEngine


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_watch_loop(
    project_root: Path | str,
    interval_seconds: int = 15 * 60,
    keep_last_auto: int = 5,
    run_once: bool = False,
) -> None:
    project_root = Path(project_root).resolve()
    engine = FlowStateEngine(project_root)

    print(
        f"[{_now_text()}] FlowState watcher started for {project_root} "
        f"(interval={interval_seconds}s, keep_last_auto={keep_last_auto})."
    )
    try:
        while True:
            snapshot = engine.take_snapshot(
                message="Auto-save", auto=True, only_if_changed=True
            )
            if snapshot is None:
                print(f"[{_now_text()}] No changes detected.")
            else:
                print(
                    f"[{_now_text()}] Created auto-save {snapshot['name']} "
                    f"(id={snapshot['id']}, saved={snapshot['timestamp']})."
                )
            pruned = engine.prune_auto_saves(keep_last=keep_last_auto)
            if pruned:
                print(f"[{_now_text()}] Pruned {pruned} old auto-save(s).")

            if run_once:
                break
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print(f"[{_now_text()}] Watcher stopped by user.")


def start_watch_background(
    project_root: Path | str,
    cli_path: Path,
    interval_minutes: int = 15,
    keep_last_auto: int = 5,
) -> int:
    project_root = Path(project_root).resolve()
    cli_path = Path(cli_path).resolve()
    cmd = [
        sys.executable,
        str(cli_path),
        "watch",
        "--foreground",
        "--path",
        str(project_root),
        "--interval-minutes",
        str(interval_minutes),
        "--keep-auto",
        str(keep_last_auto),
    ]

    popen_kwargs = {
        "cwd": str(project_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if os.name == "nt":
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        popen_kwargs["creationflags"] = detached_process | new_process_group
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    return int(proc.pid)
