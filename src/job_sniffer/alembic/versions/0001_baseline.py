"""Baseline schema for job offers and their evaluations.

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "job_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("salary", sa.String(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.String(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("title_norm", sa.String(), nullable=False),
        sa.Column("company_norm", sa.String(), nullable=False),
        sa.Column("scraped_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "job_offer_evaluations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("offer_id", sa.Integer(), nullable=False),
        sa.Column("fit_score", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("weaknesses", sa.JSON(), nullable=False),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["offer_id"], ["job_offers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("offer_id"),
    )
    op.create_index(
        "ux_job_source_external_id",
        "job_offers",
        ["source", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL AND external_id != ''"),
    )
    op.create_index(
        "ux_job_source_url",
        "job_offers",
        ["source", "url"],
        unique=True,
        sqlite_where=sa.text("url IS NOT NULL AND url != ''"),
    )


def downgrade() -> None:
    op.drop_index("ux_job_source_url", table_name="job_offers")
    op.drop_index("ux_job_source_external_id", table_name="job_offers")
    op.drop_table("job_offer_evaluations")
    op.drop_table("job_offers")
