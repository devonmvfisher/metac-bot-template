"""Pre-registered EDGE-2 changes, both disabled by default."""
import logging
import re
from . import config, schedule
from .targets import utc

DEADLINE = re.compile(r'\b(before|by)\b[^?]*\b(20\d\d|january|february|march|april|may|june|july|august|september|october|november|december)\b')
BASE_RATE = 'Base-rate reminder: start from the status quo and move away from it only as far as the evidence supports.'


def shift(question, value, env):
    if (schedule.switch(env, 'DEADLINE_SHIFT', default=False)
            and question.kind == 'binary' and question.target == 'season'
            and DEADLINE.search(question.title.lower())):
        numerator = value * config.DEADLINE_ODDS_MULT
        return numerator / (1 - value + numerator), ['deadline-x0.8']
    return value, []


def article(row):
    try:
        published = utc(row.get('pub_date')).strftime('%B %d, %Y %I:%M %p UTC')
    except Exception:
        published = 'unknown'
    source = row.get('source_id') or 'unknown'
    return (f"**{row.get('eng_title', '')}**\n{row.get('summary', '')}\n"
            f"Original language: {row.get('language') or 'unknown'}\nPublish date: {published}\n"
            f"Source:[{source}]({row.get('article_url', '')})")


def article_counts(rows, now):
    ages = []
    for row in rows:
        try:
            ages.append(max(0, (utc(now) - utc(row.get('pub_date'))).total_seconds() / 3600))
        except Exception:
            pass
    oldest = round(max(ages), 1) if ages and len(ages) == len(rows) else 'unknown'
    logging.getLogger('fbot').info('ASKNEWS articles=%s oldest_h=%s', len(rows), oldest)
