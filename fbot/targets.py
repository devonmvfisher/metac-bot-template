from datetime import datetime, timezone
from .config import SEASON_ID, SEASON_SLUG, MINIBENCH_ID, SEASON_END_UTC


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def active(now):
    return [SEASON_ID, MINIBENCH_ID] if utc(now) < utc(SEASON_END_UTC) else []


def startup(season_count, minibench_count):
    return [f"TARGET season={SEASON_ID} slug={SEASON_SLUG} open={season_count}",
            f"TARGET minibench open={minibench_count}"]
