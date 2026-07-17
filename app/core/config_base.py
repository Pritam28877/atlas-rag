from pydantic import BaseModel

MEBIBYTE = 1024 * 1024


class ImmutableSettingsModel(BaseModel):
    """Base model for configuration that cannot change after startup."""

    model_config = {"frozen": True}
