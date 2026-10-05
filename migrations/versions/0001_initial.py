"""Initial schema: plans, tenants, subscriptions, usage, idempotency, Stripe dedup,
jobs, notifications, alerts. Seeds the two plans.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    plans = op.create_table(
        "plans",
        sa.Column("code", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("api_calls_limit", sa.BigInteger, nullable=False),
        sa.Column("ai_tokens_limit", sa.BigInteger, nullable=False),
        sa.Column("monthly_fee_cents", sa.Integer, nullable=False),
    )
    op.bulk_insert(plans, [
        {"code": "free", "name": "Free", "api_calls_limit": 1_000,
         "ai_tokens_limit": 100_000, "monthly_fee_cents": 0},
        {"code": "pro", "name": "Pro", "api_calls_limit": 50_000,
         "ai_tokens_limit": 5_000_000, "monthly_fee_cents": 2_900},
    ])

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("plan_code", sa.String(16), sa.ForeignKey("plans.code"), nullable=False),
        sa.Column("subscription_status", sa.String(32), nullable=False),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=True),
        sa.Column("stripe_state_at", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_tenants_stripe_customer", "tenants", ["stripe_customer_id"])
    op.create_index("ix_tenants_stripe_sub", "tenants", ["stripe_subscription_id"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("plan_code", sa.String(16), nullable=False),
        sa.Column("current_period_end", sa.BigInteger, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_subscriptions_tenant", "subscriptions", ["tenant_id"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_pico", sa.BigInteger, nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", "type", name="uq_usage_once"),
    )
    op.create_index("ix_usage_rollup", "usage_events", ["tenant_id", "period", "type"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_code", sa.Integer, nullable=False),
        sa.Column("response_body", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_idem_tenant_key"),
    )

    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("received_at", TS, nullable=False),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("run_after", TS, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_jobs_due", "jobs", ["status", "run_after"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Integer, nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("tenant_id", "metric", "threshold", "period", name="uq_notify_once"),
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )


def downgrade() -> None:
    for t in ["alerts", "notifications", "jobs", "stripe_events", "idempotency_keys",
              "usage_events", "subscriptions", "tenants", "plans"]:
        op.drop_table(t)
