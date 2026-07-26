# Amazon Bedrock ConverseStream contract evidence

Reviewed: 2026-07-27

Treatment: behavior specification only. No AWS SDK or documentation source is
copied into the harness.

## Selected native API

Atlas uses the regional `bedrock-runtime` `ConverseStream` operation for the
native Bedrock adapter. AWS documents Converse as the uniform message interface
for models that support messages, while model and region availability still
require capability evidence.

## Request evidence

- `modelId` selects a foundation model, inference profile, provisioned model, or
  other supported model resource.
- `messages` contain ordered `user` and `assistant` content blocks.
- `system` carries system instruction content separately from messages.
- `inferenceConfig.maxTokens` provides the output ceiling.
- client-side tools use `toolConfig.tools[].toolSpec`, including name,
  description, JSON input schema, and the current optional strict-schema flag.
- tool results return the provider `toolUseId`.

Atlas therefore rejects unsupported modalities, non-AWS-compatible tool names,
and system/developer messages that appear after conversation content.

## Streaming evidence

AWS documents this order:

1. one `messageStart`;
2. indexed content blocks, each with start/delta/stop events;
3. one `messageStop` carrying `stopReason`;
4. one `metadata` event carrying token usage and latency.

Text and reasoning deltas can be emitted immediately. Tool input is partial JSON
and must be bounded until its matching block stops. Reasoning signatures and
guardrail/service-tier metadata are provider-specific evidence and must not be
silently treated as text.

The event stream can also contain modeled validation, throttling, service
unavailable, internal-server, and model-stream errors. Atlas maps these to
redacted canonical error classes and never emits the provider message.

## Sources

- https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/tool-use.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html
- locally installed locked Botocore `bedrock-runtime` service model, version
  `1.43.46`, inspected only as an official SDK schema
