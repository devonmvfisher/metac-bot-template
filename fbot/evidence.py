"""Fail-soft boundaries between research sources and the forecasting prompt."""
from dataclasses import replace
import logging
import random
from . import markets, prompts_v1, research2, sources, webread
from .types import Research

logger = logging.getLogger('fbot')


def call(name, function, fallback, state, *args):
    try:
        return function(*args)
    except Exception as error:
        state.without_modules.add(name)
        logger.info('MODULE without=%s error=%s', name, type(error).__name__)
        return fallback


def inputs(question, research, briefs, run_index, state):
    web, pages, snapshot = briefs or (research2.WebBrief(), sources.PagesBrief(), markets.MarketsBrief())
    safe_text = call('webread', webread.neutralise, '', state, research.text,
                     (prompts_v1.NEWS_OPEN, prompts_v1.NEWS_CLOSE))
    news = Research(safe_text, research.articles, research.available)
    web_block = call('research2', research2.prompt_block, '', state, web)
    page_block = call('sources', sources.prompt_block, '', state, pages)
    market_block = call('markets', markets.prompt_block, '', state, snapshot)
    news_block = call('prompts_v1', prompts_v1.news_block, '', state, news)
    blocks = call('research2', research2.ordered_blocks, [news_block, web_block], state,
                  [news_block, web_block], random.Random(f'{question.qid}:{run_index}'))
    merged = '\n\n'.join(block for block in (web_block, page_block, market_block, safe_text) if block)
    if not research.available and not web.available:
        merged = '\n'.join((prompts_v1.NO_RESEARCH, merged))
    return Research(merged, research.articles, bool(merged)), blocks, [block for block in (page_block, market_block) if block]


def sync(tally, state):
    try:
        with state.lock:
            for name, count in tally.counts().items():
                state.counts[name] = count
        state.research2_usd = tally.spend()['research2_usd']
        tally.raise_alerts(state)
    except Exception:
        state.without_modules.add('research2')
