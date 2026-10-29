# Locale-aware formatting helpers shared by routes, templates and scripts.

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
import calendar
import os
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _EuropeBerlinFallback(tzinfo):
    """Small IANA-compatible fallback for Windows checkouts without tzdata."""

    @staticmethod
    def _last_sunday(year: int, month: int) -> int:
        last_day = calendar.monthrange(year, month)[1]
        return last_day - ((datetime(year, month, last_day).weekday() + 1) % 7)

    def utcoffset(self, value):
        if value is None:
            return timedelta(hours=1)
        naive = value.replace(tzinfo=None)
        start = datetime(naive.year, 3, self._last_sunday(naive.year, 3), 2)
        end = datetime(naive.year, 10, self._last_sunday(naive.year, 10), 2)
        # The repeated hour at the autumn transition is standard time only
        # when fold=1; fold=0 remains the preceding summer-time occurrence.
        if end <= naive < end + timedelta(hours=1):
            return timedelta(hours=1 if value.fold else 2)
        return timedelta(hours=2) if start <= naive < end else timedelta(hours=1)

    def dst(self, value):
        return self.utcoffset(value) - timedelta(hours=1)

    def tzname(self, value):
        return "CEST" if self.dst(value) else "CET"


def _configured_timezone() -> tzinfo:
    """Return the application's civil timezone, including DST rules.

    The database contains a mixture of older naive local timestamps and newer
    ISO timestamps with an offset.  Resolving both through the configured
    IANA zone (rather than the machine's current fixed offset) keeps historical
    summer/winter values correct as well.
    """
    name = (os.environ.get("TZ") or "Europe/Berlin").strip() or "Europe/Berlin"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # The container has the system tz database.  A plain Windows checkout
        # may not, so retain correct EU DST behavior for local tests as well.
        return _EuropeBerlinFallback()


LOCAL_TIMEZONE = _configured_timezone()


class UiDateFormatter:
    # Format stored m0BamboSpool timestamps consistently for the UI.

    _FORMATS = {
        "de": "%d.%m.%Y %H:%M",
        "en": "%m/%d/%Y %I:%M %p",
    }

    @classmethod
    def parse(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        # Legacy database rows are naive but represent local application time.
        # Attach the IANA zone so dates around the DST transition are not
        # interpreted as UTC (or with today's fixed offset).
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(LOCAL_TIMEZONE)

    @classmethod
    def format(cls, value: Any, language: str = "de") -> str:
        parsed = cls.parse(value)
        if parsed is None:
            return "" if value is None else str(value)
        return parsed.strftime(cls._FORMATS.get(language, cls._FORMATS["de"]))


def format_ui_datetime(value: Any, language: str = "de") -> str:
    return UiDateFormatter.format(value, language)
