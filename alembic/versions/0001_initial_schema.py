"""initial schema

The complete DANAH schema (master prompt §6): 17 tables, pgvector HNSW indexes for
similarity search, Postgres FTS GIN indexes for the hybrid retriever's keyword arm, and the
trigger that makes `audit_log` genuinely append-only.

Derived from the SQLAlchemy models via `alembic revision --autogenerate`, then finalised by
hand for the four things autogenerate cannot know about:

  1. `CREATE EXTENSION vector / pgcrypto`.
  2. Enum types are created **once**, up front. Several enums (`classification`, `role`,
     `language`, `agent_name`, `publication_status`) are reused across tables; letting each
     `CREATE TABLE` emit its own `CREATE TYPE` would fail with `duplicate_object` on the
     second table that uses one.
  3. HNSW (vector) and GIN (tsvector) indexes, which are raw DDL.
  4. The append-only trigger on `audit_log`.

The `vector(n)` dimension is read from `EMBEDDING_DIM` so the column always matches the
configured embedding model (master prompt §6).

Revision ID: 0001
Revises:
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM: int = get_settings().embedding_dim

# ---------------------------------------------------------------------------
# Enum types — created once, referenced everywhere.
# ---------------------------------------------------------------------------
ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "role": ("admin", "executive", "analyst", "viewer"),
    "language": ("en", "ar"),
    "classification": ("PUBLIC", "INTERNAL", "OFFICIAL", "OFFICIAL_SENSITIVE"),
    "document_status": ("pending", "processing", "indexed", "failed"),
    "source_type": ("api", "rss", "webhook", "manual"),
    "connector_kind": ("worldbank", "gdelt", "rss", "reliefweb", "custom"),
    "item_status": ("new", "triaged", "analyzed", "archived"),
    "pipeline_trigger": ("manual", "scheduled"),
    "run_status": ("running", "completed", "failed", "partial"),
    "step_status": ("running", "completed", "failed", "skipped"),
    "agent_name": ("signal", "risk", "opportunity", "policy", "briefing", "memory"),
    "insight_kind": ("risk", "opportunity", "policy"),
    "publication_status": ("draft", "pending_approval", "published", "rejected"),
    "approval_subject": ("insight", "briefing"),
    "approval_status": ("pending", "approved", "rejected", "changes_requested"),
    "memory_kind": ("decision", "lesson", "context"),
    "chat_role": ("user", "assistant"),
    "actor_type": ("user", "agent", "system"),
    "usage_purpose": ("chat", "agent", "embedding"),
    "notification_kind": (
        "approval_pending",
        "briefing_published",
        "cost_alert",
        "source_failure",
    ),
}


def enum(name: str) -> postgresql.ENUM:
    """Reference an already-created enum type (never emits CREATE TYPE)."""
    return postgresql.ENUM(*ENUM_TYPES[name], name=name, create_type=False)


# ---------------------------------------------------------------------------
# `audit_log` is append-only. The application cannot rewrite history even with a bug
# or a compromised service account — only a superuser disabling the trigger can, and
# that is precisely what `GET /api/audit/verify` is designed to detect.
# ---------------------------------------------------------------------------
AUDIT_GUARD_FN = """
CREATE OR REPLACE FUNCTION danah_audit_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege',
              HINT = 'Audit entries are immutable by design (DANAH architecture §8).';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()

    # -- Extensions ---------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -- Enum types (once) --------------------------------------------------
    for name, values in ENUM_TYPES.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    # -- users --------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", enum("role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    # -- refresh_tokens -----------------------------------------------------
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
    )
    op.create_index(op.f("ix_refresh_tokens_token_hash"), "refresh_tokens", ["token_hash"], unique=True)
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)

    # -- documents ----------------------------------------------------------
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=200), nullable=False),
        sa.Column("storage_path", sa.String(length=1000), nullable=False),
        sa.Column("language", enum("language"), nullable=False),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column("status", enum("document_status"), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"],
            name=op.f("fk_documents_uploaded_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_classification"), "documents", ["classification"], unique=False)
    op.create_index(op.f("ix_documents_status"), "documents", ["status"], unique=False)
    op.create_index(op.f("ix_documents_uploaded_by"), "documents", ["uploaded_by"], unique=False)

    # -- document_chunks ----------------------------------------------------
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column("language", enum("language"), nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            # 'simple' rather than 'english': the corpus is bilingual, and an English
            # stemmer would mangle Arabic tokens.
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_index"),
    )
    op.create_index(op.f("ix_document_chunks_classification"), "document_chunks", ["classification"], unique=False)
    op.create_index(op.f("ix_document_chunks_document_id"), "document_chunks", ["document_id"], unique=False)

    # -- sources ------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("type", enum("source_type"), nullable=False),
        sa.Column("connector", enum("connector_kind"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("credibility_score", sa.Float(), server_default="0.7", nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=500), nullable=True),
        sa.Column("hmac_secret", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "credibility_score >= 0 AND credibility_score <= 1",
            name=op.f("ck_sources_credibility_score_range"),
        ),
        sa.CheckConstraint("poll_interval_minutes > 0", name=op.f("ck_sources_poll_interval_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("name", name=op.f("uq_sources_name")),
    )

    # -- ingested_items -----------------------------------------------------
    op.create_table(
        "ingested_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=500), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", enum("language"), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("triage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column("status", enum("item_status"), nullable=False),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column(
            "content_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(summary,'') "
                "|| ' ' || coalesce(content,''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"],
            name=op.f("fk_ingested_items_source_id_sources"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingested_items")),
    )
    op.create_index(op.f("ix_ingested_items_classification"), "ingested_items", ["classification"], unique=False)
    op.create_index(op.f("ix_ingested_items_dedup_hash"), "ingested_items", ["dedup_hash"], unique=True)
    op.create_index(op.f("ix_ingested_items_published_at"), "ingested_items", ["published_at"], unique=False)
    op.create_index(op.f("ix_ingested_items_source_id"), "ingested_items", ["source_id"], unique=False)
    op.create_index(op.f("ix_ingested_items_status"), "ingested_items", ["status"], unique=False)

    # -- pipeline_runs ------------------------------------------------------
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trigger", enum("pipeline_trigger"), nullable=False),
        sa.Column("status", enum("run_status"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("initiated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["initiated_by"], ["users.id"],
            name=op.f("fk_pipeline_runs_initiated_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index(op.f("ix_pipeline_runs_initiated_by"), "pipeline_runs", ["initiated_by"], unique=False)
    op.create_index(op.f("ix_pipeline_runs_status"), "pipeline_runs", ["status"], unique=False)

    # -- pipeline_steps -----------------------------------------------------
    op.create_table(
        "pipeline_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent", enum("agent_name"), nullable=False),
        sa.Column("status", enum("step_status"), nullable=False),
        sa.Column(
            "input_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "output_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["pipeline_runs.id"],
            name=op.f("fk_pipeline_steps_run_id_pipeline_runs"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_steps")),
    )
    op.create_index(op.f("ix_pipeline_steps_run_id"), "pipeline_steps", ["run_id"], unique=False)

    # -- insights -----------------------------------------------------------
    op.create_table(
        "insights",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", enum("insight_kind"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("likelihood", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "domains",
            postgresql.ARRAY(sa.String(length=100)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column(
            "recommendations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("language", enum("language"), nullable=False),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column("status", enum("publication_status"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_agent", enum("agent_name"), nullable=False),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_insights_confidence_range")),
        sa.CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name=op.f("ck_insights_likelihood_range"),
        ),
        sa.CheckConstraint("severity >= 1 AND severity <= 5", name=op.f("ck_insights_severity_range")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["pipeline_runs.id"],
            name=op.f("fk_insights_run_id_pipeline_runs"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insights")),
    )
    op.create_index(op.f("ix_insights_classification"), "insights", ["classification"], unique=False)
    op.create_index(op.f("ix_insights_kind"), "insights", ["kind"], unique=False)
    op.create_index(op.f("ix_insights_run_id"), "insights", ["run_id"], unique=False)
    op.create_index(op.f("ix_insights_severity"), "insights", ["severity"], unique=False)
    op.create_index(op.f("ix_insights_status"), "insights", ["status"], unique=False)

    # -- briefings ----------------------------------------------------------
    op.create_table(
        "briefings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body_en", sa.Text(), nullable=False),
        sa.Column("body_ar", sa.Text(), nullable=False),
        sa.Column(
            "sections",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column("status", enum("publication_status"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=op.f("ck_briefings_confidence_range")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["pipeline_runs.id"],
            name=op.f("fk_briefings_run_id_pipeline_runs"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefings")),
    )
    op.create_index(op.f("ix_briefings_classification"), "briefings", ["classification"], unique=False)
    op.create_index(op.f("ix_briefings_date"), "briefings", ["date"], unique=False)
    op.create_index(op.f("ix_briefings_run_id"), "briefings", ["run_id"], unique=False)
    op.create_index(op.f("ix_briefings_status"), "briefings", ["status"], unique=False)

    # -- approvals ----------------------------------------------------------
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", enum("approval_subject"), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_agent", enum("agent_name"), nullable=False),
        sa.Column("assigned_role", enum("role"), nullable=False),
        sa.Column("status", enum("approval_status"), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"],
            name=op.f("fk_approvals_decided_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.UniqueConstraint("subject_type", "subject_id", name="uq_approvals_subject"),
    )
    op.create_index(op.f("ix_approvals_decided_by"), "approvals", ["decided_by"], unique=False)
    op.create_index(op.f("ix_approvals_status"), "approvals", ["status"], unique=False)
    op.create_index(op.f("ix_approvals_subject_id"), "approvals", ["subject_id"], unique=False)
    op.create_index(op.f("ix_approvals_subject_type"), "approvals", ["subject_type"], unique=False)

    # -- memory_entries -----------------------------------------------------
    op.create_table(
        "memory_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", enum("memory_kind"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.String(length=100)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "source_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("classification", enum("classification"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name=op.f("fk_memory_entries_created_by_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_entries")),
    )
    op.create_index(op.f("ix_memory_entries_classification"), "memory_entries", ["classification"], unique=False)
    op.create_index(op.f("ix_memory_entries_created_by"), "memory_entries", ["created_by"], unique=False)
    op.create_index(op.f("ix_memory_entries_kind"), "memory_entries", ["kind"], unique=False)

    # -- chat_sessions ------------------------------------------------------
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_chat_sessions_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_sessions")),
    )
    op.create_index(op.f("ix_chat_sessions_user_id"), "chat_sessions", ["user_id"], unique=False)

    # -- chat_messages ------------------------------------------------------
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", enum("chat_role"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("language", enum("language"), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.id"],
            name=op.f("fk_chat_messages_session_id_chat_sessions"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index(op.f("ix_chat_messages_session_id"), "chat_messages", ["session_id"], unique=False)

    # -- notifications ------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("role", enum("role"), nullable=True),
        sa.Column("kind", enum("notification_kind"), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "subject_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND role IS NULL) OR (user_id IS NULL AND role IS NOT NULL)",
            name=op.f("ck_notifications_target_user_xor_role"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_notifications_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(op.f("ix_notifications_kind"), "notifications", ["kind"], unique=False)
    op.create_index(op.f("ix_notifications_read_at"), "notifications", ["read_at"], unique=False)
    op.create_index(op.f("ix_notifications_role"), "notifications", ["role"], unique=False)
    op.create_index(op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False)

    # -- api_usage ----------------------------------------------------------
    op.create_table(
        "api_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("purpose", enum("usage_purpose"), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_api_usage_user_id_users"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_usage")),
    )
    op.create_index(op.f("ix_api_usage_model"), "api_usage", ["model"], unique=False)
    op.create_index(op.f("ix_api_usage_provider"), "api_usage", ["provider"], unique=False)
    op.create_index(op.f("ix_api_usage_purpose"), "api_usage", ["purpose"], unique=False)
    op.create_index(op.f("ix_api_usage_request_id"), "api_usage", ["request_id"], unique=False)
    op.create_index(op.f("ix_api_usage_ts"), "api_usage", ["ts"], unique=False)
    op.create_index(op.f("ix_api_usage_user_id"), "api_usage", ["user_id"], unique=False)

    # -- audit_log ----------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", enum("actor_type"), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("subject_type", sa.String(length=50), nullable=True),
        sa.Column("subject_id", sa.String(length=100), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("entry_hash", name=op.f("uq_audit_log_entry_hash")),
    )
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"], unique=False)
    op.create_index(op.f("ix_audit_log_actor_id"), "audit_log", ["actor_id"], unique=False)
    op.create_index(op.f("ix_audit_log_subject_id"), "audit_log", ["subject_id"], unique=False)
    op.create_index(op.f("ix_audit_log_subject_type"), "audit_log", ["subject_type"], unique=False)
    op.create_index(op.f("ix_audit_log_ts"), "audit_log", ["ts"], unique=False)

    # -----------------------------------------------------------------------
    # pgvector HNSW indexes — cosine distance, matching the retriever's operator (<=>).
    # Named `hnsw_*` so alembic/env.py's include_object() leaves them alone.
    # -----------------------------------------------------------------------
    op.execute(
        "CREATE INDEX hnsw_document_chunks_embedding ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute(
        "CREATE INDEX hnsw_memory_entries_embedding ON memory_entries "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    # -- FTS GIN indexes (the keyword arm of hybrid retrieval) ---------------
    op.execute("CREATE INDEX gin_document_chunks_tsv ON document_chunks USING gin (content_tsv)")
    op.execute("CREATE INDEX gin_ingested_items_tsv ON ingested_items USING gin (content_tsv)")

    # -- Append-only audit guard --------------------------------------------
    op.execute(AUDIT_GUARD_FN)
    op.execute(
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION danah_audit_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION danah_audit_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION danah_audit_append_only()"
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS danah_audit_append_only()")

    op.execute("DROP INDEX IF EXISTS gin_ingested_items_tsv")
    op.execute("DROP INDEX IF EXISTS gin_document_chunks_tsv")
    op.execute("DROP INDEX IF EXISTS hnsw_memory_entries_embedding")
    op.execute("DROP INDEX IF EXISTS hnsw_document_chunks_embedding")

    for table in (
        "audit_log",
        "api_usage",
        "notifications",
        "chat_messages",
        "chat_sessions",
        "memory_entries",
        "approvals",
        "briefings",
        "insights",
        "pipeline_steps",
        "pipeline_runs",
        "ingested_items",
        "sources",
        "document_chunks",
        "documents",
        "refresh_tokens",
        "users",
    ):
        op.drop_table(table)

    for name, values in ENUM_TYPES.items():
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
