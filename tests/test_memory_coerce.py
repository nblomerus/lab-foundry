"""
Regression test for the Zep v3 created_at coercion.

Zep v3 returns message/episode created_at as an ISO *string*, but
RecalledMessage.created_at is typed datetime and the curator formats it with
%Y-%m-%d. A raw string there crashed handle_task_completed once Zep writes
started working again. _coerce_dt normalizes all cases to a datetime.
"""
from datetime import datetime, timezone

from boardroom.memory.client import _coerce_dt


def test_parses_iso_string_with_z():
    dt = _coerce_dt("2026-05-27T13:00:00Z")
    assert isinstance(dt, datetime)
    # must be usable with a datetime format specifier (the thing that crashed)
    assert f"{dt:%Y-%m-%d %H:%M}" == "2026-05-27 13:00"


def test_passes_through_datetime():
    now = datetime.now(timezone.utc)
    assert _coerce_dt(now) is now


def test_none_and_garbage_fall_back_to_now():
    assert isinstance(_coerce_dt(None), datetime)
    assert isinstance(_coerce_dt(""), datetime)
    assert isinstance(_coerce_dt("not-a-date"), datetime)
