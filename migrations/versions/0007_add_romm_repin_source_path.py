"""add_romm_repin_source_path

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-18 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: str | Sequence[str] | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Which local file this row's provider ids were read from.
    #
    # A row is keyed by its *destination*, and recording supersedes the pending
    # row for one -- which is right, since one destination can only be produced
    # once. But it leaves the row unable to say which conversion it belongs to,
    # and two clients can plan different sources onto the same destination: the
    # second supersedes the first's row, and if the FIRST then wins job
    # creation, the second's failed submit finds a queued job writing that path
    # and keeps its row on those grounds. The settle pass then hashes the
    # output the first source produced and applies the second source's ids --
    # the wrong game, identified confidently.
    #
    # With the source recorded, "a job is writing there" becomes "the job
    # writing there is *this* row's conversion", which is the question that was
    # actually being asked.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'romm_repin' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('romm_repin')}
    # Nullable, and left null on existing rows: they predate the column, and
    # a row that cannot name its source is treated as unproven rather than
    # matched -- see `romm_repin.owned_by_job`.
    if 'source_path' not in existing:
        op.add_column(
            'romm_repin', sa.Column('source_path', sa.String(), nullable=True),
        )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
