# OpenAI Responses implementation evidence

Reviewed: 2026-07-27

This is behavior evidence only. Atlas copies no OpenAI implementation or
documentation text. Provider credentials remain opaque handles and all network
traffic must pass through the audited Atlas egress gateway.

## Official sources

- [Streaming API responses](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Streaming events reference](https://platform.openai.com/docs/api-reference/responses-streaming/response/incomplete)
- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
- [Error codes](https://developers.openai.com/api/docs/guides/error-codes)

## Implemented request facts

- Responses streaming is selected explicitly.
- Server-side storage is disabled.
- Automatic truncation is disabled so context overflow fails visibly.
- The output ceiling includes visible and reasoning tokens.
- Prompt-cache evidence may be transmitted as an irreversible cache key.
- State references are rejected until Atlas holds an authorized opaque provider
  response ID; a hash is not substituted for `previous_response_id`.
- Function tools use strict schemas. Every object schema must deny additional
  properties and require every declared property.
- Parallel function calls remain enabled and are independently bounded by the
  canonical tool budget.
- Only inline text messages and text tool results are compiled in this phase.
  Blob and non-text inputs fail closed instead of being silently transformed.

## Supported stream facts

The decoder phase supports text deltas, reasoning-summary deltas, completed
function arguments, terminal usage, completed/incomplete/failed responses, and
top-level error events. It validates bounded event bytes, canonicalizes complete
tool arguments, preserves provider event order, and emits no raw provider error
message. Rate limits and server failures are retryable; authentication, context
length, policy, and malformed events are not.

Lifecycle-only and built-in-tool events are not presented to the canonical turn
engine. Supporting one requires an explicit canonical contract rather than
mislabeling it as assistant output.
