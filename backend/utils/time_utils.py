from datetime import datetime, timezone


def now_iso_utc() -> str:
    """Return current UTC time as ISO-8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat()


__all__ = ["now_iso_utc"]
