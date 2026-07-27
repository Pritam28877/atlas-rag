"""Packaged checksum-pinned conformance recording locations."""

from pathlib import Path

CONFORMANCE_FIXTURE_ROOT = Path(__file__).with_name(
    "conformance_fixtures"
)
CONFORMANCE_FIXTURE_PROVIDERS = (
    "bedrock",
    "mock",
    "openai_family",
    "vertex",
)
CONFORMANCE_FIXTURE_FILES = (
    "malformed.jsonl",
    "manifest.json",
    "output_limit.jsonl",
    "policy.jsonl",
    "reasoning.jsonl",
    "text.jsonl",
    "tools.jsonl",
)
CONFORMANCE_FIXTURE_RELATIVE_PATHS = tuple(
    f"{provider}/{filename}"
    for provider in CONFORMANCE_FIXTURE_PROVIDERS
    for filename in CONFORMANCE_FIXTURE_FILES
)
