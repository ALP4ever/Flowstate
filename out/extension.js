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
exports.activate = activate;
exports.deactivate = deactivate;
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const cliRunner_1 = require("./cliRunner");
const flowProvider_1 = require("./flowProvider");
class FlowVirtualContentProvider {
    constructor() {
        this.docs = new Map();
        this._onDidChange = new vscode.EventEmitter();
        this.onDidChange = this._onDidChange.event;
    }
    setContent(uri, content) {
        this.docs.set(uri.toString(), content);
        this._onDidChange.fire(uri);
    }
    provideTextDocumentContent(uri) {
        return this.docs.get(uri.toString()) ?? "";
    }
}
async function activate(context) {
    const workspace = vscode.workspace.workspaceFolders?.[0];
    if (!workspace) {
        vscode.window.showWarningMessage("FlowState: open a folder workspace to use the extension.");
        return;
    }
    const workspaceRoot = workspace.uri.fsPath;
    const runner = new cliRunner_1.FlowCliRunner(workspaceRoot, context.extensionPath);
    const timelineProvider = new flowProvider_1.FlowTimelineProvider(runner);
    const actionsProvider = new flowProvider_1.FlowActionsProvider();
    const virtualProvider = new FlowVirtualContentProvider();
    const initialized = runner.isInitialized();
    timelineProvider.setInitialized(initialized);
    actionsProvider.setInitialized(initialized);
    const timelineView = vscode.window.createTreeView("flowTimeline", {
        treeDataProvider: timelineProvider,
        showCollapseAll: false
    });
    const actionsView = vscode.window.createTreeView("flowActions", {
        treeDataProvider: actionsProvider,
        showCollapseAll: false
    });
    context.subscriptions.push(timelineView, actionsView, vscode.workspace.registerTextDocumentContentProvider("flowstate", virtualProvider));
    const withVersionItem = (item) => {
        if (item) {
            return item;
        }
        return timelineView.selection?.[0];
    };
    const pickVersionItem = async (title) => {
        const versions = await runner.history();
        if (!versions.length) {
            vscode.window.showInformationMessage("No FlowState versions found.");
            return undefined;
        }
        const choices = versions
            .slice()
            .sort((a, b) => b.id - a.id)
            .map((version) => ({
            label: version.name,
            description: `id=${version.id} | ${version.timestamp}`,
            detail: version.message || "",
            version
        }));
        const selected = await vscode.window.showQuickPick(choices, {
            title,
            matchOnDescription: true,
            matchOnDetail: true
        });
        if (!selected) {
            return undefined;
        }
        return new flowProvider_1.FlowVersionItem(selected.version);
    };
    const resolveVersionItem = async (item, title) => {
        return withVersionItem(item) ?? pickVersionItem(title);
    };
    const setInitializedState = async () => {
        const state = runner.isInitialized();
        timelineProvider.setInitialized(state);
        actionsProvider.setInitialized(state);
        await timelineProvider.refresh();
    };
    context.subscriptions.push(vscode.commands.registerCommand("flow.refreshTimeline", async () => {
        await setInitializedState();
    }), vscode.commands.registerCommand("flow.init", async () => {
        try {
            await runner.init();
            await setInitializedState();
            vscode.window.showInformationMessage("FlowState initialized.");
        }
        catch (error) {
            vscode.window.showErrorMessage(`FlowState init failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.takeSnapshot", async () => {
        try {
            if (!runner.isInitialized()) {
                const answer = await vscode.window.showInformationMessage("FlowState is not initialized. Initialize now?", "Initialize", "Cancel");
                if (answer !== "Initialize") {
                    return;
                }
                await runner.init();
            }
            const message = await vscode.window.showInputBox({
                title: "FlowState Snapshot",
                prompt: "Snapshot message",
                placeHolder: "Describe this version"
            });
            if (message === undefined) {
                return;
            }
            await runner.takeSnapshot(message);
            await setInitializedState();
            vscode.window.showInformationMessage("FlowState snapshot saved.");
        }
        catch (error) {
            vscode.window.showErrorMessage(`Snapshot failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.goBack", async () => {
        try {
            await runner.goBack();
            await setInitializedState();
            vscode.window.showInformationMessage("Returned to temporary snapshot.");
        }
        catch (error) {
            vscode.window.showErrorMessage(`Back failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.clean", async () => {
        try {
            const selected = await vscode.window.showQuickPick([
                { label: "Default clean", description: "temp/auto older than 24h", all: false },
                { label: "Clean --all", description: "aggressive cleanup", all: true }
            ], { title: "FlowState Clean" });
            if (!selected) {
                return;
            }
            await runner.clean(selected.all);
            await setInitializedState();
            vscode.window.showInformationMessage(`FlowState clean completed (${selected.label}).`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Clean failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.mergeVersions", async (arg) => {
        try {
            const first = await resolveVersionItem(arg, "FlowState: select first version to merge");
            if (!first) {
                return;
            }
            const second = await pickVersionItem(`FlowState: select second version to merge with ${first.version.name}`);
            if (!second) {
                return;
            }
            if (second.version.id === first.version.id) {
                vscode.window.showInformationMessage("Select two different versions for merge.");
                return;
            }
            const message = await vscode.window.showInputBox({
                title: "FlowState Merge",
                prompt: `Merge ${first.version.name} + ${second.version.name}`,
                value: `Merge ${first.version.name}+${second.version.name}`
            });
            if (message === undefined) {
                return;
            }
            await runner.merge(String(first.version.id), String(second.version.id), message);
            await setInitializedState();
            vscode.window.showInformationMessage(`Merged ${first.version.name} + ${second.version.name}.`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Merge failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.checkoutVersion", async (arg) => {
        try {
            const item = await resolveVersionItem(arg, "FlowState: select version to checkout");
            if (!item) {
                return;
            }
            let checkoutOptions = { noTemp: true };
            const dirty = await runner.isDirty();
            if (dirty && item.version.kind !== "temp") {
                const answer = await vscode.window.showWarningMessage("Unsaved changes detected. Create temporary snapshot `temp` before checkout?", { modal: true }, "Create Temp", "Skip Temp");
                if (!answer) {
                    return;
                }
                checkoutOptions = answer === "Create Temp" ? { saveTemp: true } : { noTemp: true };
            }
            await runner.checkout(String(item.version.id), checkoutOptions);
            await setInitializedState();
            vscode.window.showInformationMessage(`Checked out ${item.version.name}.`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Checkout failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.squashVersion", async (arg) => {
        try {
            const item = await resolveVersionItem(arg, "FlowState: select version to squash");
            if (!item) {
                return;
            }
            const answer = await vscode.window.showWarningMessage(`Squash ${item.version.name} and make it a root?`, { modal: true }, "Squash");
            if (answer !== "Squash") {
                return;
            }
            await runner.squash(String(item.version.id));
            await setInitializedState();
            vscode.window.showInformationMessage(`${item.version.name} is now a root version.`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Squash failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.deleteVersion", async (arg) => {
        try {
            const item = await resolveVersionItem(arg, "FlowState: select version to delete");
            if (!item) {
                return;
            }
            const mode = await vscode.window.showQuickPick([
                {
                    label: "Safe delete",
                    description: "Fail if this version has children",
                    force: false,
                    recursive: false
                },
                {
                    label: "Force detach",
                    description: "Detach children and delete target",
                    force: true,
                    recursive: false
                },
                {
                    label: "Recursive",
                    description: "Delete target and removable ancestors",
                    force: false,
                    recursive: true
                }
            ], { title: `FlowState delete mode for ${item.version.name}` });
            if (!mode) {
                return;
            }
            const answer = await vscode.window.showWarningMessage(`Delete ${item.version.name} using mode "${mode.label}"?`, { modal: true }, "Delete");
            if (answer !== "Delete") {
                return;
            }
            await runner.deleteVersion(String(item.version.id), mode.force, mode.recursive);
            await setInitializedState();
            vscode.window.showInformationMessage(`Deleted ${item.version.name} (${mode.label}).`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Delete failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.gc", async () => {
        try {
            await runner.gc();
            await setInitializedState();
            vscode.window.showInformationMessage("FlowState GC completed.");
        }
        catch (error) {
            vscode.window.showErrorMessage(`GC failed: ${String(error)}`);
        }
    }), vscode.commands.registerCommand("flow.compareWithCurrent", async (arg) => {
        try {
            const item = withVersionItem(arg);
            if (!item) {
                vscode.window.showInformationMessage("Select a version in FlowState timeline.");
                return;
            }
            const editor = vscode.window.activeTextEditor;
            if (!editor) {
                vscode.window.showInformationMessage("Open a file to compare with current.");
                return;
            }
            const activePath = editor.document.uri.fsPath;
            const relPath = path.relative(workspaceRoot, activePath).replace(/\\/g, "/");
            if (!relPath || relPath.startsWith("..")) {
                vscode.window.showWarningMessage("Active file is outside current FlowState workspace.");
                return;
            }
            const oldContent = await runner.getVersionFileContent(String(item.version.id), relPath);
            if (oldContent === undefined) {
                vscode.window.showWarningMessage(`File not found in ${item.version.name}: ${relPath}`);
                return;
            }
            const leftUri = vscode.Uri.from({
                scheme: "flowstate",
                path: `/${item.version.id}/${relPath}`,
                query: String(Date.now())
            });
            virtualProvider.setContent(leftUri, oldContent);
            await vscode.commands.executeCommand("vscode.diff", leftUri, editor.document.uri, `${item.version.name} ↔ Current • ${relPath}`);
        }
        catch (error) {
            vscode.window.showErrorMessage(`Compare failed: ${String(error)}`);
        }
    }));
    const snapshotStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 200);
    snapshotStatus.text = "$(save) Flow Snapshot";
    snapshotStatus.tooltip = "Take FlowState snapshot";
    snapshotStatus.command = "flow.takeSnapshot";
    snapshotStatus.show();
    const backStatus = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 199);
    backStatus.text = "$(history) Flow Back";
    backStatus.tooltip = "Go back to latest FlowState temp snapshot";
    backStatus.command = "flow.goBack";
    backStatus.show();
    context.subscriptions.push(snapshotStatus, backStatus);
    await setInitializedState();
}
function deactivate() {
    // No resources to dispose beyond subscriptions.
}
//# sourceMappingURL=extension.js.map