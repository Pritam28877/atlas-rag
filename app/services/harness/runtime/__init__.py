"""Owned deterministic runtime primitives for the Atlas Harness."""

from app.services.harness.protocol.recovery import (
    LeaseRecoveryState,
    OperationRecoveryAction,
    OperationRecoveryDecision,
    RecoveryLease,
)
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
from app.services.harness.runtime.connection import (
    CommandDispatcher,
    CommandErrorReply,
    CommandReply,
    ConnectionClosedError,
    ConnectionErrorCode,
    ConnectionFatalError,
    FrameSender,
    LocalConnection,
)
from app.services.harness.runtime.framing import (
    FrameDecoder,
    FrameError,
    FrameErrorCode,
    encode_frame,
)
from app.services.harness.runtime.handshake import (
    HandshakeChallenge,
    HandshakeProof,
    PeerCredentialSource,
    UnixChallengeHandshake,
    UnixHandshakeError,
    UnixHandshakeErrorCode,
)
from app.services.harness.runtime.peer_auth import (
    PeerAuthenticationError,
    PeerAuthenticationErrorCode,
    PeerCredentials,
    PeerSessionTokenManager,
    VerifiedPeerSession,
)
from app.services.harness.runtime.provider_router import (
    MAXIMUM_PROVIDER_CANDIDATES,
    ProviderRoutingError,
    ProviderRoutingErrorCode,
    select_provider_route,
)
from app.services.harness.runtime.recovery_classification import (
    classify_operation_recovery,
    expire_recovery_lease,
)
from app.services.harness.runtime.stdio_auth import (
    StdioParentCredentialReader,
    StdioParentIdentityError,
    StdioSessionAuthenticator,
)
from app.services.harness.runtime.streams import (
    AsyncStreamWriter,
    DrainingFrameSender,
    LocalStreamRunner,
)
from app.services.harness.runtime.subscriptions import (
    EventSubscription,
    PublishDisposition,
    ResyncRequired,
    SequencedEvent,
    SubscriptionDelivery,
    SubscriptionError,
    SubscriptionErrorCode,
    SubscriptionSnapshot,
    SubscriptionState,
)
from app.services.harness.runtime.unix_peer import (
    UnixPeerCredentialError,
    UnixPeerCredentialReader,
)
from app.services.harness.runtime.unix_server import (
    LocalConnectionFactory,
    UnixHarnessServer,
    UnixServerSnapshot,
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
    "AsyncStreamWriter",
    "CommandAuthorityBinder",
    "CommandDispatcher",
    "CommandErrorReply",
    "CommandReply",
    "ConnectionClosedError",
    "ConnectionErrorCode",
    "ConnectionFatalError",
    "DrainingFrameSender",
    "EventSubscription",
    "FrameDecoder",
    "FrameError",
    "FrameErrorCode",
    "FrameSender",
    "HandshakeChallenge",
    "HandshakeProof",
    "LocalConnection",
    "LocalConnectionFactory",
    "LocalStreamRunner",
    "LeaseRecoveryState",
    "MAXIMUM_PROVIDER_CANDIDATES",
    "OperationRecoveryAction",
    "OperationRecoveryDecision",
    "PeerAuthenticationError",
    "PeerAuthenticationErrorCode",
    "PeerCredentials",
    "PeerCredentialSource",
    "PeerSessionTokenManager",
    "ProviderRoutingError",
    "ProviderRoutingErrorCode",
    "PublishDisposition",
    "RequestAdmission",
    "RecoveryLease",
    "ResyncRequired",
    "SequencedEvent",
    "SubscriptionDelivery",
    "SubscriptionError",
    "SubscriptionErrorCode",
    "SubscriptionSnapshot",
    "SubscriptionState",
    "StdioParentCredentialReader",
    "StdioParentIdentityError",
    "StdioSessionAuthenticator",
    "UnixPeerCredentialError",
    "UnixPeerCredentialReader",
    "UnixChallengeHandshake",
    "UnixHandshakeError",
    "UnixHandshakeErrorCode",
    "UnixHarnessServer",
    "UnixServerSnapshot",
    "VerifiedPeerSession",
    "decode_command",
    "encode_frame",
    "classify_operation_recovery",
    "expire_recovery_lease",
    "select_provider_route",
)
