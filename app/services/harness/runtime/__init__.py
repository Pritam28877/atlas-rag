"""Owned deterministic runtime primitives for the Atlas Harness."""

from app.services.harness.runtime.admission import (
    AdmissionError,
    AdmissionErrorCode,
    AdmissionLease,
    RequestAdmission,
    decode_command,
)
from app.services.harness.runtime.authority import (
    AuthenticatedCommandContext,
    AuthorityDenialReason,
    AuthorityDeniedError,
    AuthorityRepository,
    AuthoritySnapshot,
    CommandAuthorityBinder,
)
from app.services.harness.runtime.framing import (
    FrameDecoder,
    FrameError,
    FrameErrorCode,
    encode_frame,
)
from app.services.harness.runtime.peer_auth import (
    PeerAuthenticationError,
    PeerAuthenticationErrorCode,
    PeerCredentials,
    PeerSessionTokenManager,
    VerifiedPeerSession,
)

__all__ = (
    "AdmissionError",
    "AdmissionErrorCode",
    "AdmissionLease",
    "AuthenticatedCommandContext",
    "AuthorityDeniedError",
    "AuthorityDenialReason",
    "AuthorityRepository",
    "AuthoritySnapshot",
    "CommandAuthorityBinder",
    "FrameDecoder",
    "FrameError",
    "FrameErrorCode",
    "PeerAuthenticationError",
    "PeerAuthenticationErrorCode",
    "PeerCredentials",
    "PeerSessionTokenManager",
    "RequestAdmission",
    "VerifiedPeerSession",
    "decode_command",
    "encode_frame",
)
