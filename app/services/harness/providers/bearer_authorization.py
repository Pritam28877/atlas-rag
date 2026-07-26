"""Ephemeral bearer authorization encoding for audited HTTP egress."""

from app.services.harness.providers.credential_material import CredentialLease


class BearerProviderCredentialEncoder:
    def encode(
        self,
        credential: CredentialLease,
    ) -> tuple[bytes, bytearray]:
        authorization = bytearray(b"Bearer ")
        authorization.extend(credential.secret_view())
        return b"authorization", authorization
