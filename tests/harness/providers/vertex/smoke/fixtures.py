"""Private Vertex smoke inputs and a regional SSE response."""

import hashlib
import json
import os
from pathlib import Path

from app.cli.harness.adapter_smoke_contracts import (
    AdapterSmokeLaunchRequest,
    authorize_adapter_smoke,
)
from app.cli.harness.provider_smoke_decode import EXPECTED_SMOKE_RESPONSE
from app.services.harness.protocol import DataClassification
from tests.harness.providers.vertex.fixtures import (
    MODEL_ID,
    PROJECT_ID,
    configuration,
    identity,
    route_policy,
)

PRINCIPAL = "runtime@atlas-test-12345.iam.gserviceaccount.com"
PRINCIPAL_SHA256 = hashlib.sha256(PRINCIPAL.encode()).hexdigest()


def authorized_smoke(run_directory: Path):
    configuration_path = run_directory / "providers.json"
    route_path = run_directory / "route.json"
    identity_path = run_directory / "identity.json"
    loaded, _ = configuration()
    public_policy = loaded.configuration.data_policies[0].model_copy(
        update={
            "accepted_classifications": (DataClassification.PUBLIC,),
        }
    )
    smoke_configuration = loaded.configuration.model_copy(
        update={"data_policies": (public_policy,)}
    )
    _write_private(
        configuration_path,
        smoke_configuration.model_dump_json(),
    )
    _write_private(route_path, route_policy().model_dump_json())
    _write_private(
        identity_path,
        identity(
            expected_principal_sha256=PRINCIPAL_SHA256
        ).model_dump_json(),
    )
    return authorize_adapter_smoke(
        AdapterSmokeLaunchRequest(
            acknowledged=True,
            provider="vertex",
            model=MODEL_ID,
            max_output_tokens=32,
            timeout_seconds=10,
            cost_cap_microusd=10_000,
            disposable_project_id=PROJECT_ID,
            gate_environment_variable="ATLAS_VERTEX_SMOKE_ENABLED",
            credential_environment_variable=None,
            configuration_path=configuration_path,
            route_policy_path=route_path,
            identity_path=identity_path,
            capability_evidence_path=None,
            database_path=run_directory / "smoke.sqlite3",
            result_path=run_directory / "result.json",
        )
    )


def completed_sse() -> tuple[bytes, ...]:
    records = (
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": EXPECTED_SMOKE_RESPONSE}],
                        "role": "model",
                    },
                    "index": 0,
                }
            ]
        },
        {
            "candidates": [{"finishReason": "STOP", "index": 0}],
            "usageMetadata": {
                "candidatesTokenCount": 2,
                "promptTokenCount": 3,
                "totalTokenCount": 5,
            },
        },
    )
    return tuple(
        b"data: "
        + json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n\n"
        for record in records
    )


def _write_private(path: Path, content: str) -> None:
    path.write_text(content)
    os.chmod(path, 0o600)
