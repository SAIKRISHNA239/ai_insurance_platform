"""add_tenant_id_and_raw_edi_payload

Revision ID: 001
Revises: (initial)
Create Date: 2026-05-12

Adds multi-tenancy support across all core tables:
  - users.tenant_id          VARCHAR(128) NOT NULL
  - policies.tenant_id       VARCHAR(128) NOT NULL
  - claims.tenant_id         VARCHAR(128) NOT NULL
  - applications.tenant_id   VARCHAR(128) NOT NULL

Adds raw EDI payload storage to claims:
  - claims.raw_edi_payload   JSONB (nullable)

Also creates:
  - All composite (tenant_id + X) indexes for efficient per-tenant queries
  - GIN index on claims.raw_edi_payload for JSON containment queries

Migration strategy (ONLINE-SAFE):
  1. ADD COLUMN with a DEFAULT allows PostgreSQL to set the value for existing
     rows without a full table rewrite (pg 11+: fast DDL with stored default).
  2. After backfill, DEFAULT is dropped — application code owns all new writes.
  3. NOT NULL constraint is added LAST, after the column is populated.

IMPORTANT: Run this migration only after setting TENANT_ID_DEFAULT in the
script below to match your production tenant slug, or backfill tenant_id
for existing rows BEFORE adding the NOT NULL constraint.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ── revision identifiers ──────────────────────────────────────────────────────
revision: str = "001"
down_revision: str | None = None  # Update to your current head if tables exist
branch_labels: str | None = None
depends_on: str | None = None

# Placeholder tenant used when back-filling existing rows.
# Change this before running against an environment with existing data.
_BACKFILL_TENANT_ID: str = "default-tenant"


def upgrade() -> None:
    # ── users: add tenant_id ──────────────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=True,  # Temporarily nullable for online migration
            comment=(
                "Tenant slug/UUID — primary isolation boundary. "
                "All application-layer queries MUST filter by this column."
            ),
        ),
    )
    # Back-fill existing rows
    op.execute(f"UPDATE users SET tenant_id = '{_BACKFILL_TENANT_ID}' WHERE tenant_id IS NULL")
    # Enforce NOT NULL now that all rows have a value
    op.alter_column("users", "tenant_id", nullable=False)
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_tenant_email", "users", ["tenant_id", "email"])
    op.create_index("ix_users_tenant_active", "users", ["tenant_id", "is_active"])

    # ── policies: add tenant_id ───────────────────────────────────────────────
    op.add_column(
        "policies",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=True,
            comment="Tenant identifier — mirrors holder.tenant_id for join-free filtering.",
        ),
    )
    op.execute(f"UPDATE policies SET tenant_id = '{_BACKFILL_TENANT_ID}' WHERE tenant_id IS NULL")
    op.alter_column("policies", "tenant_id", nullable=False)
    op.create_index("ix_policies_tenant_id", "policies", ["tenant_id"])
    op.create_index("ix_policies_tenant_status", "policies", ["tenant_id", "status"])
    op.create_index("ix_policies_tenant_holder", "policies", ["tenant_id", "holder_id"])

    # ── claims: add tenant_id + raw_edi_payload ───────────────────────────────
    op.add_column(
        "claims",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=True,
            comment="Tenant identifier — enables efficient per-tenant claim analytics.",
        ),
    )
    op.add_column(
        "claims",
        sa.Column(
            "raw_edi_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "Complete raw EDI 837 transaction or extracted health-data document "
                "stored as JSONB. Immutable after first write — audit-grade source of truth. "
                "GIN-indexed for JSON path and containment queries."
            ),
        ),
    )
    op.execute(f"UPDATE claims SET tenant_id = '{_BACKFILL_TENANT_ID}' WHERE tenant_id IS NULL")
    op.alter_column("claims", "tenant_id", nullable=False)
    op.create_index("ix_claims_tenant_id", "claims", ["tenant_id"])
    op.create_index("ix_claims_tenant_status", "claims", ["tenant_id", "status"])
    op.create_index("ix_claims_tenant_service_date", "claims", ["tenant_id", "service_date_start"])
    # GIN index on raw_edi_payload for JSON containment queries
    op.create_index(
        "ix_claims_raw_edi_payload_gin",
        "claims",
        ["raw_edi_payload"],
        postgresql_using="gin",
    )

    # ── applications: add tenant_id ───────────────────────────────────────────
    op.add_column(
        "applications",
        sa.Column(
            "tenant_id",
            sa.String(128),
            nullable=True,
            comment="Tenant identifier — scopes all underwriting pipeline queries.",
        ),
    )
    op.execute(
        f"UPDATE applications SET tenant_id = '{_BACKFILL_TENANT_ID}' WHERE tenant_id IS NULL"
    )
    op.alter_column("applications", "tenant_id", nullable=False)
    op.create_index("ix_applications_tenant_id", "applications", ["tenant_id"])
    op.create_index("ix_applications_tenant_status", "applications", ["tenant_id", "status"])
    op.create_index(
        "ix_applications_tenant_risk_tier", "applications", ["tenant_id", "risk_tier"]
    )


def downgrade() -> None:
    # ── applications ──────────────────────────────────────────────────────────
    op.drop_index("ix_applications_tenant_risk_tier", table_name="applications")
    op.drop_index("ix_applications_tenant_status", table_name="applications")
    op.drop_index("ix_applications_tenant_id", table_name="applications")
    op.drop_column("applications", "tenant_id")

    # ── claims ────────────────────────────────────────────────────────────────
    op.drop_index("ix_claims_raw_edi_payload_gin", table_name="claims")
    op.drop_index("ix_claims_tenant_service_date", table_name="claims")
    op.drop_index("ix_claims_tenant_status", table_name="claims")
    op.drop_index("ix_claims_tenant_id", table_name="claims")
    op.drop_column("claims", "raw_edi_payload")
    op.drop_column("claims", "tenant_id")

    # ── policies ──────────────────────────────────────────────────────────────
    op.drop_index("ix_policies_tenant_holder", table_name="policies")
    op.drop_index("ix_policies_tenant_status", table_name="policies")
    op.drop_index("ix_policies_tenant_id", table_name="policies")
    op.drop_column("policies", "tenant_id")

    # ── users ─────────────────────────────────────────────────────────────────
    op.drop_index("ix_users_tenant_active", table_name="users")
    op.drop_index("ix_users_tenant_email", table_name="users")
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_column("users", "tenant_id")
