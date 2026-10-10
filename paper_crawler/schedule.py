"""Conference eligibility uses verified calendar dates, never submission deadlines."""
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse


def validate_meeting_dates(entries):
    if not isinstance(entries, dict):
        raise ValueError("meeting_dates must be an object keyed by year")
    for year, entry in entries.items():
        if not isinstance(year, str) or not year.isdigit() or not 1900 <= int(year) <= 2200:
            raise ValueError("meeting_dates keys must be year strings")
        if not isinstance(entry, dict):
            raise ValueError("meeting_dates entries need end_date and source_url")
        try:
            end = date.fromisoformat(entry["end_date"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("meeting_dates.end_date must be YYYY-MM-DD") from None
        if entry["end_date"] != end.isoformat() or end.year != int(year):
            raise ValueError("meeting_dates.end_date must match its year")
        url = entry.get("source_url", "")
        if not isinstance(url, str) or urlparse(url).scheme not in {"https", "http"} or not urlparse(url).netloc:
            raise ValueError("meeting_dates.source_url must be an absolute HTTP(S) URL")


def eligibility(source, year, now):
    """Return a skip reason or None. End dates expire conservatively in UTC-12."""
    if year > now.year:
        return "future_year"
    if source.options.get("item_type") == "journalArticle":
        return None
    entry = source.meeting_dates.get(str(year))
    if entry:
        end = date.fromisoformat(entry["end_date"])
        available = datetime.combine(end + timedelta(days=1), time(), timezone(timedelta(hours=-12)))
        if now < available:
            return "conference_not_finished"
    elif year == now.year:
        return "unknown_meeting_end_date"
    return None


def utc_now():
    return datetime.now(timezone.utc)
