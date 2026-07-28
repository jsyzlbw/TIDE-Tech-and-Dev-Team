from __future__ import annotations

import inspect

from app.reviews.service import ReviewService


def test_review_service_exposes_caller_owned_action_primitives() -> None:
    confirm = inspect.signature(ReviewService.confirm_in_transaction)
    reevaluate = inspect.signature(ReviewService.reevaluate_in_transaction)
    current = inspect.signature(ReviewService.require_current_report_in_transaction)
    assert list(confirm.parameters)[:2] == ["self", "session"]
    assert list(reevaluate.parameters)[:2] == ["self", "session"]
    assert list(current.parameters)[:2] == ["self", "session"]
