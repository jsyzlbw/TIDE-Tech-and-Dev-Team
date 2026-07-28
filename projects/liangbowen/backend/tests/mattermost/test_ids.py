from __future__ import annotations

import pytest

from app.integrations.mattermost.ids import MATTERMOST_ID_SQL, validate_mattermost_id
from app.integrations.mattermost.model import MattermostIdentity


@pytest.mark.parametrize(
    "value",
    [
        "3g8f1k9m4p6x7z2q5w0n8c1vbd",
        "0123456789abcdefghijklmnop",
        "post0000000000000000000001",
        "test-id_1",
    ],
)
def test_shared_mattermost_id_validator_accepts_official_and_test_ids(value: str) -> None:
    assert validate_mattermost_id(value) == value


@pytest.mark.parametrize("value", ["", " space", "bad/id", "a" * 129, "用户"])
def test_shared_mattermost_id_validator_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_mattermost_id(value)


def test_database_contract_allows_a_numeric_first_character() -> None:
    assert "[A-Za-z0-9]" in MATTERMOST_ID_SQL
    constraint = next(
        item
        for item in MattermostIdentity.__table__.constraints
        if item.name == "ck_mattermost_identities_user_id_safe"
    )
    assert MATTERMOST_ID_SQL in str(constraint.sqltext)
