"""Validated bounded task DAG compilation and deterministic ready waves."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Self

from pydantic import Field, model_validator

from app.services.harness.protocol.base import (
    BoundedLabel,
    PrincipalId,
    Sha256,
    StrictProtocolModel,
    TaskGraphId,
    TaskId,
    WorkspaceId,
)

MAXIMUM_DAG_NODES = 1_024
MAXIMUM_DAG_DEPTH = 16
MAXIMUM_DAG_EDGES = 8_192


class DagNodeSpec(StrictProtocolModel):
    task_id: TaskId
    dependency_task_ids: tuple[TaskId, ...] = Field(max_length=64)
    role: BoundedLabel
    depth: int = Field(ge=0, le=MAXIMUM_DAG_DEPTH)
    priority: int = Field(ge=0, le=1_000)
    task_spec_sha256: Sha256

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if tuple(sorted(set(self.dependency_task_ids))) != self.dependency_task_ids:
            raise ValueError("DAG dependencies must be unique and sorted")
        if self.task_id in self.dependency_task_ids:
            raise ValueError("DAG node cannot depend on itself")
        return self


class TaskGraphDefinition(StrictProtocolModel):
    graph_id: TaskGraphId
    workspace_id: WorkspaceId
    owner_principal_id: PrincipalId
    nodes: tuple[DagNodeSpec, ...] = Field(max_length=MAXIMUM_DAG_NODES)
    graph_sha256: Sha256

    @model_validator(mode="after")
    def validate_graph_hash(self) -> Self:
        if not self.nodes:
            raise ValueError("DAG must contain at least one node")
        node_ids = tuple(node.task_id for node in self.nodes)
        if tuple(sorted(set(node_ids))) != node_ids:
            raise ValueError("DAG node IDs must be unique and sorted")
        edge_count = sum(len(node.dependency_task_ids) for node in self.nodes)
        if edge_count > MAXIMUM_DAG_EDGES:
            raise ValueError("DAG edge count exceeds limit")
        expected = task_graph_sha256(
            graph_id=self.graph_id,
            workspace_id=self.workspace_id,
            owner_principal_id=self.owner_principal_id,
            nodes=self.nodes,
        )
        if self.graph_sha256 != expected:
            raise ValueError("DAG graph hash is invalid")
        _validate_acyclic(self.nodes)
        return self


class DagCompilation(StrictProtocolModel):
    graph: TaskGraphDefinition
    topological_order: tuple[TaskId, ...] = Field(max_length=MAXIMUM_DAG_NODES)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if set(self.topological_order) != {
            node.task_id for node in self.graph.nodes
        }:
            raise ValueError("DAG topological order must contain every node")
        return self


class DagCompileError(ValueError):
    pass


def compile_task_graph(
    *,
    graph_id: TaskGraphId,
    workspace_id: WorkspaceId,
    owner_principal_id: PrincipalId,
    nodes: tuple[DagNodeSpec, ...],
) -> DagCompilation:
    if not nodes or len(nodes) > MAXIMUM_DAG_NODES:
        raise DagCompileError("DAG node count is outside bounds")
    node_ids = {node.task_id for node in nodes}
    if len(node_ids) != len(nodes):
        raise DagCompileError("DAG node IDs must be unique")
    if any(
        dependency not in node_ids
        for node in nodes
        for dependency in node.dependency_task_ids
    ):
        raise DagCompileError("DAG dependency references an unknown node")
    edge_count = sum(len(node.dependency_task_ids) for node in nodes)
    if edge_count > MAXIMUM_DAG_EDGES:
        raise DagCompileError("DAG edge count exceeds limit")
    verified_nodes = tuple(sorted(
        (DagNodeSpec.model_validate(node.model_dump()) for node in nodes),
        key=lambda node: node.task_id,
    ))
    _validate_acyclic(verified_nodes)
    order = _topological_order(verified_nodes)
    graph = TaskGraphDefinition(
        graph_id=graph_id,
        workspace_id=workspace_id,
        owner_principal_id=owner_principal_id,
        nodes=verified_nodes,
        graph_sha256=task_graph_sha256(
            graph_id=graph_id,
            workspace_id=workspace_id,
            owner_principal_id=owner_principal_id,
            nodes=verified_nodes,
        ),
    )
    return DagCompilation(graph=graph, topological_order=order)


def ready_task_ids(
    graph: TaskGraphDefinition,
    *,
    completed: frozenset[TaskId],
    active: frozenset[TaskId] = frozenset(),
) -> tuple[TaskId, ...]:
    available = tuple(
        node
        for node in graph.nodes
        if node.task_id not in completed
        and node.task_id not in active
        and set(node.dependency_task_ids).issubset(completed)
    )
    return tuple(
        node.task_id
        for node in sorted(available, key=lambda node: (-node.priority, node.task_id))
    )


def task_graph_sha256(
    *,
    graph_id: TaskGraphId,
    workspace_id: WorkspaceId,
    owner_principal_id: PrincipalId,
    nodes: tuple[DagNodeSpec, ...],
) -> str:
    values = {
        "graph_id": graph_id,
        "workspace_id": workspace_id,
        "owner_principal_id": owner_principal_id,
        "nodes": [node.model_dump(mode="json") for node in nodes],
    }
    return hashlib.sha256(
        json.dumps(
            values,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _validate_acyclic(nodes: tuple[DagNodeSpec, ...]) -> None:
    indegree = {node.task_id: len(node.dependency_task_ids) for node in nodes}
    dependents: dict[TaskId, list[TaskId]] = {node.task_id: [] for node in nodes}
    for node in nodes:
        for dependency in node.dependency_task_ids:
            dependents[dependency].append(node.task_id)
    queue = deque(
        sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    )
    visited = 0
    while queue:
        task_id = queue.popleft()
        visited += 1
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if visited != len(nodes):
        raise DagCompileError("DAG contains a cycle")


def _topological_order(nodes: tuple[DagNodeSpec, ...]) -> tuple[TaskId, ...]:
    indegree = {node.task_id: len(node.dependency_task_ids) for node in nodes}
    dependents: dict[TaskId, list[TaskId]] = {node.task_id: [] for node in nodes}
    for node in nodes:
        for dependency in node.dependency_task_ids:
            dependents[dependency].append(node.task_id)
    queue = deque(
        sorted(task_id for task_id, degree in indegree.items() if degree == 0)
    )
    order: list[TaskId] = []
    while queue:
        task_id = queue.popleft()
        order.append(task_id)
        for dependent in sorted(dependents[task_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    return tuple(order)
