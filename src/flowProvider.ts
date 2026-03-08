import * as vscode from "vscode";
import { FlowCliRunner, FlowVersion } from "./cliRunner";

export class FlowVersionItem extends vscode.TreeItem {
  public readonly contextValue = "version";
  public readonly version: FlowVersion;

  constructor(version: FlowVersion) {
    super(version.name, vscode.TreeItemCollapsibleState.None);
    this.version = version;
    this.description = version.timestamp;
    this.tooltip = `${version.name}\n${version.timestamp}\n${version.message || ""}`;

    if (version.kind === "auto") {
      this.iconPath = new vscode.ThemeIcon("history");
    } else if (version.kind === "temp") {
      this.iconPath = new vscode.ThemeIcon("archive");
    } else {
      this.iconPath = new vscode.ThemeIcon("git-commit");
    }

    this.command = {
      command: "flow.checkoutVersion",
      title: "Checkout Version",
      arguments: [this]
    };
  }
}

class ActionItem extends vscode.TreeItem {
  constructor(label: string, commandId: string, icon: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon(icon);
    this.command = { command: commandId, title: label };
    this.contextValue = "action";
  }
}

export class FlowTimelineProvider implements vscode.TreeDataProvider<FlowVersionItem> {
  private readonly runner: FlowCliRunner;
  private versions: FlowVersion[] = [];
  private initialized = false;

  private readonly _onDidChangeTreeData = new vscode.EventEmitter<FlowVersionItem | void>();
  public readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(runner: FlowCliRunner) {
    this.runner = runner;
  }

  public setInitialized(initialized: boolean): void {
    this.initialized = initialized;
  }

  public async refresh(): Promise<void> {
    if (!this.initialized) {
      this.versions = [];
      this._onDidChangeTreeData.fire();
      return;
    }

    this.versions = await this.runner.history();
    this._onDidChangeTreeData.fire();
  }

  public getTreeItem(element: FlowVersionItem): vscode.TreeItem {
    return element;
  }

  public getChildren(): vscode.ProviderResult<FlowVersionItem[]> {
    if (!this.initialized) {
      return [];
    }

    const ordered = [...this.versions].sort((a, b) => b.id - a.id);
    return ordered.map((version) => new FlowVersionItem(version));
  }
}

export class FlowActionsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private initialized = false;
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | void>();
  public readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  public setInitialized(initialized: boolean): void {
    this.initialized = initialized;
    this._onDidChangeTreeData.fire();
  }

  public getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(): vscode.ProviderResult<vscode.TreeItem[]> {
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
