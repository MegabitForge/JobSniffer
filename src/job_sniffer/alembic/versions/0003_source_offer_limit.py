"""Move the offer limit from profiles to their source settings.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("profile_source_settings") as batch_op:
        batch_op.add_column(sa.Column("offer_limit", sa.Integer(), nullable=True))
    # Every source keeps the limit its profile had.
    op.execute(
        "UPDATE profile_source_settings SET offer_limit = "
        "(SELECT offer_limit FROM profiles WHERE profiles.id = profile_source_settings.profile_id)"
    )
    with op.batch_alter_table("profiles") as batch_op:
        batch_op.drop_column("offer_limit")


def downgrade() -> None:
    with op.batch_alter_table("profiles") as batch_op:
        batch_op.add_column(sa.Column("offer_limit", sa.Integer(), nullable=True))
    # A profile limit cannot express different limits per source; keep the largest one.
    op.execute(
        "UPDATE profiles SET offer_limit = (SELECT MAX(offer_limit) FROM profile_source_settings "
        "WHERE profile_source_settings.profile_id = profiles.id)"
    )
    with op.batch_alter_table("profile_source_settings") as batch_op:
        batch_op.drop_column("offer_limit")
