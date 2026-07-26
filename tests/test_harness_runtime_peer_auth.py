from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from app.services.harness.protocol import AuthenticationMethod
from app.services.harness.runtime import (
    PeerAuthenticationError,
    PeerAuthenticationErrorCode,
    PeerCredentials,
    PeerSessionTokenManager,
)


@dataclass
class MutableClock:
    value: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value


class DeterministicEntropy:
    def __init__(self) -> None:
        self._counter = 0

    def __call__(self, size: int) -> bytes:
        self._counter += 1
        return self._counter.to_bytes(size, byteorder="big")


def peer(**overrides: object) -> PeerCredentials:
    values: dict[str, object] = {
        "user_id": 1000,
        "group_id": 1000,
        "process_id": 4242,
        "process_start_ticks": 99_000,
        "executable_sha256": "0" * 64,
    }
    values.update(overrides)
    return PeerCredentials.model_validate(values)


def manager(
    clock: MutableClock,
    *,
    maximum_replay_entries: int = 16,
) -> PeerSessionTokenManager:
    return PeerSessionTokenManager(
        b"k" * 32,
        token_ttl_seconds=30,
        maximum_replay_entries=maximum_replay_entries,
        clock=clock,
        entropy=DeterministicEntropy(),
    )


def test_verified_session_derives_principal_only_from_peer_and_token() -> None:
    clock = MutableClock()
    token_manager = manager(clock)
    credentials = peer()
    token = token_manager.issue(credentials)

    session = token_manager.verify_and_consume(token, credentials)

    assert (
        session.principal.authentication_method
        is AuthenticationMethod.PEER_CREDENTIALS
    )
    assert session.principal.principal_id.startswith("prn_")
    assert session.principal.subject_sha256 == credentials.binding_sha256()
    assert session.principal.session_binding_sha256 != credentials.binding_sha256()
    assert session.expires_at > session.principal.authenticated_at


@pytest.mark.parametrize(
    "changed_peer",
    (
        peer(user_id=1001),
        peer(process_id=4243),
        peer(process_start_ticks=99_001),
        peer(executable_sha256="1" * 64),
    ),
)
def test_token_is_bound_to_complete_peer_process_evidence(
    changed_peer: PeerCredentials,
) -> None:
    clock = MutableClock()
    token_manager = manager(clock)
    token = token_manager.issue(peer())

    with pytest.raises(PeerAuthenticationError) as error:
        token_manager.verify_and_consume(token, changed_peer)

    assert error.value.code is PeerAuthenticationErrorCode.PEER_MISMATCH


def test_tampered_and_expired_tokens_fail_closed() -> None:
    clock = MutableClock()
    token_manager = manager(clock)
    credentials = peer()
    token = token_manager.issue(credentials)
    tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(PeerAuthenticationError) as invalid:
        token_manager.verify_and_consume(tampered, credentials)
    assert invalid.value.code is PeerAuthenticationErrorCode.INVALID_TOKEN

    clock.value += 30
    with pytest.raises(PeerAuthenticationError) as expired:
        token_manager.verify_and_consume(token, credentials)
    assert expired.value.code is PeerAuthenticationErrorCode.EXPIRED_TOKEN


def test_token_can_be_consumed_exactly_once() -> None:
    clock = MutableClock()
    token_manager = manager(clock)
    credentials = peer()
    token = token_manager.issue(credentials)

    token_manager.verify_and_consume(token, credentials)
    with pytest.raises(PeerAuthenticationError) as replayed:
        token_manager.verify_and_consume(token, credentials)

    assert replayed.value.code is PeerAuthenticationErrorCode.REPLAYED_TOKEN


def test_concurrent_replay_race_has_exactly_one_winner() -> None:
    clock = MutableClock()
    token_manager = manager(clock)
    credentials = peer()
    token = token_manager.issue(credentials)

    def verify() -> PeerAuthenticationErrorCode | None:
        try:
            token_manager.verify_and_consume(token, credentials)
        except PeerAuthenticationError as error:
            return error.code
        return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(executor.map(lambda _: verify(), range(8)))

    assert outcomes.count(None) == 1
    assert outcomes.count(PeerAuthenticationErrorCode.REPLAYED_TOKEN) == 7


def test_replay_ledger_fails_closed_then_reclaims_expired_entries() -> None:
    clock = MutableClock()
    token_manager = manager(clock, maximum_replay_entries=1)
    credentials = peer()
    first = token_manager.issue(credentials)
    second = token_manager.issue(credentials)
    token_manager.verify_and_consume(first, credentials)

    with pytest.raises(PeerAuthenticationError) as full:
        token_manager.verify_and_consume(second, credentials)
    assert full.value.code is PeerAuthenticationErrorCode.REPLAY_CAPACITY

    clock.value += 31
    replacement = token_manager.issue(credentials)
    token_manager.verify_and_consume(replacement, credentials)


def test_configuration_and_entropy_bounds_are_enforced() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        PeerSessionTokenManager(b"short")

    token_manager = PeerSessionTokenManager(
        b"k" * 32,
        entropy=lambda _: b"short",
    )
    with pytest.raises(RuntimeError, match="entropy"):
        token_manager.issue(peer())
