from __future__ import annotations

from sqlalchemy import CheckConstraint

from app.evaluations.model import EvaluationOutbox
from app.integrations.mattermost.model import IntegrationEvent


def _checks(model: type[object]) -> dict[str, str]:
    return {
        constraint.name or "": str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def test_notification_outbox_has_distinct_terminal_delivery_contract() -> None:
    columns = EvaluationOutbox.__table__.columns
    assert {"failed_at", "delivery_ref", "last_error_summary"} <= set(columns.keys())
    checks = _checks(EvaluationOutbox)
    assert "kind = 'dispatch'" in checks["ck_evaluation_outbox_attempt_count"]
    assert "attempt_count BETWEEN 0 AND 3" in checks["ck_evaluation_outbox_attempt_count"]
    terminal = checks["ck_evaluation_outbox_terminal_state"]
    assert "failed_at" in terminal
    assert "delivery_ref" in terminal
    assert "last_error_summary" in terminal


def test_integration_events_accept_only_the_four_closed_event_types() -> None:
    check = _checks(IntegrationEvent)["ck_integration_events_event_type"]
    assert "slash_command" in check
    assert "interactive_action" in check
    assert "notification_delivered" in check
    assert "notification_failed" in check
