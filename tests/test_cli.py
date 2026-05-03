import json
import subprocess
import sys
from pathlib import Path

import cli as cli_module
from database import initialize_database
from engine import FlowStateEngine


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"


def run_cli(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(CLI), *args, "--json"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_cli_json_diff_export_import(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    archive = tmp_path / "flowstate-backup.zip"
    imported = tmp_path / "imported"

    (project / "file.txt").write_text("v1\n", encoding="utf-8")
    init = run_cli("init", str(project))
    assert init["ok"] is True

    first = run_cli("save", "-m", "first", "--path", str(project))
    assert first["version"]["name"] == "v1"

    (project / "file.txt").write_text("v2\n", encoding="utf-8")
    (project / "added.txt").write_text("new\n", encoding="utf-8")
    second = run_cli("save", "-m", "second", "--path", str(project))
    assert second["version"]["name"] == "v2"

    diff = run_cli("diff", "v1", "v2", "--path", str(project))
    assert diff["diff"] == {"added": ["added.txt"], "modified": ["file.txt"], "removed": []}

    exported = run_cli("export", str(archive), "--path", str(project))
    assert Path(exported["archive"]).exists()

    imported_result = run_cli("import", str(archive), "--path", str(imported))
    assert imported_result["ok"] is True

    history = run_cli("history", "--path", str(imported))
    assert [item["name"] for item in history["history"]] == ["v1", "v2"]


def test_cli_reports_json_errors(tmp_path):
    result = subprocess.run(
        [sys.executable, str(CLI), "status", "--path", str(tmp_path), "--json"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "FlowState database not found" in payload["error"]


def test_checkout_json_prompts_and_creates_temp_snapshot_when_interactive(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    initialize_database(project)

    (project / "file.txt").write_text("v1\n", encoding="utf-8")
    run_cli("save", "-m", "first", "--path", str(project))

    (project / "file.txt").write_text("v2\n", encoding="utf-8")
    run_cli("save", "-m", "second", "--path", str(project))

    (project / "file.txt").write_text("dirty work\n", encoding="utf-8")

    parser = cli_module.build_parser()
    args = parser.parse_args(["checkout", "v1", "--path", str(project), "--json"])

    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)

    args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert prompts == [
        "Unsaved changes detected. Create temporary snapshot 'temp' before checkout? [y/N]: "
    ]
    assert payload["temp_created"] is not None
    assert payload["temp_created"]["name"] == "temp"
    assert payload["version"]["name"] == "v1"
    assert (project / "file.txt").read_text(encoding="utf-8") == "v1\n"

    history_names = [item["name"] for item in FlowStateEngine(project).get_history()]
    assert history_names == ["v1", "v2", "temp"]
