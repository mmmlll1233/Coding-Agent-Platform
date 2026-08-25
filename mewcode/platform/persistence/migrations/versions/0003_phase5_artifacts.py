"""Constrain Phase 5 Artifacts and add retention cleanup indexes."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phase5_artifacts"
down_revision = "0002_phase4_delivery_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "artifact_kind_values",
        "artifacts",
        "kind IN ('agent_log', 'command_log', 'diff', 'verification_report')",
    )
    op.create_check_constraint(
        "artifact_sha256_format",
        "artifacts",
        "sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "artifact_size_non_negative", "artifacts", "size_bytes >= 0"
    )
    op.create_unique_constraint(
        "uq_artifacts_storage_key", "artifacts", ["storage_key"]
    )
    op.create_index("ix_artifacts_expires_at", "artifacts", ["expires_at"])
    op.create_index("ix_jobs_retention_until", "jobs", ["retention_until"])
    op.execute(
        sa.text(
            """
            UPDATE jobs
            SET retention_until = finished_at + INTERVAL '30 days'
            WHERE status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
              AND finished_at IS NOT NULL
              AND retention_until IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_retention_until", table_name="jobs")
    op.drop_index("ix_artifacts_expires_at", table_name="artifacts")
    op.drop_constraint("uq_artifacts_storage_key", "artifacts", type_="unique")
    op.drop_constraint("artifact_size_non_negative", "artifacts", type_="check")
    op.drop_constraint("artifact_sha256_format", "artifacts", type_="check")
    op.drop_constraint("artifact_kind_values", "artifacts", type_="check")
