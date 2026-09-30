"""registration iban becomes pool account

Revision ID: a23d883e1724
Revises: ece23cf71310
Create Date: 2026-09-30 11:09:35.160816

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models


# revision identifiers, used by Alembic.
revision: str = 'a23d883e1724'
down_revision: Union[str, Sequence[str], None] = 'ece23cf71310'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """آيبان الشركة اللي انكتب وقت التسجيل يصير «الحساب المجمّع» في الحسابات البنكية.

    بيانات فقط: لكل شركة لها آيبان وما لها حساب مجمّع، وما في حساب مسجّل بنفس الآيبان.
    """
    from datetime import datetime, timezone

    conn = op.get_bind()
    companies = conn.execute(sa.text("SELECT id, iban, bank_name FROM entities WHERE iban IS NOT NULL AND iban <> ''"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # UTCDateTime يخزّن UTC بدون منطقة
    for entity_id, iban, bank_name in companies.fetchall():
        taken = conn.execute(
            sa.text("SELECT 1 FROM bank_accounts WHERE entity_id = :e AND (kind = 'pool' OR iban = :i)"),
            {"e": entity_id, "i": iban},
        ).first()
        if taken:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO bank_accounts (entity_id, name, bank_name, iban, kind, is_active, created_at) "
                "VALUES (:e, 'الحساب المجمّع', :b, :i, 'pool', :active, :now)"
            ),
            {"e": entity_id, "b": bank_name or "", "i": iban, "active": True, "now": now},
        )


def downgrade() -> None:
    """ما نحذف حسابات ممكن صارت مستخدمة كوجهات أو مصادر."""
