import {
  HarnessClient,
  type HarnessTransport,
} from "./src/index";
import type { ThreadListCommand, WorkspaceId } from "../../generated/harness/protocol/atlas-harness";

const workspaceId = "wsp_11111111111111111111111111111111" as WorkspaceId;
const command: ThreadListCommand = { kind: "thread.list", states: [], page: { limit: 10 } };
const transport: HarnessTransport = {
  async request(): Promise<unknown> {
    return { status: "error", request_id: null, error_code: "unauthorized" };
  },
};
const client = new HarnessClient(transport, { workspaceId, clientId: "cli_22222222222222222222222222222222" });
void client.execute(command);
