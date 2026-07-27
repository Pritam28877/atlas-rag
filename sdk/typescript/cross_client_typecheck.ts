import {
  BoundedEventBuffer,
  HarnessClient,
  IdeWorkspaceAdapter,
  type HarnessTransport,
} from "./src/index";
import type {
  CommandEnvelope,
  EventRecord,
  ThreadListCommand,
  WorkspaceId,
} from "../../generated/harness/protocol/atlas-harness";

const workspaceId = "wsp_11111111111111111111111111111111" as WorkspaceId;
const command: ThreadListCommand = {
  kind: "thread.list",
  page: { limit: 10 },
  states: [],
};
const envelope: CommandEnvelope = {
  schema_version: "1.2",
  request_id: "req_33333333333333333333333333333333",
  client_id: "cli_22222222222222222222222222222222",
  workspace_id: workspaceId,
  command,
};
const transport: HarnessTransport = {
  async request(): Promise<unknown> {
    return {
      status: "error",
      request_id: envelope.request_id,
      error_code: "unauthorized",
    };
  },
};
const client = new HarnessClient(transport, {
  workspaceId,
  clientId: envelope.client_id,
});
const ide = new IdeWorkspaceAdapter(workspaceId);
const events = new BoundedEventBuffer<EventRecord>(8);
ide.subscribe(() => undefined);
events.close();
void client.execute(command);
