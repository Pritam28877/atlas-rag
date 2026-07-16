import base64
import json
from datetime import datetime
from uuid import UUID

from app.services.catalog.errors import CatalogValidationError


def encode_cursor(created_at: datetime, identifier: UUID) -> str:
    payload = json.dumps([created_at.isoformat(), str(identifier)]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        padding = "=" * (-len(value) % 4)
        payload = base64.urlsafe_b64decode(value + padding)
        created_at, identifier = json.loads(payload)
        parsed_created_at = datetime.fromisoformat(created_at)
        if parsed_created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include a timezone")
        return parsed_created_at, UUID(identifier)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise CatalogValidationError("pagination cursor is invalid") from error
