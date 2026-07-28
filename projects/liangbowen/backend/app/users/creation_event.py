from __future__ import annotations

import uuid

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.db.types import Role, enum_values

CREATE_ACCOUNT_CREATION_IMMUTABILITY_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_account_creation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'account creation events are append-only'
        USING ERRCODE = '55000';
END;
$function$
"""


class AccountCreationEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "account_creation_events"
    __table_args__ = (
        UniqueConstraint(
            "target_user_id",
            name="uq_account_creation_events_target_user_id",
        ),
        CheckConstraint(
            "target_role IN ('teacher', 'student')",
            name="ck_account_creation_events_target_role",
        ),
        CheckConstraint(
            "octet_length(request_id) BETWEEN 1 AND 128 "
            "AND request_id = btrim(request_id) "
            "AND request_id !~ '[[:cntrl:]]'",
            name="ck_account_creation_events_request_id_safe",
        ),
    )

    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_account_creation_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_account_creation_events_target_user_id_users",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    target_role: Mapped[Role] = mapped_column(
        Enum(Role, name="role", values_callable=enum_values),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)


Index(
    "ix_account_creation_events_actor_created_id",
    AccountCreationEvent.actor_user_id,
    AccountCreationEvent.created_at.desc(),
    AccountCreationEvent.id.desc(),
)
Index(
    "ix_account_creation_events_created_id",
    AccountCreationEvent.created_at.desc(),
    AccountCreationEvent.id.desc(),
)


event.listen(
    AccountCreationEvent.__table__,
    "after_create",
    DDL(CREATE_ACCOUNT_CREATION_IMMUTABILITY_FUNCTION_SQL).execute_if(dialect="postgresql"),
)
event.listen(
    AccountCreationEvent.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_account_creation_events_no_update
        BEFORE UPDATE ON account_creation_events
        FOR EACH ROW EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AccountCreationEvent.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_account_creation_events_no_delete
        BEFORE DELETE ON account_creation_events
        FOR EACH ROW EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AccountCreationEvent.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_account_creation_events_no_truncate
        BEFORE TRUNCATE ON account_creation_events
        FOR EACH STATEMENT EXECUTE FUNCTION prevent_account_creation_event_mutation()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(
    AccountCreationEvent.__table__,
    "after_drop",
    DDL("DROP FUNCTION IF EXISTS prevent_account_creation_event_mutation()").execute_if(
        dialect="postgresql"
    ),
)
