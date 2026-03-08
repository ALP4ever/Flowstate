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
exports.FlowActionsProvider = exports.FlowTimelineProvider = exports.FlowVersionItem = void 0;
const vscode = __importStar(require("vscode"));
class FlowVersionItem extends vscode.TreeItem {
    constructor(version) {
        super(version.name, vscode.TreeItemCollapsibleState.None);
        this.contextValue = "version";
        this.version = version;
        this.description = version.timestamp;
        this.tooltip = `${version.name}\n${version.timestamp}\n${version.message || ""}`;
        if (version.kind === "auto") {
            this.iconPath = new vscode.ThemeIcon("history");
        }
        else if (version.kind === "temp") {
            this.iconPath = new vscode.ThemeIcon("archive");
        }
        else {
            this.iconPath = new vscode.ThemeIcon("git-commit");
        }
        this.command = {
            command: "flow.checkoutVersion",
            title: "Checkout Version",
            arguments: [this]
        };
    }
}
exports.FlowVersionItem = FlowVersionItem;
class ActionItem extends vscode.TreeItem {
    constructor(label, commandId, icon) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.iconPath = new vscode.ThemeIcon(icon);
        this.command = { command: commandId, title: label };
        this.contextValue = "action";
    }
}
class FlowTimelineProvider {
    constructor(runner) {
        this.versions = [];
        this.initialized = false;
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
        this.runner = runner;
    }
    setInitialized(initialized) {
        this.initialized = initialized;
    }
    async refresh() {
        if (!this.initialized) {
            this.versions = [];
            this._onDidChangeTreeData.fire();
            return;
        }
        this.versions = await this.runner.history();
        this._onDidChangeTreeData.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren() {
        if (!this.initialized) {
            return [];
        }
        const ordered = [...this.versions].sort((a, b) => b.id - a.id);
        return ordered.map((version) => new FlowVersionItem(version));
    }
}
exports.FlowTimelineProvider = FlowTimelineProvider;
class FlowActionsProvider {
    constructor() {
        this.initialized = false;
        this._onDidChangeTreeData = new vscode.EventEmitter();
        this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    }
    setInitialized(initialized) {
        this.initialized = initialized;
        this._onDidChangeTreeData.fire();
    }
    getTreeItem(element) {
        return element;
    }
    getChildren() {
        if (!this.initialized) {
            return [
                new ActionItem("Initialize FlowState", "flow.init", "repo-create")
            ];
        }
        return [
            new ActionItem("Take Snapshot", "flow.takeSnapshot", "save"),
            new ActionItem("Merge Versions", "flow.mergeVersions", "git-merge"),
            new ActionItem("Go Back", "flow.goBack", "history"),
            new ActionItem("Squash Version", "flow.squashVersion", "git-merge"),
            new ActionItem("Delete Version", "flow.deleteVersion", "trash"),
            new ActionItem("Run GC", "flow.gc", "symbol-event"),
            new ActionItem("Clean", "flow.clean", "clear-all")
        ];
    }
}
exports.FlowActionsProvider = FlowActionsProvider;
//# sourceMappingURL=flowProvider.js.map