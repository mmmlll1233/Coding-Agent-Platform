"""Require complete Phase 4 Delivery evidence for successful Jobs."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_phase4_delivery_evidence"
down_revision = "0001_phase3_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM jobs
                WHERE status = 'SUCCEEDED' AND (
                  pr_number IS NULL OR pr_number <= 0 OR
                  NOT (CASE WHEN pr_url IS NOT NULL AND
                    pr_url ~ '^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$'
                    THEN split_part(pr_url, '/', 7)::bigint = pr_number ELSE false END) OR
                  head_branch IS NULL OR
                  head_branch !~ '^mewcode/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' OR
                  head_sha IS NULL OR
                  head_sha !~ '^[0-9a-f]{40}([0-9a-f]{24})?$' OR
                  verification_succeeded IS DISTINCT FROM true
                )
              ) THEN
                RAISE EXCEPTION 'Phase 4 migration found incomplete successful Delivery';
              END IF;
            END $$;
            """
        )
    )
    op.drop_constraint("succeeded_requires_delivery", "jobs", type_="check")
    op.create_check_constraint(
        "succeeded_requires_delivery",
        "jobs",
        "status <> 'SUCCEEDED' OR ("
        "pr_number IS NOT NULL AND pr_number > 0 AND "
        "CASE WHEN pr_url IS NOT NULL AND "
        "pr_url ~ '^https://github[.]com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$' "
        "THEN split_part(pr_url, '/', 7)::bigint = pr_number ELSE false END AND "
        "head_branch IS NOT NULL AND "
        "head_branch ~ '^mewcode/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND "
        "head_sha IS NOT NULL AND "
        "head_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$' AND "
        "verification_succeeded)",
    )


def downgrade() -> None:
    op.drop_constraint("succeeded_requires_delivery", "jobs", type_="check")
    op.create_check_constraint(
        "succeeded_requires_delivery",
        "jobs",
        "status <> 'SUCCEEDED' OR ("
        "pr_url IS NOT NULL AND length(btrim(pr_url)) > 0 AND "
        "head_sha IS NOT NULL AND "
        "head_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$' AND "
        "verification_succeeded)",
    )
