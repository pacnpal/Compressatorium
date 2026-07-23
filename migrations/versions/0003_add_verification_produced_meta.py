"""add_verification_produced_meta

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-23 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tolerate a column already present. apply_migrations() documents the
    # create_all-then-stamp path as supported: a DB built from the current ORM
    # metadata (which already includes Verification.produced_meta) gets stamped
    # at baseline 0001 and then migrations run forward, so an unconditional
    # ADD COLUMN would raise "duplicate column name" there. Only add it when
    # missing. Nullable with no backfill: existing rows (and manual /info
    # verifies) keep produced_meta NULL, which the re-run fast path treats as
    # "not enough evidence" and skips.
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("verifications")}
    if "produced_meta" in columns:
        return
    op.add_column(
        "verifications",
        sa.Column("produced_meta", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project, forward-only.
    raise NotImplementedError("downgrade not supported")
