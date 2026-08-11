"""partial match tracking

Revision ID: 7b8bf99b2722
Revises: c5772edd5b54
Create Date: 2026-08-11 21:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7b8bf99b2722"
down_revision: Union[str, Sequence[str], None] = "c5772edd5b54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("job_postings", sa.Column("missing_qualifications", sa.JSON(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column("partial_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("job_postings", "partial_notified")
    op.drop_column("job_postings", "missing_qualifications")
