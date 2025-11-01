from alembic import op
import sqlalchemy as sa

revision = "20251101_init"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "tastings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("category", sa.String(64)),
        sa.Column("aromas", sa.Text()),
        sa.Column("aftertaste", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("tz", sa.String(64), server_default="Europe/Amsterdam"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_table(
        "photos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tasting_id", sa.Integer, sa.ForeignKey("tastings.id", ondelete="CASCADE"), index=True),
        sa.Column("tg_file_id", sa.String(256)),
        sa.Column("s3_key", sa.String(512)),
        sa.Column("filename", sa.String(255)),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )

def downgrade():
    op.drop_table("photos")
    op.drop_table("tastings")
