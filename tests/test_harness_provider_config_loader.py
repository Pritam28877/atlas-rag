import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.services.harness.providers import (
    ProviderConfigLoadError,
    ProviderConfigLoadErrorCode,
    load_provider_configuration,
)
from app.services.harness.providers.config_loader import (
    MAXIMUM_PROVIDER_CONFIG_BYTES,
)
from tests.harness_provider_config_fixtures import provider_configuration

ConfigMutation = Callable[[dict[str, Any]], None]


def write_configuration(
    path: Path,
    mutation: ConfigMutation | None = None,
) -> bytes:
    payload = provider_configuration().model_dump(mode="json")
    if mutation is not None:
        mutation(payload)
    content = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return content


def unknown_model(payload: dict[str, Any]) -> None:
    payload["routes"][0]["model"] = "unknown-model"


def mismatched_credential_provider(payload: dict[str, Any]) -> None:
    payload["credential_bindings"][0]["provider"] = "other-provider"


def mismatched_destination(payload: dict[str, Any]) -> None:
    payload["credential_bindings"][0]["destination_sha256"] = "a" * 64


def unavailable_region(payload: dict[str, Any]) -> None:
    payload["routes"][0]["region"] = "eu-west-1"


def unknown_price(payload: dict[str, Any]) -> None:
    payload["routes"][0]["price_version_sha256"] = "b" * 64


def unknown_credential(payload: dict[str, Any]) -> None:
    payload["routes"][0]["credential_handle"] = "pcr_" + "a" * 32


def unknown_policy(payload: dict[str, Any]) -> None:
    payload["routes"][0]["policy_revision_sha256"] = "c" * 64


def policy_region_mismatch(payload: dict[str, Any]) -> None:
    payload["data_policies"][0]["allowed_regions"] = ["ap-south-1"]


def duplicate_route(payload: dict[str, Any]) -> None:
    payload["routes"].append(dict(payload["routes"][0]))


def disable_every_route(payload: dict[str, Any]) -> None:
    payload["routes"][0]["enabled"] = False


def add_secret_field(payload: dict[str, Any]) -> None:
    payload["credential_bindings"][0]["api_key"] = "must-not-leak"


def test_loader_returns_strict_configuration_and_raw_digest(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "providers.json"
        content = write_configuration(path)

        loaded = await load_provider_configuration(path)

        assert loaded.configuration == provider_configuration()
        assert loaded.content_sha256 == hashlib.sha256(content).hexdigest()
        assert "api_key" not in loaded.model_dump_json()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        (unknown_model, "unknown model revision"),
        (mismatched_credential_provider, "provider does not match"),
        (mismatched_destination, "destination binding does not match"),
        (unavailable_region, "unavailable model region"),
        (unknown_price, "unknown price revision"),
        (unknown_credential, "unknown credential handle"),
        (unknown_policy, "unknown data policy"),
        (policy_region_mismatch, "violates policy region"),
        (duplicate_route, "provider routes must be unique and sorted"),
        (disable_every_route, "requires an enabled route"),
    ],
)
def test_invalid_cross_references_have_actionable_sanitized_details(
    tmp_path: Path,
    mutation: ConfigMutation,
    detail: str,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "invalid.json"
        write_configuration(path, mutation)

        with pytest.raises(ProviderConfigLoadError) as captured:
            await load_provider_configuration(path)

        assert captured.value.code is ProviderConfigLoadErrorCode.VALIDATION
        assert any(detail in message for message in captured.value.details)
        assert str(captured.value) == (
            "provider configuration could not be loaded"
        )

    asyncio.run(scenario())


def test_unknown_secret_field_is_rejected_without_echoing_value(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "secret-field.json"
        write_configuration(path, add_secret_field)

        with pytest.raises(ProviderConfigLoadError) as captured:
            await load_provider_configuration(path)

        joined_details = " ".join(captured.value.details)
        assert captured.value.code is ProviderConfigLoadErrorCode.VALIDATION
        assert "api_key" in joined_details
        assert "must-not-leak" not in joined_details

    asyncio.run(scenario())


def test_loader_rejects_untrusted_file_shapes_and_sizes(
    tmp_path: Path,
) -> None:
    async def rejected(
        path: Path,
        code: ProviderConfigLoadErrorCode,
    ) -> None:
        with pytest.raises(ProviderConfigLoadError) as captured:
            await load_provider_configuration(path)
        assert captured.value.code is code

    async def scenario() -> None:
        await rejected(
            Path("relative.json"),
            ProviderConfigLoadErrorCode.INVALID_PATH,
        )
        await rejected(
            tmp_path / "missing.json",
            ProviderConfigLoadErrorCode.READ,
        )
        await rejected(tmp_path, ProviderConfigLoadErrorCode.NOT_REGULAR)

        target = tmp_path / "target.json"
        write_configuration(target)
        symlink = tmp_path / "providers-link.json"
        symlink.symlink_to(target)
        await rejected(symlink, ProviderConfigLoadErrorCode.NOT_REGULAR)

        empty = tmp_path / "empty.json"
        empty.write_bytes(b"")
        await rejected(empty, ProviderConfigLoadErrorCode.SIZE)

        oversized = tmp_path / "oversized.json"
        oversized.write_bytes(b"x" * (MAXIMUM_PROVIDER_CONFIG_BYTES + 1))
        await rejected(oversized, ProviderConfigLoadErrorCode.SIZE)

    asyncio.run(scenario())
