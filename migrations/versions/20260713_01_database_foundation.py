"""Establish the application migration baseline.

Revision ID: 20260713_01
Revises: None
"""

from collections.abc import Sequence

revision: str = "20260713_01"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Reserve the baseline; P3 introduces the first catalog tables."""


def downgrade() -> None:
    """Remove the baseline by allowing Alembic to clear its version row."""
