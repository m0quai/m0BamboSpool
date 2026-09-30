# Locale-aware formatting helpers shared by routes, templates and scripts.

from __future__ import annotations

from datetime import datetime
from typing import Any


class UiDateFormatter:
    # Format stored m0BamboSpool timestamps consistently for the UI.

    _FORMATS = {
        "de": "%d.%m.%Y %H:%M",
        "en": "%m/%d/%Y %I:%M %p",
    }

    @classmethod
    def parse(cls, value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @classmethod
    def format(cls, value: Any, language: str = "de") -> str:
        parsed = cls.parse(value)
        if parsed is None:
            return "" if value is None else str(value)
        # Stored timestamps are local application timestamps.  Preserve the
        # displayed wall-clock time even when an ISO value carries a timezone.
        return parsed.strftime(cls._FORMATS.get(language, cls._FORMATS["de"]))


def format_ui_datetime(value: Any, language: str = "de") -> str:
    return UiDateFormatter.format(value, language)
