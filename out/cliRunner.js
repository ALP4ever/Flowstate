"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.FlowCliRunner = void 0;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const child_process_1 = require("child_process");
const vscode = __importStar(require("vscode"));
class CliExecutionError extends Error {
    constructor(message, stdout, stderr) {
        super(message);
        this.stdout = stdout;
        this.stderr = stderr;
    }
}
class FlowCliRunner {
    constructor(workspaceRoot, extensionRoot) {
        this.workspaceRoot = workspaceRoot;
        this.extensionRoot = extensionRoot;
    }
    get rootPath() {
        return this.workspaceRoot;
    }
    get cliPath() {
        const cfg = vscode.workspace.getConfiguration("flowstate");
        const configured = cfg.get("cliPath")?.trim();
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
    isInitialized() {
        return fs.existsSync(path.join(this.workspaceRoot, ".flowstate"));
    }
    async init() {
        await this.runBestEffort(["init", this.workspaceRoot]);
    }
    async takeSnapshot(message) {
        await this.runBestEffort(["save", "-m", message, "--path", this.workspaceRoot]);
    }
    async merge(versionA, versionB, message) {
        const args = ["merge", versionA, versionB, "--path", this.workspaceRoot];
        if (message.trim()) {
            args.push("-m", message.trim());
        }
        await this.runBestEffort(args);
    }
    async goBack() {
        await this.runBestEffort(["back", "--path", this.workspaceRoot]);
    }
    async clean(all) {
        const args = ["clean", "--path", this.workspaceRoot];
        if (all) {
            args.push("--all");
        }
        await this.runBestEffort(args);
    }
    async deleteVersion(versionRef, force = false, recursive = false) {
        const args = ["delete", versionRef, "--path", this.workspaceRoot];
        if (force) {
            args.push("--force");
        }
        if (recursive) {
            args.push("--recursive");
        }
        await this.runBestEffort(args);
    }
    async gc() {
        await this.runBestEffort(["gc", "--path", this.workspaceRoot]);
    }
    async isDirty() {
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
    async checkout(versionRef, options = {}) {
        if (options.saveTemp) {
            await this.createTempSnapshot("Temporary save before checkout");
        }
        await this.runBestEffort(["checkout", versionRef, "--path", this.workspaceRoot]);
    }
    async squash(versionRef) {
        await this.runBestEffort(["squash", versionRef, "--path", this.workspaceRoot]);
    }
    async history() {
        const json = await this.runJsonIfSupported(["history", "--path", this.workspaceRoot]);
        if (json && Array.isArray(json.history)) {
            return json.history
                .map((item) => this.normalizeVersion(item))
                .filter((item) => Boolean(item));
        }
        const text = await this.runRaw(["history", "--path", this.workspaceRoot], false);
        return this.parseHistoryText(text);
    }
    async getVersionFileContent(versionRef, relativePath) {
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
    normalizeVersion(item) {
        if (!item || typeof item !== "object") {
            return undefined;
        }
        const raw = item;
        const id = Number(raw.id);
        const name = String(raw.name ?? "");
        if (!Number.isFinite(id) || !name) {
            return undefined;
        }
        const kind = name.startsWith("auto-")
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
    parseHistoryText(text) {
        const versions = [];
        const lines = text.split(/\r?\n/);
        let sectionKind;
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
            const currentFormat = /^\s*(\d+)\s+\[(VERSION|AUTO|TEMP)\]\s+(\S+)\s+\(Saved:\s*([^)]+)\)\s+parents=\[([^\]]*)\]\s*(.*)$/.exec(line);
            if (currentFormat) {
                const id = Number(currentFormat[1]);
                const label = currentFormat[2];
                const name = currentFormat[3];
                const message = currentFormat[6] ?? "";
                const kind = label === "AUTO" ? "auto" : label === "TEMP" ? "temp" : "manual";
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
            const oldFormat = /^\s*(\d+)\s+(\S+)\s+parents=\[([^\]]*)\]\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*(.*)$/.exec(line);
            if (oldFormat) {
                const id = Number(oldFormat[1]);
                const name = oldFormat[2];
                const timestamp = oldFormat[4];
                const message = (oldFormat[5] ?? "").trim();
                const kind = sectionKind ??
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
    async runBestEffort(args) {
        try {
            return await this.runRaw(args, true);
        }
        catch {
            return await this.runRaw(args, false);
        }
    }
    async runJsonIfSupported(args) {
        try {
            const raw = await this.runRaw(args, true);
            return JSON.parse(raw);
        }
        catch {
            return undefined;
        }
    }
    async runRaw(args, asJson) {
        const cliPath = this.cliPath;
        if (!fs.existsSync(cliPath)) {
            throw new Error(`FlowState CLI not found: ${cliPath}. ` +
                "Set flowstate.cliPath to your central cli.py path.");
        }
        const cliArgs = [...args];
        if (asJson) {
            cliArgs.push("--json");
        }
        const result = await this.execPython(cliPath, cliArgs, this.workspaceRoot);
        return result.stdout.trim();
    }
    async runPythonInline(script, scriptArgs) {
        const cliPath = this.cliPath;
        if (!fs.existsSync(cliPath)) {
            return undefined;
        }
        const python = this.resolvePythonSpec();
        const execArgs = [...python.prefixArgs, "-c", script, ...scriptArgs];
        try {
            const result = await this.execFileAsync(python.command, execArgs, path.dirname(cliPath));
            return JSON.parse(result.stdout.trim());
        }
        catch {
            return undefined;
        }
    }
    async createTempSnapshot(message) {
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
    async execPython(cliPath, args, cwd) {
        const python = this.resolvePythonSpec();
        const execArgs = [...python.prefixArgs, cliPath, ...args];
        return this.execFileAsync(python.command, execArgs, cwd);
    }
    resolvePythonSpec() {
        const cfg = vscode.workspace.getConfiguration("flowstate");
        const explicit = cfg.get("pythonPath")?.trim();
        if (explicit) {
            return { command: explicit, prefixArgs: [] };
        }
        const pyExt = vscode.workspace
            .getConfiguration("python")
            .get("defaultInterpreterPath")
            ?.trim();
        if (pyExt && pyExt !== "python") {
            return { command: pyExt, prefixArgs: [] };
        }
        if (process.platform === "win32") {
            return { command: "py", prefixArgs: ["-3"] };
        }
        return { command: "python3", prefixArgs: [] };
    }
    execFileAsync(command, args, cwd) {
        return new Promise((resolve, reject) => {
            (0, child_process_1.execFile)(command, args, { cwd, windowsHide: true, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
                if (error) {
                    reject(new CliExecutionError(stderr || error.message || "FlowState command failed", String(stdout ?? ""), String(stderr ?? "")));
                    return;
                }
                resolve({ stdout: String(stdout ?? ""), stderr: String(stderr ?? "") });
            });
        });
    }
}
exports.FlowCliRunner = FlowCliRunner;
//# sourceMappingURL=cliRunner.js.map