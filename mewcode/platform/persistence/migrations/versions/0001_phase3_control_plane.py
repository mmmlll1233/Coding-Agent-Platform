"""Create the Phase 3 control-plane schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_phase3_control_plane"
down_revision = None
branch_labels = None
depends_on = None


JOB_VALUES = (
    "'RECEIVED', 'QUEUED', 'RUNNING', 'NEEDS_INPUT', "
    "'CANCEL_REQUESTED', 'SUCCEEDED', 'FAILED', 'CANCELLED'"
)
ATTEMPT_VALUES = (
    "'QUEUED', 'RUNNING', 'COMPLETED', 'NEEDS_INPUT', 'FAILED', 'CANCELLED'"
)
STAGE_VALUES = (
    "'PREPARING', 'ANALYZING', 'IMPLEMENTING', 'VERIFYING', 'PUBLISHING', 'CLEANING_UP'"
)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("name", name="uq_tenants_name"),
    )
    op.create_table(
        "requesters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_requesters_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_requesters"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_requesters_tenant_id"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_prefix", sa.String(64), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["requesters.id"],
            name="fk_api_keys_requester_id_requesters",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
    )
    op.create_index(
        "ix_api_keys_requester_active", "api_keys", ["requester_id", "revoked_at"]
    )
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column(
            "current_attempt_no", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "automatic_retry_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "next_event_sequence", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_owner", sa.String(128), nullable=False),
        sa.Column("repo_name", sa.String(128), nullable=False),
        sa.Column("base_ref", sa.String(255), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column(
            "work_request", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "execution_contract",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "attachment_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("head_branch", sa.String(255), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column(
            "verification_succeeded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({JOB_VALUES})", name="status_values"),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({STAGE_VALUES})", name="stage_values"
        ),
        sa.CheckConstraint(
            "status = 'RECEIVED' OR (base_sha IS NOT NULL AND "
            "base_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$')",
            name="queued_requires_base_sha",
        ),
        sa.CheckConstraint(
            "status <> 'SUCCEEDED' OR "
            "(pr_url IS NOT NULL AND length(btrim(pr_url)) > 0 AND "
            "head_sha IS NOT NULL AND "
            "head_sha ~ '^[0-9a-f]{40}([0-9a-f]{24})?$' AND "
            "verification_succeeded)",
            name="succeeded_requires_delivery",
        ),
        sa.ForeignKeyConstraint(
            ["requester_id"],
            ["requesters.id"],
            name="fk_jobs_requester_id_requesters",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.UniqueConstraint(
            "requester_id", "idempotency_key", name="uq_jobs_requester_id"
        ),
    )
    op.create_index("ix_jobs_queue", "jobs", ["status", "created_at"])
    op.create_index("ix_jobs_tenant_requester", "jobs", ["tenant_id", "requester_id"])
    op.create_table(
        "attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("fencing_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({ATTEMPT_VALUES})", name="status_values"),
        sa.CheckConstraint(
            f"stage IS NULL OR stage IN ({STAGE_VALUES})", name="stage_values"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_attempts_job_id_jobs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attempts"),
        sa.UniqueConstraint("job_id", "attempt_no", name="uq_attempts_job_id"),
    )
    op.create_index("ix_attempts_queue", "attempts", ["status", "queued_at"])
    op.create_index("ix_attempts_lease", "attempts", ["status", "lease_expires_at"])
    op.create_table(
        "job_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "attachment_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_job_inputs_attempt_id_attempts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_job_inputs_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_inputs"),
    )
    op.create_index("ix_job_inputs_job_created", "job_inputs", ["job_id", "created_at"])
    op.create_table(
        "job_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("attempt_sequence", sa.BigInteger(), nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_job_events_attempt_id_attempts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_job_events_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_events"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_job_events_job_id"),
    )
    op.create_index("ix_job_events_job_created", "job_events", ["job_id", "created_at"])
    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
            name="fk_artifacts_attempt_id_attempts",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_artifacts_job_id_jobs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("destination", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(32), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name="fk_notification_outbox_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notification_outbox"),
        sa.UniqueConstraint(
            "job_id", "event_type", "destination", name="uq_notification_outbox_job_id"
        ),
    )
    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_nodes"),
    )


def downgrade() -> None:
    op.drop_table("worker_nodes")
    op.drop_table("notification_outbox")
    op.drop_table("artifacts")
    op.drop_index("ix_job_events_job_created", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_job_inputs_job_created", table_name="job_inputs")
    op.drop_table("job_inputs")
    op.drop_index("ix_attempts_lease", table_name="attempts")
    op.drop_index("ix_attempts_queue", table_name="attempts")
    op.drop_table("attempts")
    op.drop_index("ix_jobs_tenant_requester", table_name="jobs")
    op.drop_index("ix_jobs_queue", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_api_keys_requester_active", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("requesters")
    op.drop_table("tenants")
