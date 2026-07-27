"""Bounded structured capability policy contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from app.services.harness.protocol import (
    Capability,
    DecisionId,
    ExecutionBudget,
    PolicyVersion,
    Sha256,
    StrictProtocolModel,
    TraceLink,
    UtcTimestamp,
)

type PolicyRuleId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
type BoundedPolicyValue = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00\r\n]+$"),
]


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PolicyLayer(StrEnum):
    SYSTEM = "system"
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"
    SESSION = "session"
    USER = "user"


POLICY_LAYER_ORDER = {
    PolicyLayer.SYSTEM: 0,
    PolicyLayer.ORGANIZATION: 1,
    PolicyLayer.WORKSPACE: 2,
    PolicyLayer.SESSION: 3,
    PolicyLayer.USER: 4,
}


class CapabilityKind(StrEnum):
    FILESYSTEM = "filesystem"
    EXECUTABLE = "executable"
    NETWORK = "network"
    SECRET = "secret"
    TOOL = "tool"
    EXTENSION = "extension"
    RESOURCE = "resource"
    CHILD_AGENT = "child_agent"


class FilesystemOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class FilesystemTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.FILESYSTEM] = CapabilityKind.FILESYSTEM
    root_uri: BoundedPolicyValue
    relative_path: BoundedPolicyValue
    operation: FilesystemOperation

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not self.root_uri.startswith("file:///"):
            raise ValueError("filesystem root must be an absolute file URI")
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("filesystem path must be canonical and relative")
        if path.as_posix() != self.relative_path:
            raise ValueError("filesystem path must use canonical POSIX separators")
        return self


class ExecutableTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.EXECUTABLE] = CapabilityKind.EXECUTABLE
    executable: BoundedPolicyValue
    arguments: tuple[BoundedPolicyValue, ...] = Field(max_length=64)
    working_directory: BoundedPolicyValue

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        executable_path = PurePosixPath(self.executable)
        if not executable_path.is_absolute() or executable_path.as_posix() != (
            self.executable
        ):
            raise ValueError("executable must be a canonical absolute POSIX path")
        working_path = PurePosixPath(self.working_directory)
        if working_path.is_absolute() or ".." in working_path.parts:
            raise ValueError("working directory must be relative and traversal-free")
        return self


class NetworkMethod(StrEnum):
    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


class NetworkTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.NETWORK] = CapabilityKind.NETWORK
    scheme: Literal["http", "https"]
    host: BoundedPolicyValue
    port: int = Field(ge=1, le=65_535)
    method: NetworkMethod
    follow_redirects: bool = False

    @model_validator(mode="after")
    def validate_host(self) -> Self:
        if (
            self.host != self.host.lower()
            or "://" in self.host
            or any(character.isspace() for character in self.host)
            or self.host.startswith(".")
            or self.host.endswith(".")
        ):
            raise ValueError("network host must be a canonical lowercase host")
        return self


class SecretTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.SECRET] = CapabilityKind.SECRET
    secret_handle: BoundedPolicyValue
    destination_sha256: Sha256

    @model_validator(mode="after")
    def validate_handle(self) -> Self:
        if not self.secret_handle.startswith("secret:"):
            raise ValueError("secret target must contain an opaque secret handle")
        return self


class ToolTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.TOOL] = CapabilityKind.TOOL
    tool_name: BoundedPolicyValue
    tool_version: BoundedPolicyValue
    operation: BoundedPolicyValue


class ExtensionTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.EXTENSION] = CapabilityKind.EXTENSION
    extension_name: BoundedPolicyValue
    extension_version: BoundedPolicyValue
    operation: BoundedPolicyValue


class ResourceTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.RESOURCE] = CapabilityKind.RESOURCE
    max_processes: int = Field(ge=1, le=1_024)
    max_cpu_ms: int = Field(ge=100, le=3_600_000)
    max_memory_bytes: int = Field(ge=16 * 1024 * 1024, le=64 * 1024**3)
    max_output_bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    max_wall_time_ms: int = Field(ge=100, le=3_600_000)


class ChildAgentTarget(StrictProtocolModel):
    kind: Literal[CapabilityKind.CHILD_AGENT] = CapabilityKind.CHILD_AGENT
    role: BoundedPolicyValue
    maximum_depth: int = Field(ge=1, le=8)
    budget: ExecutionBudget


type CapabilityTarget = Annotated[
    FilesystemTarget
    | ExecutableTarget
    | NetworkTarget
    | SecretTarget
    | ToolTarget
    | ExtensionTarget
    | ResourceTarget
    | ChildAgentTarget,
    Field(discriminator="kind"),
]


class CapabilityLimits(StrictProtocolModel):
    max_duration_ms: int | None = Field(default=None, ge=100, le=3_600_000)
    max_cpu_ms: int | None = Field(default=None, ge=100, le=3_600_000)
    max_memory_bytes: int | None = Field(
        default=None,
        ge=16 * 1024 * 1024,
        le=64 * 1024**3,
    )
    max_output_bytes: int | None = Field(
        default=None,
        ge=0,
        le=16 * 1024 * 1024,
    )
    max_processes: int | None = Field(default=None, ge=1, le=1_024)


class CapabilityProposal(StrictProtocolModel):
    capability: Capability
    target: CapabilityTarget
    target_sha256: Sha256
    sandbox_limits: CapabilityLimits

    @model_validator(mode="after")
    def validate_target_hash(self) -> Self:
        if self.target_sha256 != capability_target_sha256(self.target):
            message = "capability target hash does not match its structured target"
            raise ValueError(message)
        return self


class PolicyRule(StrictProtocolModel):
    rule_id: PolicyRuleId
    capability_kind: CapabilityKind
    capability: Capability
    effect: PolicyEffect
    target_sha256: Sha256 | None = None
    limits: CapabilityLimits = CapabilityLimits()


class PolicyDocument(StrictProtocolModel):
    schema_version: Literal["1.0"] = "1.0"
    layer: PolicyLayer
    policy_version: PolicyVersion
    default_effect: Literal[PolicyEffect.DENY] = PolicyEffect.DENY
    rules: tuple[PolicyRule, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_canonical_document(self) -> Self:
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if tuple(sorted(set(rule_ids))) != rule_ids:
            raise ValueError("policy rules must be unique and sorted by rule ID")
        expected_version = policy_document_version(
            layer=self.layer,
            rules=self.rules,
        )
        if self.policy_version != expected_version:
            raise ValueError("policy version does not match canonical policy content")
        return self


class PolicyBundle(StrictProtocolModel):
    documents: tuple[PolicyDocument, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_layer_order(self) -> Self:
        layer_ranks = tuple(POLICY_LAYER_ORDER[item.layer] for item in self.documents)
        if tuple(sorted(set(layer_ranks))) != layer_ranks:
            raise ValueError("policy documents must use unique canonical layers")
        return self


class EvaluatedPolicyRule(StrictProtocolModel):
    layer: PolicyLayer
    policy_version: PolicyVersion
    rule_id: PolicyRuleId
    effect: PolicyEffect
    target_matched: bool


class LayerPolicyDecision(StrictProtocolModel):
    layer: PolicyLayer
    policy_version: PolicyVersion
    effect: PolicyEffect
    matched_rule_ids: tuple[PolicyRuleId, ...] = Field(max_length=64)
    used_default: bool


class EffectivePolicyDecision(StrictProtocolModel):
    decision_id: DecisionId
    effect: PolicyEffect
    capability: Capability
    capability_kind: CapabilityKind
    request_sha256: Sha256
    policy_bundle_version: PolicyVersion
    layer_decisions: tuple[LayerPolicyDecision, ...] = Field(
        min_length=1,
        max_length=5,
    )
    evaluated_rules: tuple[EvaluatedPolicyRule, ...] = Field(max_length=320)
    effective_limits: CapabilityLimits
    evaluated_at: UtcTimestamp
    trace: TraceLink


def capability_target_sha256(target: CapabilityTarget) -> str:
    return _canonical_sha256(target.model_dump(mode="json"))


def policy_document_version(
    *,
    layer: PolicyLayer,
    rules: tuple[PolicyRule, ...],
) -> str:
    content = {
        "schema_version": "1.0",
        "layer": layer.value,
        "default_effect": PolicyEffect.DENY.value,
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    return f"pol_{_canonical_sha256(content)}"


def policy_bundle_version(bundle: PolicyBundle) -> str:
    content = {
        "documents": [
            {
                "layer": document.layer.value,
                "policy_version": document.policy_version,
            }
            for document in bundle.documents
        ]
    }
    return f"pol_{_canonical_sha256(content)}"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
