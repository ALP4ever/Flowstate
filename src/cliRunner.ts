import * as fs from "fs";
import * as path from "path";
import { execFile } from "child_process";
import * as vscode from "vscode";

export interface FlowVersion {
  id: number;
  name: string;
  parent1_id: number | null;
  parent2_id: number | null;
  message: string;
  timestamp: string;
  kind: "manual" | "auto" | "temp";
}

interface ExecResult {
  stdout: string;
  stderr: string;
}

interface PythonExecSpec {
  command: string;
  prefixArgs: string[];
}

export interface CheckoutOptions {
  saveTemp?: boolean;
  noTemp?: boolean;
}

class CliExecutionError extends Error {
  public readonly stdout: string;
  public readonly stderr: string;

  constructor(message: string, stdout: string, stderr: string) {
    super(message);
    this.stdout = stdout;
    this.stderr = stderr;
  }
}

export class FlowCliRunner {
  private readonly workspaceRoot: string;
  private readonly extensionRoot?: string;

  constructor(workspaceRoot: string, extensionRoot?: string) {
    this.workspaceRoot = workspaceRoot;
    this.extensionRoot = extensionRoot;
  }

  public get rootPath(): string {
    return this.workspaceRoot;
  }

  public get cliPath(): string {
    const cfg = vscode.workspace.getConfiguration("flowstate");
    const configured = cfg.get<string>("cliPath")?.trim();
    if (configured) {
      if (path.isAbsolute(configured)) {
        return configured;
      }

      const workspaceCandidate = path.resolve(this.workspaceRoot, configured);
      if (fs.existsSync(workspaceCandidate)) {
        return workspaceCandidate;
      }

      if (this.extensionRoot) {
        const extensionCandidate = path.resolve(this.extensionRoot, configured);
        if (fs.existsSync(extensionCandidate)) {
          return extensionCandidate;
        }
      }

      return workspaceCandidate;
    }

    if (this.extensionRoot) {
      const bundled = path.join(this.extensionRoot, "core", "cli.py");
      if (fs.existsSync(bundled)) {
        return bundled;
      }

      const extensionRootCli = path.join(this.extensionRoot, "cli.py");
      if (fs.existsSync(extensionRootCli)) {
        return extensionRootCli;
      }
    }

    return path.join(this.workspaceRoot, "cli.py");
  }

  public isInitialized(): boolean {
    return fs.existsSync(path.join(this.workspaceRoot, ".flowstate"));
  }

  public async init(): Promise<void> {
    await this.runBestEffort(["init", this.workspaceRoot]);
  }

  public async takeSnapshot(message: string): Promise<void> {
    await this.runBestEffort(["save", "-m", message, "--path", this.workspaceRoot]);
  }

  public async merge(versionA: string, versionB: string, message: string): Promise<void> {
    const args = ["merge", versionA, versionB, "--path", this.workspaceRoot];
    if (message.trim()) {
      args.push("-m", message.trim());
    }
    await this.runBestEffort(args);
  }

  public async goBack(): Promise<void> {
    await this.runBestEffort(["back", "--path", this.workspaceRoot]);
  }

  public async clean(all: boolean): Promise<void> {
    const args = ["clean", "--path", this.workspaceRoot];
    if (all) {
      args.push("--all");
    }
    await this.runBestEffort(args);
  }

  public async deleteVersion(
    versionRef: string,
    force = false,
    recursive = false
  ): Promise<void> {
    const args = ["delete", versionRef, "--path", this.workspaceRoot];
    if (force) {
      args.push("--force");
    }
    if (recursive) {
      args.push("--recursive");
    }
    await this.runBestEffort(args);
  }

  public async gc(): Promise<void> {
    await this.runBestEffort(["gc", "--path", this.workspaceRoot]);
  }

  public async isDirty(): Promise<boolean> {
    const script = `
import json
import sys
from engine import FlowStateEngine

engine = FlowStateEngine(sys.argv[1])
print(json.dumps({"dirty": engine.is_dirty()}))
`.trim();
    const parsed = await this.runPythonInline(script, [this.workspaceRoot]);
    return Boolean(parsed && parsed.dirty === true);
  }

  public async checkout(versionRef: string, options: CheckoutOptions = {}): Promise<void> {
    if (options.saveTemp) {
      await this.createTempSnapshot("Temporary save before checkout");
    }
    await this.runBestEffort(["checkout", versionRef, "--path", this.workspaceRoot]);
  }

  public async squash(versionRef: string): Promise<void> {
    await this.runBestEffort(["squash", versionRef, "--path", this.workspaceRoot]);
  }

  public async history(): Promise<FlowVersion[]> {
    const json = await this.runJsonIfSupported(["history", "--path", this.workspaceRoot]);
    if (json && Array.isArray(json.history)) {
      return json.history
        .map((item: unknown) => this.normalizeVersion(item))
        .filter((item: FlowVersion | undefined): item is FlowVersion => Boolean(item));
    }

    const text = await this.runRaw(["history", "--path", this.workspaceRoot], false);
    return this.parseHistoryText(text);
  }

  public async getVersionFileContent(
    versionRef: string,
    relativePath: string
  ): Promise<string | undefined> {
    const root = this.workspaceRoot;
    const script = `
import json
import sys
from engine import FlowStateEngine

root = sys.argv[1]
version_ref = sys.argv[2]
rel_path = sys.argv[3].replace('\\\\\\\\', '/')
engine = FlowStateEngine(root)
with engine._conn() as conn:
    row = engine._version_row(conn, version_ref)
    files = engine._collect_files_from_tree(conn, row["root_tree_hash"])
    object_hash = files.get(rel_path)
    if object_hash is None:
        print(json.dumps({"ok": False, "missing": True}))
    else:
        data = engine._get_object_content(conn, object_hash)
        print(json.dumps({"ok": True, "content": data.decode("utf-8", errors="replace")}))
`.trim();

    const parsed = await this.runPythonInline(script, [root, versionRef, relativePath]);
    if (parsed && parsed.ok === true && typeof parsed.content === "string") {
      return parsed.content;
    }
    return undefined;
  }

  private normalizeVersion(item: unknown): FlowVersion | undefined {
    if (!item || typeof item !== "object") {
      return undefined;
    }

    const raw = item as Record<string, unknown>;
    const id = Number(raw.id);
    const name = String(raw.name ?? "");
    if (!Number.isFinite(id) || !name) {
      return undefined;
    }

    const kind: FlowVersion["kind"] = name.startsWith("auto-")
      ? "auto"
      : name === "temp" || name.startsWith("temp-")
      ? "temp"
      : "manual";

    return {
      id,
      name,
      parent1_id: raw.parent1_id === null ? null : Number(raw.parent1_id ?? NaN),
      parent2_id: raw.parent2_id === null ? null : Number(raw.parent2_id ?? NaN),
      message: String(raw.message ?? ""),
      timestamp: String(raw.timestamp ?? ""),
      kind
    };
  }

  private parseHistoryText(text: string): FlowVersion[] {
    const versions: FlowVersion[] = [];
    const lines = text.split(/\r?\n/);
    let sectionKind: FlowVersion["kind"] | undefined;

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      const normalized = line.trim();
      if (!normalized) {
        continue;
      }

      if (normalized.startsWith("Versions:")) {
        sectionKind = "manual";
        continue;
      }
      if (normalized.startsWith("Auto-Saves:")) {
        sectionKind = "auto";
        continue;
      }
      if (normalized.startsWith("Temp Saves:")) {
        sectionKind = "temp";
        continue;
      }

      const currentFormat =
        /^\s*(\d+)\s+\[(VERSION|AUTO|TEMP)\]\s+(\S+)\s+\(Saved:\s*([^)]+)\)\s+parents=\[([^\]]*)\]\s*(.*)$/.exec(
          line
        );
      if (currentFormat) {
        const id = Number(currentFormat[1]);
        const label = currentFormat[2];
        const name = currentFormat[3];
        const message = currentFormat[6] ?? "";
        const kind: FlowVersion["kind"] =
          label === "AUTO" ? "auto" : label === "TEMP" ? "temp" : "manual";

        versions.push({
          id,
          name,
          parent1_id: null,
          parent2_id: null,
          message: message.trim(),
          timestamp: currentFormat[4].trim(),
          kind
        });
        continue;
      }

      const oldFormat =
        /^\s*(\d+)\s+(\S+)\s+parents=\[([^\]]*)\]\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*(.*)$/.exec(
          line
        );
      if (oldFormat) {
        const id = Number(oldFormat[1]);
        const name = oldFormat[2];
        const timestamp = oldFormat[4];
        const message = (oldFormat[5] ?? "").trim();
        const kind: FlowVersion["kind"] =
          sectionKind ??
          (name.startsWith("auto-")
            ? "auto"
            : name === "temp" || name.startsWith("temp-")
            ? "temp"
            : "manual");
        versions.push({
          id,
          name,
          parent1_id: null,
          parent2_id: null,
          message,
          timestamp,
          kind
        });
      }
    }

    return versions.sort((a, b) => a.id - b.id);
  }

  private async runBestEffort(args: string[]): Promise<string> {
    try {
      return await this.runRaw(args, true);
    } catch {
      return await this.runRaw(args, false);
    }
  }

  private async runJsonIfSupported(args: string[]): Promise<any | undefined> {
    try {
      const raw = await this.runRaw(args, true);
      return JSON.parse(raw);
    } catch {
      return undefined;
    }
  }

  private async runRaw(args: string[], asJson: boolean): Promise<string> {
    const cliPath = this.cliPath;
    if (!fs.existsSync(cliPath)) {
      throw new Error(
        `FlowState CLI not found: ${cliPath}. ` +
          "Set flowstate.cliPath to your central cli.py path."
      );
    }

    const cliArgs = [...args];
    if (asJson) {
      cliArgs.push("--json");
    }

    const result = await this.execPython(cliPath, cliArgs, this.workspaceRoot);
    return result.stdout.trim();
  }

  private async runPythonInline(script: string, scriptArgs: string[]): Promise<any | undefined> {
    const cliPath = this.cliPath;
    if (!fs.existsSync(cliPath)) {
      return undefined;
    }
    const python = this.resolvePythonSpec();
    const execArgs = [...python.prefixArgs, "-c", script, ...scriptArgs];
    try {
      const result = await this.execFileAsync(
        python.command,
        execArgs,
        path.dirname(cliPath)
      );
      return JSON.parse(result.stdout.trim());
    } catch {
      return undefined;
    }
  }

  private async createTempSnapshot(message: string): Promise<void> {
    const script = `
import json
import sys
from engine import FlowStateEngine

engine = FlowStateEngine(sys.argv[1])
snapshot = engine.take_snapshot(message=sys.argv[2], name_prefix="temp")
print(json.dumps({"ok": True, "created": snapshot is not None}))
`.trim();
    const parsed = await this.runPythonInline(script, [this.workspaceRoot, message]);
    if (!parsed || parsed.ok !== true) {
      throw new Error("Failed to create temporary snapshot.");
    }
  }

  private async execPython(cliPath: string, args: string[], cwd: string): Promise<ExecResult> {
    const python = this.resolvePythonSpec();
    const execArgs = [...python.prefixArgs, cliPath, ...args];
    return this.execFileAsync(python.command, execArgs, cwd);
  }

  private resolvePythonSpec(): PythonExecSpec {
    const cfg = vscode.workspace.getConfiguration("flowstate");
    const explicit = cfg.get<string>("pythonPath")?.trim();
    if (explicit) {
      return { command: explicit, prefixArgs: [] };
    }

    const pyExt = vscode.workspace
      .getConfiguration("python")
      .get<string>("defaultInterpreterPath")
      ?.trim();
    if (pyExt && pyExt !== "python") {
      return { command: pyExt, prefixArgs: [] };
    }

    if (process.platform === "win32") {
      return { command: "py", prefixArgs: ["-3"] };
    }

    return { command: "python3", prefixArgs: [] };
  }

  private execFileAsync(
    command: string,
    args: string[],
    cwd: string
  ): Promise<ExecResult> {
    return new Promise((resolve, reject) => {
      execFile(
        command,
        args,
        { cwd, windowsHide: true, maxBuffer: 10 * 1024 * 1024 },
        (error, stdout, stderr) => {
          if (error) {
            reject(
              new CliExecutionError(
                stderr || error.message || "FlowState command failed",
                String(stdout ?? ""),
                String(stderr ?? "")
              )
            );
            return;
          }
          resolve({ stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
        }
      );
    });
  }
}
