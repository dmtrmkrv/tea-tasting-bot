"""Initial schema for tea tasting bot."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20240701_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    op.create_table(
        "tastings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("tasting_date", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index(
        "idx_tastings_user_date",
        "tastings",
        ["user_id", sa.text("tasting_date DESC")],
    )

    bind = op.get_bind()
    dialect = bind.dialect.name if bind else ""
    if dialect == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE INDEX IF NOT EXISTS ix_tastings_user_year
                ON tastings (user_id, (EXTRACT(YEAR FROM tasting_date)))
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                CREATE INDEX IF NOT EXISTS ix_tastings_user_year
                ON tastings (user_id, strftime('%Y', tasting_date))
                """
            )
        )

    op.create_table(
        "photos",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tasting_id", sa.Integer(), sa.ForeignKey("tastings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("backend", sa.String(length=10), nullable=False),
        sa.Column("key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("photos")
    op.drop_index("idx_tastings_user_date", table_name="tastings")
    op.drop_table("tastings")
    op.drop_table("users")
    op.execute("DROP INDEX IF EXISTS ix_tastings_user_year")
