"""Provider-neutral contracts for bounded cross-adapter conformance."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol import (
    ProviderFailureClass,
    ProviderFinishReason,
    ProviderName,
    ProviderStreamKind,
    ProviderTokenUsage,
    Sha256,
    StrictProtocolModel,
    UtcTimestamp,
)
from app.services.harness.protocol.execution import ToolName

MAXIMUM_CONFORMANCE_ADAPTERS = 16
MAXIMUM_CONFORMANCE_EVENTS = 256
MAXIMUM_CONFORMANCE_SCENARIOS = 16
MAXIMUM_CONFORMANCE_TOOLS = 64


class ConformanceScenario(StrEnum):
    CANCELLATION = "cancellation"
    MALFORMED_STREAM = "malformed_stream"
    OUTPUT_LIMIT = "output_limit"
    POLICY = "policy"
    REASONING_STREAM = "reasoning_stream"
    TEXT_STREAM = "text_stream"
    TOOL_CALLS = "tool_calls"
    USAGE_COST = "usage_cost"


CONFORMANCE_SCENARIOS = tuple(sorted(ConformanceScenario))


class ConformanceCaseStatus(StrEnum):
    FAILED = "failed"
    PASSED = "passed"
    UNSUPPORTED = "unsupported"


class ConformanceFailureCode(StrEnum):
    CONTRACT = "contract"
    EXECUTION = "execution"
    TIMEOUT = "timeout"


class ConformanceComparisonStatus(StrEnum):
    EQUIVALENT = "equivalent"
    INSUFFICIENT = "insufficient"
    MISMATCH = "mismatch"


class ConformanceAdapterDescriptor(StrictProtocolModel):
    provider: ProviderName
    adapter_revision_sha256: Sha256
    supported_scenarios: tuple[ConformanceScenario, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )

    @model_validator(mode="after")
    def validate_supported_scenarios(self) -> Self:
        if (
            tuple(sorted(set(self.supported_scenarios)))
            != self.supported_scenarios
        ):
            raise ValueError(
                "conformance scenarios must be unique and sorted"
            )
        return self


class ConformanceToolSignature(StrictProtocolModel):
    tool_name: ToolName
    arguments_sha256: Sha256


class ConformanceOutcome(StrictProtocolModel):
    event_kinds: tuple[ProviderStreamKind, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_EVENTS,
    )
    text_sha256: Sha256 | None = None
    reasoning_sha256: Sha256 | None = None
    tools: tuple[ConformanceToolSignature, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_TOOLS,
    )
    usage: ProviderTokenUsage | None = None
    failure_class: ProviderFailureClass | None = None
    retry_allowed: bool | None = None
    finish_reason: ProviderFinishReason | None = None
    cancelled: bool
    equivalence_sha256: Sha256

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        terminal_kinds = {
            ProviderStreamKind.CANCELLED,
            ProviderStreamKind.COMPLETED,
            ProviderStreamKind.ERROR,
        }
        if (
            self.event_kinds[-1] not in terminal_kinds
            or sum(kind in terminal_kinds for kind in self.event_kinds) != 1
        ):
            raise ValueError(
                "conformance outcome requires one final terminal event"
            )
        if (self.failure_class is None) != (self.retry_allowed is None):
            raise ValueError("conformance failure fields are incomplete")
        if (self.event_kinds[-1] is ProviderStreamKind.ERROR) != (
            self.failure_class is not None
        ):
            raise ValueError("conformance failure terminal is inconsistent")
        if (self.event_kinds[-1] is ProviderStreamKind.COMPLETED) != (
            self.finish_reason is not None
        ):
            raise ValueError(
                "conformance completion terminal is inconsistent"
            )
        if self.cancelled != (
            self.event_kinds[-1] is ProviderStreamKind.CANCELLED
        ):
            raise ValueError(
                "conformance cancellation terminal is inconsistent"
            )
        if (ProviderStreamKind.USAGE in self.event_kinds) != (
            self.usage is not None
        ):
            raise ValueError("conformance usage evidence is inconsistent")
        if (ProviderStreamKind.TEXT_DELTA in self.event_kinds) != (
            self.text_sha256 is not None
        ):
            raise ValueError("conformance text evidence is inconsistent")
        if (ProviderStreamKind.REASONING_DELTA in self.event_kinds) != (
            self.reasoning_sha256 is not None
        ):
            raise ValueError(
                "conformance reasoning evidence is inconsistent"
            )
        if self.event_kinds.count(ProviderStreamKind.TOOL_CALL) != len(
            self.tools
        ):
            raise ValueError("conformance tool evidence is inconsistent")
        return self


class ConformanceObservation(StrictProtocolModel):
    provider: ProviderName
    adapter_revision_sha256: Sha256
    scenario: ConformanceScenario
    status: ConformanceCaseStatus
    outcome: ConformanceOutcome | None = None
    failure_code: ConformanceFailureCode | None = None

    @model_validator(mode="after")
    def validate_status_evidence(self) -> Self:
        valid = (
            self.status is ConformanceCaseStatus.PASSED
            and self.outcome is not None
            and self.failure_code is None
        ) or (
            self.status is ConformanceCaseStatus.FAILED
            and self.outcome is None
            and self.failure_code is not None
        ) or (
            self.status is ConformanceCaseStatus.UNSUPPORTED
            and self.outcome is None
            and self.failure_code is None
        )
        if not valid:
            raise ValueError(
                "conformance observation evidence is inconsistent"
            )
        return self


class ConformanceComparison(StrictProtocolModel):
    scenario: ConformanceScenario
    status: ConformanceComparisonStatus
    eligible_providers: tuple[ProviderName, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    reference_sha256: Sha256 | None = None
    mismatched_providers: tuple[ProviderName, ...] = Field(
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        for providers in (
            self.eligible_providers,
            self.mismatched_providers,
        ):
            if tuple(sorted(set(providers))) != providers:
                raise ValueError(
                    "conformance comparison providers are not canonical"
                )
        if not set(self.mismatched_providers).issubset(
            self.eligible_providers
        ):
            raise ValueError(
                "conformance mismatches must be eligible providers"
            )
        if self.status is ConformanceComparisonStatus.INSUFFICIENT:
            valid = (
                len(self.eligible_providers) < 2
                and self.reference_sha256 is None
                and not self.mismatched_providers
            )
        elif self.status is ConformanceComparisonStatus.EQUIVALENT:
            valid = (
                len(self.eligible_providers) >= 2
                and self.reference_sha256 is not None
                and not self.mismatched_providers
            )
        else:
            valid = (
                len(self.eligible_providers) >= 2
                and bool(self.mismatched_providers)
            )
        if not valid:
            raise ValueError("conformance comparison is inconsistent")
        return self


class ConformanceReport(StrictProtocolModel):
    scenarios: tuple[ConformanceScenario, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )
    adapters: tuple[ConformanceAdapterDescriptor, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_ADAPTERS,
    )
    observations: tuple[ConformanceObservation, ...] = Field(
        min_length=1,
        max_length=(
            MAXIMUM_CONFORMANCE_ADAPTERS
            * MAXIMUM_CONFORMANCE_SCENARIOS
        ),
    )
    comparisons: tuple[ConformanceComparison, ...] = Field(
        min_length=1,
        max_length=MAXIMUM_CONFORMANCE_SCENARIOS,
    )
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def validate_matrix(self) -> Self:
        if tuple(sorted(set(self.scenarios))) != self.scenarios:
            raise ValueError(
                "conformance report scenarios are not canonical"
            )
        providers = tuple(
            adapter.provider for adapter in self.adapters
        )
        if tuple(sorted(set(providers))) != providers:
            raise ValueError(
                "conformance report adapters are not canonical"
            )
        revisions = {
            adapter.provider: adapter.adapter_revision_sha256
            for adapter in self.adapters
        }
        expected_observations = tuple(
            (scenario, provider)
            for scenario in self.scenarios
            for provider in providers
        )
        actual_observations = tuple(
            (observation.scenario, observation.provider)
            for observation in self.observations
        )
        if (
            actual_observations != expected_observations
            or any(
                revisions.get(observation.provider)
                != observation.adapter_revision_sha256
                for observation in self.observations
            )
        ):
            raise ValueError(
                "conformance report observations are incomplete"
            )
        if tuple(
            comparison.scenario for comparison in self.comparisons
        ) != self.scenarios:
            raise ValueError(
                "conformance report comparisons are incomplete"
            )
        return self
