from datetime import datetime, timezone
from .config import SEASON_ID, SEASON_SLUG, MINIBENCH_ID, SEASON_END_UTC
from dataclasses import dataclass

KNOWN_SEASONS = {SEASON_ID: (SEASON_SLUG, SEASON_END_UTC)}


@dataclass(frozen=True)
class Season:
    id: int
    slug: str
    end: str
    valid: bool


def season(env=None):
    from . import config, schedule
    env = env or {}
    def setting(name, default):
        return str(env.get(name) or '').strip() or default
    try:
        identity = int(setting('SEASON_ID', SEASON_ID))
        slug = setting('SEASON_SLUG', SEASON_SLUG)
        end = setting('SEASON_END_UTC', SEASON_END_UTC)
        normalized = utc(end).isoformat()
        known = KNOWN_SEASONS.get(identity)
        valid = bool(known and known[0] == slug and utc(known[1]).isoformat() == normalized)
        if not known and schedule.switch(env, 'ALLOW_NEW_SEASON', default=False):
            import re
            valid = identity > 0 and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug) is not None
        valid = valid and identity not in config.FORBIDDEN_TARGETS and not any(str(value) in slug for value in config.FORBIDDEN_TARGETS if isinstance(value, str))
        return Season(identity, slug, end, bool(valid))
    except Exception:
        return Season(SEASON_ID, SEASON_SLUG, SEASON_END_UTC, False)


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def active(now, env=None):
    configured = season(env)
    if utc(now) >= utc(configured.end):
        return []
    return [configured.id, MINIBENCH_ID] if configured.valid else [MINIBENCH_ID]


def startup(season_count, minibench_count, env=None):
    configured = season(env)
    return [f"TARGET season={configured.id} slug={configured.slug if configured.valid else 'invalid'} open={season_count}",
            f"TARGET minibench open={minibench_count}"]
