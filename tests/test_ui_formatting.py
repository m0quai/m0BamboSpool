from datetime import datetime, timezone

from inventory_service import _parse_last_used
from ui_formatting import LOCAL_TIMEZONE, UiDateFormatter


def test_naive_local_timestamps_keep_their_wall_clock_time_across_dst():
    winter = _parse_last_used("2026-01-15 12:00:00")
    summer = _parse_last_used("2026-07-15 12:00:00")

    assert winter.hour == 12
    assert summer.hour == 12
    assert winter.utcoffset().total_seconds() == 3600
    assert summer.utcoffset().total_seconds() == 7200


def test_utc_timestamps_are_rendered_in_the_configured_local_zone():
    assert UiDateFormatter.format("2026-01-15T11:00:00Z", "de") == "15.01.2026 12:00"
    assert UiDateFormatter.format("2026-07-15T10:00:00Z", "de") == "15.07.2026 12:00"
    assert UiDateFormatter.format(datetime(2026, 7, 15, 10, tzinfo=timezone.utc), "de") == "15.07.2026 12:00"


def test_local_timezone_has_expected_name_or_offset():
    # Keep this assertion portable between the container's zoneinfo and the
    # Windows fallback used when the checkout has no tzdata package.
    value = datetime(2026, 7, 15, 12, tzinfo=LOCAL_TIMEZONE)
    assert value.utcoffset().total_seconds() == 7200
