import type { EventRecord, TaskNodeRecord, ThreadRecord, WorkspaceId } from "../../../generated/harness/protocol/atlas-harness";

export type IdeViewState = "loading" | "empty" | "ready" | "partial" | "error" | "unauthorized" | "stale" | "resync";

export interface IdeSnapshot {
  readonly workspaceId: WorkspaceId;
  readonly state: IdeViewState;
  readonly threads: readonly ThreadRecord[];
  readonly tasks: readonly TaskNodeRecord[];
  readonly events: readonly Pick<EventRecord, "event_id" | "event_type" | "aggregate_sequence">[];
  readonly errorCode?: string;
}

export type IdeListener = (snapshot: IdeSnapshot) => void;

export class IdeWorkspaceAdapter {
  private readonly listeners = new Set<IdeListener>();
  private snapshotValue: IdeSnapshot;

  constructor(
    private readonly workspaceId: WorkspaceId,
    private readonly maximumRows = 256,
  ) {
    if (!Number.isInteger(maximumRows) || maximumRows < 1 || maximumRows > 256) {
      throw new RangeError("IDE row bound is outside the SDK limit");
    }
    this.snapshotValue = { workspaceId, state: "loading", threads: [], tasks: [], events: [] };
  }

  subscribe(listener: IdeListener): () => void {
    if (this.listeners.size >= 64) {
      throw new RangeError("IDE listener capacity is exhausted");
    }
    this.listeners.add(listener);
    listener(this.snapshotValue);
    return () => this.listeners.delete(listener);
  }

  update(
    patch: Partial<Omit<IdeSnapshot, "workspaceId">>,
  ): void {
    const next: IdeSnapshot = {
      workspaceId: this.workspaceId,
      state: patch.state ?? this.snapshotValue.state,
      threads: (patch.threads ?? this.snapshotValue.threads).slice(0, this.maximumRows),
      tasks: (patch.tasks ?? this.snapshotValue.tasks).slice(0, this.maximumRows),
      events: (patch.events ?? this.snapshotValue.events).slice(0, this.maximumRows),
      ...(patch.errorCode === undefined ? {} : { errorCode: patch.errorCode }),
    };
    this.snapshotValue = next;
    for (const listener of this.listeners) {
      listener(next);
    }
  }

  snapshot(): IdeSnapshot {
    return {
      ...this.snapshotValue,
      threads: [...this.snapshotValue.threads],
      tasks: [...this.snapshotValue.tasks],
      events: [...this.snapshotValue.events],
    };
  }

  dispose(): void {
    this.listeners.clear();
    this.update({ state: "stale" });
  }
}
