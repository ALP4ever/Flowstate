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

interface CliJsonError {
  ok: false;
  error?: string;
}

type CliJsonResult<T> = (T & { ok: true }) | CliJsonError;

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
    // Strong fast-path check: the FlowState SQLite database must exist.
    const dbPath = path.join(this.workspaceRoot, ".flowstate", "flowstate.db");
    return fs.existsSync(dbPath);
  }

  public async verifyInitialized(): Promise<{ initialized: boolean; error?: string }> {
    const result = await this.runJson<{ initialized: boolean; error?: string }>([
      "is-init",
      "--path",
      this.workspaceRoot,
    ]);
    if (!result || result.ok !== true) {
      return { initialized: false, error: result?.error };
    }
    return { initialized: Boolean(result.initialized), error: result.error };
  }

  public async init(): Promise<void> {
    await this.runBestEffort(["init", this.workspaceRoot]);
  }

  public async takeSnapshot(message: string): Promise<void> {
    const args = ["save", "--path", this.workspaceRoot];
    if (message.trim()) {
      args.push("-m", message);
    }
    const result = await this.runJson<{ created: boolean }>(args);
    if (!result || result.ok !== true) {
      throw new Error(result?.error || "Failed to create snapshot.");
    }
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
    const result = await this.runJson<{ dirty: boolean }>([
      "status",
      "--path",
      this.workspaceRoot,
    ]);
    if (!result || result.ok !== true) {
      return false;
    }
    return Boolean(result.dirty);
  }

  public async checkout(versionRef: string, options: CheckoutOptions = {}): Promise<void> {
    const args = ["checkout", versionRef, "--path", this.workspaceRoot];
    if (options.saveTemp) {
      args.push("--save-temp");
    } else if (options.noTemp) {
      args.push("--no-temp");
    }
    await this.runBestEffort(args);
  }

  public async squash(versionRef: string): Promise<void> {
    await this.runBestEffort(["squash", versionRef, "--path", this.workspaceRoot]);
  }

  public async history(): Promise<FlowVersion[]> {
    const result = await this.runJson<{ history: unknown[] }>([
      "history",
      "--path",
      this.workspaceRoot,
    ]);
    if (!result || result.ok !== true || !Array.isArray(result.history)) {
      return [];
    }
    return result.history
      .map((item) => this.normalizeVersion(item))
      .filter((item): item is FlowVersion => Boolean(item));
  }

  public async getVersionFileContent(
    versionRef: string,
    relativePath: string
  ): Promise<string | undefined> {
    const result = await this.runJson<{ missing: boolean; content: string | null }>([
      "show",
      versionRef,
      relativePath,
      "--path",
      this.workspaceRoot,
    ]);
    if (!result || result.ok !== true || result.missing) {
      return undefined;
    }
    return typeof result.content === "string" ? result.content : undefined;
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

    const explicitKind =
      typeof raw.kind === "string" && ["manual", "auto", "temp"].includes(raw.kind)
        ? (raw.kind as FlowVersion["kind"])
        : undefined;

    const inferredKind: FlowVersion["kind"] = name.startsWith("auto-")
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
      kind: explicitKind ?? inferredKind,
    };
  }

  private async runBestEffort(args: string[]): Promise<string> {
    try {
      return await this.runRaw(args, true);
    } catch {
      return await this.runRaw(args, false);
    }
  }

  private async runJson<T>(args: string[]): Promise<CliJsonResult<T> | undefined> {
    try {
      const raw = await this.runRaw(args, true);
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        return parsed as CliJsonResult<T>;
      }
      return undefined;
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
        {
          cwd,
          windowsHide: true,
          maxBuffer: 10 * 1024 * 1024,
          env: { ...process.env, PYTHONIOENCODING: "utf-8" },
        },
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
