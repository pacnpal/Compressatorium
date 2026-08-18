"""add_romm_repin_pending_unique

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'ux_romm_repin_pending_output'


def upgrade() -> None:
    # At most one *pending* re-pin row per output path. Partial, so settled
    # rows stay as history and the same path can be converted again later.
    #
    # Without it, "insert unless a pending row already covers this output" is
    # only a read-then-insert, and a manual submit racing an automation sweep
    # can leave two pending rows for one file -- the second then re-pins
    # whatever the first already settled.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'romm_repin' not in inspector.get_table_names():
        return
    if any(ix['name'] == _INDEX for ix in inspector.get_indexes('romm_repin')):
        # Already present: a DB built straight from the ORM metadata (which
        # declares this index in __table_args__) is stamped at baseline and
        # then run forward through here, exactly as 0004 anticipates.
        return

    # Collapse any duplicates the old read-then-insert let through, keeping the
    # newest row for each output -- it carries the most recent provider ids.
    # A unique index cannot be created over existing violations.
    bind.execute(
        sa.text(
            "UPDATE romm_repin SET state = 'abandoned', "
            "detail = 'Superseded by a newer pending re-pin' "
            "WHERE state = 'pending' AND id NOT IN ("
            "  SELECT MAX(id) FROM romm_repin WHERE state = 'pending' "
            "  GROUP BY output_path"
            ")",
        ),
    )
    op.create_index(
        _INDEX,
        'romm_repin',
        ['output_path'],
        unique=True,
        sqlite_where=sa.text("state = 'pending'"),
    )


def downgrade() -> None:
    # Downgrade migrations are not supported in this project — forward-only.
    raise NotImplementedError("downgrade not supported")
