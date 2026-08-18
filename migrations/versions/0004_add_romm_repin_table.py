"""add_romm_repin_table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-17 22:14:37.943022
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: str | Sequence[str] | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tolerate a pre-created table, exactly as 0002 does. apply_migrations()
    # supports the create_all-then-stamp path: a DB built from the current ORM
    # metadata (which already includes RommRepin, and its indexes via
    # __table_args__) is stamped at baseline 0001 and then run forward through
    # this migration. An unconditional CREATE TABLE raises "table romm_repin
    # already exists" there, so only create it when missing.
    bind = op.get_bind()
    if 'romm_repin' in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        'romm_repin',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_rom_id', sa.Integer(), nullable=False),
        sa.Column('source_name', sa.String(), nullable=True),
        sa.Column('output_path', sa.String(), nullable=False),
        sa.Column('output_sha1', sa.String(), nullable=True),
        sa.Column('metadata_ids', sa.JSON(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('settled_at', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # The settle pass selects on state alone ("give me the pending rows"), and
    # done rows accumulate forever since nothing prunes them.
    op.create_index('ix_romm_repin_state', 'romm_repin', ['state'])
    # Queueing a conversion asks "is this output already queued?" before
    # inserting, which is what keeps re-submitting the same batch idempotent.
    op.create_index('ix_romm_repin_output_path', 'romm_repin', ['output_path'])


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
