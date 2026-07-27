"""Bounded graph compiler and ready-wave tests."""

import hashlib

import pytest

from app.services.harness.scheduler import (
    DagCompileError,
    DagNodeSpec,
    compile_task_graph,
    ready_task_ids,
)

GRAPH = "tgr_" + "1" * 32
WORKSPACE = "wsp_" + "2" * 32
PRINCIPAL = "prn_" + "3" * 32


def _node(
    task_id: str,
    dependencies: tuple[str, ...] = (),
    priority: int = 1,
) -> DagNodeSpec:
    return DagNodeSpec(
        task_id=task_id,
        dependency_task_ids=dependencies,
        role="worker",
        depth=len(dependencies),
        priority=priority,
        task_spec_sha256=hashlib.sha256(task_id.encode()).hexdigest(),
    )


def test_graph_compilation_is_deterministic_and_ready_wave_is_bounded() -> None:
    nodes = (
        _node("tsk_" + "2" * 32, ("tsk_" + "1" * 32,), priority=2),
        _node("tsk_" + "1" * 32, priority=1),
        _node("tsk_" + "3" * 32, ("tsk_" + "1" * 32,), priority=3),
    )
    first = compile_task_graph(
        graph_id=GRAPH,
        workspace_id=WORKSPACE,
        owner_principal_id=PRINCIPAL,
        nodes=nodes,
    )
    second = compile_task_graph(
        graph_id=GRAPH,
        workspace_id=WORKSPACE,
        owner_principal_id=PRINCIPAL,
        nodes=tuple(reversed(nodes)),
    )
    assert first.graph.graph_sha256 == second.graph.graph_sha256
    assert first.topological_order == (
        "tsk_" + "1" * 32,
        "tsk_" + "2" * 32,
        "tsk_" + "3" * 32,
    )
    assert ready_task_ids(first.graph, completed=frozenset()) == (
        "tsk_" + "1" * 32,
    )
    assert ready_task_ids(
        first.graph,
        completed=frozenset(("tsk_" + "1" * 32,)),
    ) == ("tsk_" + "3" * 32, "tsk_" + "2" * 32)


def test_graph_cycle_and_unknown_dependency_fail_closed() -> None:
    with pytest.raises(DagCompileError):
        compile_task_graph(
            graph_id=GRAPH,
            workspace_id=WORKSPACE,
            owner_principal_id=PRINCIPAL,
            nodes=(
                _node("tsk_" + "1" * 32, ("tsk_" + "2" * 32,)),
                _node("tsk_" + "2" * 32, ("tsk_" + "1" * 32,)),
            ),
        )
    with pytest.raises(DagCompileError):
        compile_task_graph(
            graph_id=GRAPH,
            workspace_id=WORKSPACE,
            owner_principal_id=PRINCIPAL,
            nodes=(_node("tsk_" + "1" * 32, ("tsk_" + "9" * 32,)),),
        )
