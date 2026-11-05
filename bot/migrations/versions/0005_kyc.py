"""KYC: verified bank cards, admin review messages, card used for a top-up

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

T = sa.String(20)


def upgrade() -> None:
    op.create_table(
        "kyc_cards",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("card_number", sa.String(16), nullable=False),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("photo_id", sa.String(256)),
        sa.Column("photo_unique_id", sa.String(128)),
        sa.Column("photo_mode", sa.String(16), nullable=False, server_default="note"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(256)),
        sa.Column("admin_id", sa.BigInteger),
        sa.Column("created_at", T, nullable=False),
        sa.Column("decided_at", T),
    )
    op.create_index("idx_kyc_user", "kyc_cards", ["user_id"])
    op.create_index("idx_kyc_status", "kyc_cards", ["status"])
    op.create_index("idx_kyc_card", "kyc_cards", ["card_number"])
    op.create_table(
        "kyc_messages",
        sa.Column("kyc_id", sa.Integer, nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
    )
    op.create_index("idx_kyc_messages_kyc", "kyc_messages", ["kyc_id"])
    with op.batch_alter_table("topups") as b:
        b.add_column(sa.Column("card_id", sa.Integer))


def downgrade() -> None:
    with op.batch_alter_table("topups") as b:
        b.drop_column("card_id")
    op.drop_index("idx_kyc_messages_kyc", table_name="kyc_messages")
    op.drop_table("kyc_messages")
    op.drop_index("idx_kyc_card", table_name="kyc_cards")
    op.drop_index("idx_kyc_status", table_name="kyc_cards")
    op.drop_index("idx_kyc_user", table_name="kyc_cards")
    op.drop_table("kyc_cards")
