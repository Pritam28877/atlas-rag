"""Canonical operation argument and identity tests."""

import pytest
from pydantic import ValidationError

from app.services.harness.protocol.operation_admission import (
    canonical_operation_args_sha256,
)
from tests.harness.operations.fixtures import request


def test_argument_hash_ignores_object_insertion_order() -> None:
    first = {"path": "src/app.py", "flags": ["safe", 1]}
    second = {"flags": ["safe", 1], "path": "src/app.py"}

    assert canonical_operation_args_sha256(first) == (
        canonical_operation_args_sha256(second)
    )


@pytest.mark.parametrize(
    "arguments",
    (
        {"ratio": 1.5},
        {"raw": b"secret"},
        {"nested": [[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]},
    ),
)
def test_noncanonical_or_excessive_arguments_fail(arguments: object) -> None:
    with pytest.raises(ValueError, match="operation argument"):
        canonical_operation_args_sha256(arguments)


def test_operation_id_is_bound_to_attempt_and_argument_hash() -> None:
    original = request()
    with pytest.raises(ValidationError, match="operation ID"):
        original.model_copy(
            update={"attempt": 2},
        ).model_dump()
        type(original).model_validate(
            {**original.model_dump(), "attempt": 2}
        )
