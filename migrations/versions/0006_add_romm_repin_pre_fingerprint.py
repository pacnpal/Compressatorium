"""add_romm_repin_pre_fingerprint

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-18 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ('pre_fingerprint', 'mode')


def upgrade() -> None:
    # Records what was already at the output path when the re-pin row was
    # written, so the settle pass can tell "the conversion produced this" from
    # "this is the file the conversion was going to overwrite".
    #
    # Under the overwrite policy the destination is occupied by definition, so
    # existence alone proves nothing: a batch that was planned and then never
    # submitted would have its row hash the previous artifact and push this
    # ROM's provider ids onto whatever RomM identifies that as.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'romm_repin' not in inspector.get_table_names():
        return
    #
    # `mode` rides along so the settle pass can ask the owning tool what it
    # produced: a split build leaves numbered parts and no bare output, which
    # from the path alone is indistinguishable from a conversion that never ran.
    existing = {col['name'] for col in inspector.get_columns('romm_repin')}
    for column in _COLUMNS:
        # Column-by-column: a DB built straight from the ORM metadata is
        # stamped at baseline and then run forward through here, as 0004
        # anticipates, so some may already be present.
        if column not in existing:
            op.add_column('romm_repin', sa.Column(column, sa.String(), nullable=True))


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
