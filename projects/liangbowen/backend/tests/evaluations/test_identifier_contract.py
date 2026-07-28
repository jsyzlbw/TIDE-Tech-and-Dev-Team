from __future__ import annotations

import bisect
import re
import unicodedata

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.contracts.evaluation_v1 import (
    CREATE_SAFE_IDENTIFIER_FUNCTION_SQL,
    MARK_CODEPOINT_MULTIRANGE,
    MARK_CODEPOINT_RANGES,
    UNSAFE_CODEPOINT_MULTIRANGE,
    UNSAFE_CODEPOINT_RANGES,
    is_safe_identifier,
)

_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _parse_multirange(value: str) -> tuple[tuple[int, int], ...]:
    return tuple((int(start), int(end)) for start, end in re.findall(r"\[(\d+),(\d+)\)", value))


def _contains(
    codepoint: int,
    ranges: tuple[tuple[int, int], ...],
    starts: tuple[int, ...],
) -> bool:
    index = bisect.bisect_right(starts, codepoint) - 1
    return index >= 0 and codepoint < ranges[index][1]


def test_frozen_identifier_ranges_exactly_match_python_312_unicode_15() -> None:
    assert unicodedata.unidata_version == "15.0.0"
    unsafe_ranges = _parse_multirange(UNSAFE_CODEPOINT_MULTIRANGE)
    mark_ranges = _parse_multirange(MARK_CODEPOINT_MULTIRANGE)
    unsafe_starts = tuple(start for start, _ in unsafe_ranges)
    mark_starts = tuple(start for start, _ in mark_ranges)

    for codepoint in range(0x110000):
        category = unicodedata.category(chr(codepoint))
        expected_unsafe = (
            category in {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"}
            or (category.startswith("Z") and codepoint != 0x20)
            or any(start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES)
        )
        assert _contains(codepoint, unsafe_ranges, unsafe_starts) is expected_unsafe
        assert _contains(codepoint, mark_ranges, mark_starts) is category.startswith("M")


def test_provider_identifier_validation_does_not_follow_runtime_unicode_drift(
    monkeypatch,
) -> None:
    from app.evaluations.providers import base

    monkeypatch.setattr(unicodedata, "category", lambda _character: "Cn")

    assert base.validate_provider_identifier("模型😀", field="model", maximum=256) == "模型😀"


def test_python_and_pg16_sql_share_the_exact_frozen_ranges() -> None:
    assert "unicode_assigned" not in CREATE_SAFE_IDENTIFIER_FUNCTION_SQL
    assert UNSAFE_CODEPOINT_MULTIRANGE in CREATE_SAFE_IDENTIFIER_FUNCTION_SQL
    assert MARK_CODEPOINT_MULTIRANGE in CREATE_SAFE_IDENTIFIER_FUNCTION_SQL

    assert not is_safe_identifier("mock\u070fhidden", 128)
    assert not is_safe_identifier("mock\u0378unassigned", 128)
    assert not is_safe_identifier("\u0301", 128)
    assert is_safe_identifier("通义 千问", 128)
    assert is_safe_identifier("模型😀", 128)
    assert is_safe_identifier("Cafe\u0301", 128)


@pytest.mark.asyncio
async def test_database_and_python_contract_agree_at_every_frozen_range_boundary(
    postgres_session: AsyncSession,
) -> None:
    codepoints: set[int] = set()
    for start, end in (*UNSAFE_CODEPOINT_RANGES, *MARK_CODEPOINT_RANGES):
        codepoints.update((start - 1, start, end - 1, end))
    scalar_values = [
        chr(codepoint)
        for codepoint in sorted(codepoints)
        if 0 < codepoint < 0x110000 and not 0xD800 <= codepoint <= 0xDFFF
    ]
    values = [
        *scalar_values,
        "通义 千问",
        "模型😀",
        "Cafe\u0301",
        "mock\u070fhidden",
        "mock\u0378unassigned",
        " leading",
        "trailing ",
    ]

    database_results = (
        await postgres_session.execute(
            text(
                "SELECT is_safe_identifier(sample.value, 128) "
                "FROM unnest(CAST(:values AS text[])) WITH ORDINALITY "
                "AS sample(value, ordinal) ORDER BY sample.ordinal"
            ),
            {"values": values},
        )
    ).scalars()

    assert list(database_results) == [is_safe_identifier(value, 128) for value in values]
