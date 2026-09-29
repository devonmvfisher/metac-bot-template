"""Optional numeric-module routing, retaining legacy values for internal faults."""
from dataclasses import dataclass
import logging
from . import numeric, parse

logger = logging.getLogger('fbot')


@dataclass
class Run:
    values: dict
    built: object = None


def configured(env, state):
    with state.lock:
        if state.numeric_enabled is None:
            try:
                state.numeric_enabled = numeric.enabled(env)
                line = numeric.config_line(env)
                if line:
                    logger.info(line)
            except Exception:
                state.numeric_enabled = False
                state.without_modules.add('numeric')
        return state.numeric_enabled


def enabled(question, env, state):
    return configured(env, state) and question.kind in ('numeric', 'discrete') and question.size_known


def fallback(state):
    with state.lock:
        state.counts['numeric_fallback'] += 1
    state.alert('NUMERIC_FALLBACK')


def build(question, values, state):
    try:
        return Run(values, numeric.build_cdf(question, values))
    except numeric.NumericError as error:
        with state.lock:
            state.counts['numeric_drop_' + error.reason] += 1
        if error.reason != 'cdf':
            raise
    except Exception:
        state.without_modules.add('numeric')
    return Run(values)


def legacy(run):
    return {p: run.values[p] for p in parse.PERCENTILES}
