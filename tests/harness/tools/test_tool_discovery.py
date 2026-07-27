"""Progressive compact catalog and capability-gated materialization tests."""

import pytest

from app.services.harness.tools import (
    ProgressiveToolDiscovery,
    ToolDiscoveryError,
    ToolDiscoveryErrorCode,
    ToolDiscoveryRequest,
    builtin_tool_registry,
)


def test_compact_catalog_omits_schemas_and_materializes_allowed_tool() -> None:
    discovery = ProgressiveToolDiscovery(builtin_tool_registry())
    result = discovery.compact(
        ToolDiscoveryRequest(allowed_capabilities=("filesystem.read",), limit=8)
    )
    assert result.summaries
    assert all(summary.input_schema_sha256 for summary in result.summaries)
    descriptor = discovery.materialize(
        result.summaries[0].name,
        version=result.summaries[0].version,
        allowed_capabilities=(result.summaries[0].capability,),
    )
    assert descriptor.input_schema_json


def test_materialization_cannot_expand_capability() -> None:
    discovery = ProgressiveToolDiscovery(builtin_tool_registry())
    summary = discovery.compact(
        ToolDiscoveryRequest(allowed_capabilities=("filesystem.read",), limit=1)
    ).summaries[0]
    with pytest.raises(ToolDiscoveryError) as failure:
        discovery.materialize(
            summary.name,
            version=summary.version,
            allowed_capabilities=(),
        )
    assert failure.value.code is ToolDiscoveryErrorCode.CAPABILITY_DENIED
