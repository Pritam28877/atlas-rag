import type {
  ArtifactFetchCommand,
  Command,
  CommandEnvelope,
  CommandResponseKind,
  PageInfo,
  SchemaVersion,
  WorkspaceId,
} from "../../../generated/harness/protocol/atlas-harness";

export const CURRENT_SCHEMA_VERSION: SchemaVersion = "1.2";
export const MAXIMUM_RESPONSE_BYTES = 4 * 1024 * 1024;
export const MAXIMUM_RETRY_ATTEMPTS = 3;
const COMMAND_RESPONSE_KINDS: readonly CommandResponseKind[] = [
  "workspace_status",
  "thread_status",
  "thread_page",
  "turn_status",
  "approval_status",
  "task_status",
  "capability_page",
  "event_subscription",
  "event_acknowledgement",
  "artifact_range",
  "evaluation_status",
];

export interface HarnessSuccessResponse {
  readonly status: "success";
  readonly request_id: string;
  readonly response_kind: CommandResponseKind;
  readonly payload: unknown;
}

export interface HarnessErrorResponse {
  readonly status: "error";
  readonly request_id: string | null;
  readonly error_code: string;
}

export type HarnessResponse = HarnessSuccessResponse | HarnessErrorResponse;

export interface HarnessTransport {
  request(body: string, signal: AbortSignal): Promise<unknown>;
}

export interface HarnessClientOptions {
  readonly workspaceId: WorkspaceId;
  readonly clientId: string;
  readonly schemaVersion?: SchemaVersion;
  readonly maxAttempts?: number;
}

export class HarnessProtocolError extends Error {
  readonly code: "invalid_response" | "transport" | "aborted";

  constructor(
    code: "invalid_response" | "transport" | "aborted",
    message: string,
  ) {
    super(message);
    this.name = "HarnessProtocolError";
    this.code = code;
  }
}

export class HarnessClient {
  private readonly workspaceId: WorkspaceId;
  private readonly clientId: string;
  private readonly schemaVersion: SchemaVersion;
  private readonly maxAttempts: number;

  constructor(
    private readonly transport: HarnessTransport,
    options: HarnessClientOptions,
  ) {
    const maxAttempts = options.maxAttempts ?? 1;
    if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > MAXIMUM_RETRY_ATTEMPTS) {
      throw new RangeError("maxAttempts is outside the SDK bound");
    }
    this.workspaceId = options.workspaceId;
    this.clientId = options.clientId;
    this.schemaVersion = options.schemaVersion ?? CURRENT_SCHEMA_VERSION;
    this.maxAttempts = maxAttempts;
  }

  async execute(
    command: Command,
    options: { readonly expectedSequence?: number; readonly signal?: AbortSignal } = {},
  ): Promise<HarnessResponse> {
    const envelope: CommandEnvelope = {
      schema_version: this.schemaVersion,
      request_id: randomIdentifier("req"),
      client_id: this.clientId,
      workspace_id: this.workspaceId,
      ...(options.expectedSequence === undefined
        ? {}
        : { expected_sequence: options.expectedSequence }),
      command,
    };
    const body = JSON.stringify(envelope);
    let lastError: unknown;
    for (let attempt = 1; attempt <= this.maxAttempts; attempt += 1) {
      try {
        const rawResponse = await this.transport.request(body, options.signal ?? new AbortController().signal);
        return parseResponse(rawResponse);
      } catch (error) {
        if (options.signal?.aborted) {
          throw new HarnessProtocolError("aborted", "request was aborted");
        }
        lastError = error;
        if (attempt === this.maxAttempts) {
          break;
        }
      }
    }
    if (lastError instanceof HarnessProtocolError) {
      throw lastError;
    }
    throw new HarnessProtocolError("transport", "harness transport failed");
  }

  async fetchArtifact(
    command: ArtifactFetchCommand,
    signal?: AbortSignal,
  ): Promise<HarnessResponse> {
    return this.execute(
      command,
      signal === undefined ? {} : { signal },
    );
  }
}

export class FetchHarnessTransport implements HarnessTransport {
  private readonly endpoint: URL;

  constructor(
    endpoint: string,
    private readonly authorizationToken?: string,
    private readonly fetchImplementation: typeof fetch = fetch,
  ) {
    const parsed = new URL(endpoint);
    if (parsed.protocol !== "https:" && parsed.hostname !== "127.0.0.1" && parsed.hostname !== "localhost") {
      throw new TypeError("harness endpoint must use HTTPS or loopback");
    }
    this.endpoint = parsed;
  }

  async request(body: string, signal: AbortSignal): Promise<unknown> {
    const headers = new Headers({ "content-type": "application/json" });
    if (this.authorizationToken !== undefined) {
      headers.set("authorization", `Bearer ${this.authorizationToken}`);
    }
    const response = await this.fetchImplementation(this.endpoint, {
      method: "POST",
      headers,
      body,
      signal,
    });
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > MAXIMUM_RESPONSE_BYTES) {
      throw new HarnessProtocolError("transport", "response exceeds SDK bound");
    }
    const text = new TextDecoder().decode(bytes);
    if (!response.ok) {
      throw new HarnessProtocolError("transport", "harness endpoint rejected request");
    }
    try {
      return JSON.parse(text) as unknown;
    } catch (error) {
      throw new HarnessProtocolError("invalid_response", "response is not JSON");
    }
  }
}

export class BoundedEventBuffer<T> {
  private readonly values: T[] = [];
  private closed = false;
  private waiter: ((value: T | undefined) => void) | undefined;

  constructor(private readonly capacity: number = 2048) {
    if (!Number.isInteger(capacity) || capacity < 1 || capacity > 2048) {
      throw new RangeError("event buffer capacity is outside the SDK bound");
    }
  }

  push(value: T): boolean {
    if (this.closed) {
      return false;
    }
    if (this.waiter !== undefined) {
      const waiter = this.waiter;
      this.waiter = undefined;
      waiter(value);
      return true;
    }
    if (this.values.length >= this.capacity) {
      return false;
    }
    this.values.push(value);
    return true;
  }

  async take(): Promise<T | undefined> {
    if (this.values.length > 0) {
      return this.values.shift();
    }
    if (this.closed) {
      return undefined;
    }
    return new Promise<T | undefined>((resolve) => {
      this.waiter = resolve;
    });
  }

  close(): void {
    this.closed = true;
    const waiter = this.waiter;
    this.waiter = undefined;
    waiter?.(undefined);
    this.values.length = 0;
  }
}

export function readPage<T>(
  payload: unknown,
  decode: (value: unknown) => T,
): { readonly items: readonly T[]; readonly page: PageInfo } {
  if (!isRecord(payload) || !Array.isArray(payload.items) || !isRecord(payload.page)) {
    throw new HarnessProtocolError("invalid_response", "page response is malformed");
  }
  const pageValue = payload.page;
  if (
    typeof pageValue.has_more !== "boolean" ||
    (pageValue.has_more && typeof pageValue.next_cursor !== "string") ||
    (!pageValue.has_more && pageValue.next_cursor !== undefined)
  ) {
    throw new HarnessProtocolError("invalid_response", "page metadata is malformed");
  }
  const page: PageInfo = {
    has_more: pageValue.has_more,
    ...(typeof pageValue.next_cursor === "string" ? { next_cursor: pageValue.next_cursor } : {}),
  };
  return { items: payload.items.map(decode), page };
}

export function parseResponse(value: unknown): HarnessResponse {
  if (!isRecord(value) || (value.status !== "success" && value.status !== "error")) {
    throw new HarnessProtocolError("invalid_response", "response envelope is malformed");
  }
  if (value.status === "success") {
    if (
      typeof value.request_id !== "string" ||
      typeof value.response_kind !== "string" ||
      !COMMAND_RESPONSE_KINDS.includes(value.response_kind as CommandResponseKind) ||
      !("payload" in value)
    ) {
      throw new HarnessProtocolError("invalid_response", "success response is malformed");
    }
    return {
      status: "success",
      request_id: value.request_id,
      response_kind: value.response_kind as CommandResponseKind,
      payload: value.payload,
    };
  }
  if ((value.request_id !== null && typeof value.request_id !== "string") || typeof value.error_code !== "string") {
    throw new HarnessProtocolError("invalid_response", "error response is malformed");
  }
  return { status: "error", request_id: value.request_id, error_code: value.error_code };
}

function randomIdentifier(prefix: string): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `${prefix}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
