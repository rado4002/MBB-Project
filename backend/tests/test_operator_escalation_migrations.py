from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

B1_REVISION = "b1e2c3d4e5f6"
B2_REVISION = "b2e2c3d4e5f6"
C3_REVISION = "c3d4e5f6a7b8"
D4_REVISION = "d4e5f6a7b8c9"


def test_b1_b2_c3_are_additive_linear_reversible_migrations() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_current_head() == D4_REVISION
    assert script.get_revision(D4_REVISION).down_revision == C3_REVISION
    assert script.get_revision(C3_REVISION).down_revision == B2_REVISION
    assert script.get_revision(B2_REVISION).down_revision == B1_REVISION
    assert script.get_revision(B1_REVISION).down_revision == "a4b5c6d7e8f9"

    versions = Path("alembic/versions")
    b1 = (
        versions / "b1e2c3d4e5f6_add_operator_escalation_persistence.py"
    ).read_text(encoding="utf-8")
    b2 = (
        versions / "b2e2c3d4e5f6_enforce_one_active_escalation.py"
    ).read_text(encoding="utf-8")
    c3 = (
        versions / "c3d4e5f6a7b8_add_conversation_ownership.py"
    ).read_text(encoding="utf-8")
    assert "op.bulk_insert" not in b1
    assert "UPDATE mbb.escalation_tickets" not in b1
    assert "DELETE FROM mbb.escalation_tickets" not in b2
    assert "UPDATE mbb.escalation_tickets" not in b2
    assert "HAVING count(*) > 1" in b2
    assert "uq_escalation_tickets_one_active_conversation" in b2
    assert "def downgrade()" in b1
    assert "def downgrade()" in b2
    assert "UPDATE mbb.escalation_tickets" not in c3
    assert "DELETE FROM mbb.escalation_tickets" not in c3
    assert "conversation_ownership_idempotency" in c3
    assert "chk_conv_exclusive_owner" in c3
    assert "def downgrade()" in c3
