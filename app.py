from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from database import initialize_database, is_initialized
from engine import FlowStateEngine, FlowStateError


def _format_saved(timestamp: str) -> str:
    try:
        return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return timestamp


def _version_label(version: dict) -> str:
    kind = "AUTO" if version.get("kind") == "auto" else (
        "TEMP" if version.get("kind") == "temp" else "VERSION"
    )
    return f"{version['name']} [{kind}] (Saved: {_format_saved(version['timestamp'])}, id={version['id']})"


def _render_timeline(history: list[dict]) -> None:
    st.subheader("Timeline")
    if not history:
        st.info("No versions yet. Save your first snapshot.")
        return

    for version in reversed(history):
        parents = [p for p in [version["parent1_id"], version["parent2_id"]] if p is not None]
        parent_text = ", ".join(str(p) for p in parents) if parents else "None"
        kind = version.get("kind")
        if kind == "auto":
            badge = "AUTO-SAVE"
            badge_color = "#f59e0b"
        elif kind == "temp":
            badge = "TEMP-SAVE"
            badge_color = "#a855f7"
        else:
            badge = "VERSION"
            badge_color = "#0ea5e9"
        saved_at = _format_saved(version["timestamp"])
        st.markdown(
            (
                f"<span style='display:inline-block;padding:2px 8px;border-radius:999px;"
                f"background:{badge_color};color:white;font-size:12px;'>{badge}</span> "
                f"<strong>{version['name']} (Saved: {saved_at})</strong> "
                f"<span style='color:#64748b;'>(id={version['id']})</span><br/>"
                f"Parents: {parent_text}<br/>"
                f"Message: {version['message'] or 'No message'}"
            ),
            unsafe_allow_html=True,
        )
        st.divider()


def _render_diff(engine: FlowStateEngine, history: list[dict]) -> None:
    st.subheader("Difference Preview")
    if len(history) < 2:
        st.caption("Need at least two versions.")
        return

    version_map = {_version_label(v): v["id"] for v in history}
    labels = list(version_map.keys())
    col_left, col_right = st.columns(2)
    with col_left:
        from_label = st.selectbox("From Version", labels, index=max(0, len(labels) - 2))
    with col_right:
        to_label = st.selectbox("To Version", labels, index=len(labels) - 1)

    from_id = version_map[from_label]
    to_id = version_map[to_label]
    if from_id == to_id:
        st.caption("Select two different versions.")
        return

    diff = engine.describe_diff(from_id, to_id)
    st.write(
        f"Changed from `{from_label}` to `{to_label}`: "
        f"{len(diff['added'])} added, {len(diff['modified'])} modified, {len(diff['removed'])} removed."
    )
    if diff["added"]:
        st.markdown("**Added Files**")
        st.code("\n".join(diff["added"]), language="text")
    if diff["modified"]:
        st.markdown("**Modified Files**")
        st.code("\n".join(diff["modified"]), language="text")
    if diff["removed"]:
        st.markdown("**Removed Files**")
        st.code("\n".join(diff["removed"]), language="text")


def main() -> None:
    st.set_page_config(page_title="FlowState", layout="wide")
    st.title("FlowState")
    st.caption("Local-first snapshot VCS with immutable versions.")

    default_root = str(Path.cwd())
    root_input = st.text_input("Project Root", value=default_root)
    project_root = Path(root_input).expanduser().resolve()

    if not is_initialized(project_root):
        st.warning(f"FlowState is not initialized at `{project_root}`.")
        if st.button("Initialize FlowState"):
            initialize_database(project_root)
            st.success("Initialized successfully.")
            st.rerun()
        return

    try:
        engine = FlowStateEngine(project_root)
    except FlowStateError as exc:
        st.error(str(exc))
        return

    history = engine.get_history()

    left, right = st.columns([2, 1])
    with right:
        st.subheader("Action Bar")
        msg = st.text_input("Snapshot Message", placeholder="Describe this version")
        if st.button("Save New Version", use_container_width=True):
            version = engine.take_snapshot(msg or "Snapshot")
            st.success(f"Saved {version['name']} (id={version['id']}).")
            st.rerun()

        st.subheader("Merge Tool")
        if len(history) < 2:
            st.caption("Need at least two versions to merge.")
        else:
            labels = [_version_label(v) for v in history]
            label_to_id = {_version_label(v): v["id"] for v in history}
            label_a = st.selectbox("Version A", labels, index=max(0, len(labels) - 2))
            label_b = st.selectbox("Version B", labels, index=len(labels) - 1)
            merge_msg = st.text_input(
                "Merge Message", placeholder="Merge summary", value=""
            )
            if st.button("Merge into New Version", use_container_width=True):
                id_a = label_to_id[label_a]
                id_b = label_to_id[label_b]
                if id_a == id_b:
                    st.error("Select two different versions.")
                else:
                    merged = engine.smart_merge(id_a, id_b, merge_msg or "")
                    st.success(f"Merged into {merged['name']} (id={merged['id']}).")
                    if merged["conflicts"]:
                        st.warning(
                            "Conflicts were auto-resolved for: "
                            + ", ".join(merged["conflicts"])
                        )
                    st.rerun()

    with left:
        _render_timeline(history)
        _render_diff(engine, history)


if __name__ == "__main__":
    if st.runtime.exists():
        main()
    else:
        print("Run this UI with: streamlit run app.py")
