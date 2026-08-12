"""applied tracking and bot state

Revision ID: 1f7647697c13
Revises: 7b8bf99b2722
Create Date: 2026-08-11 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1f7647697c13"
down_revision: Union[str, Sequence[str], None] = "7b8bf99b2722"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "job_postings",
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("job_postings", sa.Column("applied_at", sa.DateTime(), nullable=True))
    op.create_table(
        "bot_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_update_id", sa.Integer(), nullable=True),
        sa.Column(
            "notifications_paused", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("bot_state")
    op.drop_column("job_postings", "applied_at")
    op.drop_column("job_postings", "applied")
