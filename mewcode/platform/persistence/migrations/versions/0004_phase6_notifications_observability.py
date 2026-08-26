"""Add the Phase 6 transactional notification outbox and service heartbeats."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_phase6_notifications_observability"
down_revision = "0003_phase5_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic creates this as VARCHAR(32), while the descriptive Phase 6
    # revision identifier is intentionally longer.
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(32),
        type_=sa.String(128),
        existing_nullable=False,
    )
    connection = op.get_bind()
    existing = connection.scalar(sa.text("SELECT count(*) FROM notification_outbox"))
    if existing:
        raise RuntimeError(
            "Phase 6 migration requires an empty notification_outbox because "
            "historical rows have no trustworthy source event sequence"
        )

    op.drop_constraint(
        "uq_notification_outbox_job_id", "notification_outbox", type_="unique"
    )
    op.add_column(
        "notification_outbox",
        sa.Column("source_event_sequence", sa.BigInteger(), nullable=False),
    )
    op.add_column(
        "notification_outbox", sa.Column("locked_by", sa.String(128), nullable=True)
    )
    op.add_column(
        "notification_outbox",
        sa.Column("fencing_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_notification_outbox_source_destination",
        "notification_outbox",
        ["job_id", "source_event_sequence", "destination"],
    )
    op.create_check_constraint(
        "notification_outbox_status_values",
        "notification_outbox",
        "status IN ('PENDING', 'IN_FLIGHT', 'DELIVERED')",
    )
    op.create_check_constraint(
        "notification_outbox_attempt_count_non_negative",
        "notification_outbox",
        "attempt_count >= 0",
    )
    op.create_index(
        "ix_notification_outbox_pending",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_notification_outbox_lease",
        "notification_outbox",
        ["status", "lease_expires_at"],
    )

    op.add_column(
        "worker_nodes",
        sa.Column(
            "service_type", sa.String(32), server_default="worker", nullable=False
        ),
    )
    op.create_check_constraint(
        "worker_nodes_service_type_values",
        "worker_nodes",
        "service_type IN ('worker', 'notifier')",
    )
    op.create_index(
        "ix_worker_nodes_service_heartbeat",
        "worker_nodes",
        ["service_type", "heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_worker_nodes_service_heartbeat", table_name="worker_nodes")
    op.drop_constraint(
        "worker_nodes_service_type_values", "worker_nodes", type_="check"
    )
    op.drop_column("worker_nodes", "service_type")

    op.drop_index("ix_notification_outbox_lease", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_pending", table_name="notification_outbox")
    op.drop_constraint(
        "notification_outbox_attempt_count_non_negative",
        "notification_outbox",
        type_="check",
    )
    op.drop_constraint(
        "notification_outbox_status_values", "notification_outbox", type_="check"
    )
    op.drop_constraint(
        "uq_notification_outbox_source_destination",
        "notification_outbox",
        type_="unique",
    )
    op.drop_column("notification_outbox", "updated_at")
    op.drop_column("notification_outbox", "delivered_at")
    op.drop_column("notification_outbox", "lease_expires_at")
    op.drop_column("notification_outbox", "fencing_token")
    op.drop_column("notification_outbox", "locked_by")
    op.drop_column("notification_outbox", "source_event_sequence")
    op.create_unique_constraint(
        "uq_notification_outbox_job_id",
        "notification_outbox",
        ["job_id", "event_type", "destination"],
    )
