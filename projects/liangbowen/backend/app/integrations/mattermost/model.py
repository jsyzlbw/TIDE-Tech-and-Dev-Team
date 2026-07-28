from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    DDL,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin
from app.db.types import enum_values
from app.integrations.mattermost.ids import MATTERMOST_ID_SQL


class IntegrationEventStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    DETERMINISTIC_ERROR = "deterministic_error"


CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION guard_integration_event_evidence()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.status <> 'processing' THEN
        RAISE EXCEPTION 'integration event evidence is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
       OR NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id
       OR NEW.arrived_at IS DISTINCT FROM OLD.arrived_at
       OR NEW.status NOT IN ('completed', 'deterministic_error')
       OR NEW.response IS NULL
       OR NEW.completed_at IS NULL THEN
        RAISE EXCEPTION 'invalid integration event transition'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$
"""

CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL = """
CREATE TRIGGER trg_integration_events_guard
BEFORE UPDATE OR DELETE ON integration_events
FOR EACH ROW EXECUTE FUNCTION guard_integration_event_evidence()
"""

CREATE_INTEGRATION_EVENT_TRUNCATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_integration_event_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'integration event evidence cannot be truncated'
        USING ERRCODE = '55000';
END;
$function$
"""

CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL = """
CREATE TRIGGER trg_integration_events_no_truncate
BEFORE TRUNCATE ON integration_events
FOR EACH STATEMENT EXECUTE FUNCTION prevent_integration_event_truncate()
"""

CREATE_MATTERMOST_IDENTITY_GUARD_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION guard_mattermost_identity_authority()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'mattermost identity authority is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.mattermost_user_id IS DISTINCT FROM OLD.mattermost_user_id
       OR NEW.bound_at IS DISTINCT FROM OLD.bound_at THEN
        RAISE EXCEPTION 'mattermost identity authority is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$function$
"""

CREATE_MATTERMOST_IDENTITY_GUARD_TRIGGER_SQL = """
CREATE TRIGGER trg_mattermost_identities_guard
BEFORE UPDATE OR DELETE ON mattermost_identities
FOR EACH ROW EXECUTE FUNCTION guard_mattermost_identity_authority()
"""

CREATE_MATTERMOST_IDENTITY_TRUNCATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_mattermost_identity_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'mattermost identity authority cannot be truncated'
        USING ERRCODE = '55000';
END;
$function$
"""

CREATE_MATTERMOST_IDENTITY_TRUNCATE_TRIGGER_SQL = """
CREATE TRIGGER trg_mattermost_identities_no_truncate
BEFORE TRUNCATE ON mattermost_identities
FOR EACH STATEMENT EXECUTE FUNCTION prevent_mattermost_identity_truncate()
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MattermostIdentity(Base):
    __tablename__ = "mattermost_identities"
    __table_args__ = (
        UniqueConstraint(
            "mattermost_user_id",
            name="uq_mattermost_identities_mattermost_user_id",
        ),
        CheckConstraint(
            f"mattermost_user_id ~ '{MATTERMOST_ID_SQL}'",
            name="ck_mattermost_identities_user_id_safe",
        ),
        CheckConstraint(
            "octet_length(mattermost_username) BETWEEN 1 AND 128 "
            "AND mattermost_username = btrim(mattermost_username)",
            name="ck_mattermost_identities_username_safe",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    mattermost_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mattermost_username: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )


event.listen(
    MattermostIdentity.__table__,
    "after_create",
    DDL(CREATE_MATTERMOST_IDENTITY_GUARD_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    MattermostIdentity.__table__,
    "after_create",
    DDL(CREATE_MATTERMOST_IDENTITY_GUARD_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    MattermostIdentity.__table__,
    "after_create",
    DDL(CREATE_MATTERMOST_IDENTITY_TRUNCATE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    MattermostIdentity.__table__,
    "after_create",
    DDL(CREATE_MATTERMOST_IDENTITY_TRUNCATE_TRIGGER_SQL).execute_if(dialect="postgresql"),
)


class IntegrationEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "integration_events"
    __table_args__ = (
        UniqueConstraint("request_hash", name="uq_integration_events_request_hash"),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_integration_events_request_hash",
        ),
        CheckConstraint("source = 'mattermost'", name="ck_integration_events_source"),
        CheckConstraint(
            "event_type IN ('slash_command', 'interactive_action', "
            "'notification_delivered', 'notification_failed')",
            name="ck_integration_events_event_type",
        ),
        CheckConstraint(
            "response IS NULL OR (jsonb_typeof(response) = 'object' "
            "AND pg_column_size(response) <= 65536)",
            name="ck_integration_events_response_object",
        ),
        CheckConstraint(
            "jsonb_typeof(business_refs) = 'object' AND pg_column_size(business_refs) <= 8192",
            name="ck_integration_events_business_refs_object",
        ),
        CheckConstraint(
            "((status = 'processing' AND response IS NULL AND completed_at IS NULL) OR "
            "(status IN ('completed', 'deterministic_error') "
            "AND response IS NOT NULL AND completed_at IS NOT NULL))",
            name="ck_integration_events_status_coherence",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= arrived_at",
            name="ck_integration_events_timestamp_order",
        ),
    )

    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32),
        default="mattermost",
        server_default="mattermost",
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        String(32),
        default="slash_command",
        server_default="slash_command",
        nullable=False,
    )
    status: Mapped[IntegrationEventStatus] = mapped_column(
        Enum(
            IntegrationEventStatus,
            name="integration_event_status",
            values_callable=enum_values,
        ),
        default=IntegrationEventStatus.PROCESSING,
        server_default=IntegrationEventStatus.PROCESSING.value,
        nullable=False,
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    response: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    business_refs: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )
    arrived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


event.listen(
    IntegrationEvent.__table__,
    "after_create",
    DDL(CREATE_INTEGRATION_EVENT_GUARD_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    IntegrationEvent.__table__,
    "after_create",
    DDL(CREATE_INTEGRATION_EVENT_GUARD_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    IntegrationEvent.__table__,
    "after_create",
    DDL(CREATE_INTEGRATION_EVENT_TRUNCATE_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    IntegrationEvent.__table__,
    "after_create",
    DDL(CREATE_INTEGRATION_EVENT_TRUNCATE_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
