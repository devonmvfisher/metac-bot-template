import asyncio
from weakref import WeakKeyDictionary
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from . import CreditExhausted, ModelFailure, SkipQuestion
from . import aggregate, comment, guards, parse, prompts, validate
from .config import SLOTS, TIERS, FLASH, SOL, COSTS, model_weight, enabled
from .types import Result
from . import schedule, prompts_v1, misread, numeric, numeric_path, evidence, research2, edge


@dataclass
class Dependencies:
    client: object
    clock: object
    state: object
    env: dict
    prepare: object = None
    parser: object = None
    deadline: float | None = None
    timeout: float = 480
    granted: object = None
    without: set = field(default_factory=set)
    evidence: tuple | None = None


def _run(question, research, slot, tier, deps, retry=False, run_index=0):
    modern = schedule.switch(deps.env, 'PROMPTS_V1')
    new_numeric = numeric_path.enabled(question, deps.env, deps.state)
    legacy, research_blocks, evidence_blocks = evidence.inputs(question, research, deps.evidence, run_index, deps.state)
    try:
        final = numeric.percentile_guidance(question) + '\n' + numeric.final_format(question) if new_numeric else None
        prompt = (prompts_v1.build(question, research, deps.clock.now(), retry=retry, numeric_final=final, research_blocks=research_blocks, evidence=evidence_blocks, deadline_shift=schedule.switch(deps.env, "DEADLINE_SHIFT", default=False)) if modern else
                  prompts.build(question, legacy, deps.clock.now(), retry=retry, use_numeric=new_numeric))
    except Exception:
        deps.without.add('prompts_v1')
        try:
            prompt = prompts.build(question, legacy, deps.clock.now(), retry=retry, use_numeric=new_numeric)
        except Exception:
            deps.without.add('numeric')
            prompt = prompts.build(question, legacy, deps.clock.now(), retry=retry)
    text, model = deps.client.slot(slot, prompt, bridge=tier == "BRIDGE",
                                  deadline=deps.deadline, timeout=deps.timeout, target=question.target, minimum="M" if tier == "M" else None)
    if not modern and guards.misread(text):
        return None, model, text, "misread"
    try:
        value = numeric.parse_percentiles(question, text) if new_numeric else parse.parse(question, text)
    except ValueError:
        if deps.parser is None:
            return None, model, text, "parse"
        try:
            value = deps.parser(question, text, tier)
        except CreditExhausted:
            raise
        except Exception:
            return None, model, text, "parse"
    except Exception:
        if not new_numeric:
            raise
        deps.without.add('numeric')
        value = parse.legacy_numeric(question, text)
        new_numeric = False
    if modern:
        try:
            if misread.drop(question, text, value):
                return None, model, text, 'misread'
        except Exception:
            deps.without.add('misread')
    base_rate = parse.base_rate(text)
    if base_rate is None:
        import logging
        logging.getLogger("fbot").info("no-base-rate qid=%s", question.qid)
    if new_numeric:
        try:
            value = numeric_path.build(question, value, deps.state)
        except numeric.NumericError as error:
            return None, model, text, error.reason
    elif question.kind in ("numeric", "discrete") and not guards.units_ok(question, value):
        return None, model, text, "units"
    return value, model, text, None


def forecast(question, research, tier, deps, _retry=False):
    if question.kind not in ("binary", "multiple_choice", "numeric", "discrete"):
        raise SkipQuestion("UNHANDLED_TYPE")
    if deps.deadline is not None and deps.deadline <= deps.clock.monotonic():
        raise SkipQuestion("TOO_LATE")
    post_deadline = deps.clock.monotonic() + max(0, 3300 - (deps.clock.now() - deps.state.job_started).total_seconds())
    if question.close_time is not None:
        from .targets import utc
        post_deadline = min(post_deadline, deps.clock.monotonic() + (utc(question.close_time) - deps.clock.now()).total_seconds() - 60)
    combine_at = None if deps.deadline is None else deps.deadline - 120
    run_deps = replace(deps, deadline=combine_at)
    slots = SLOTS[tier]
    fallback_runs = TIERS[tier].get("flash_down", ())
    flash_indices = tuple(index for index, slot in enumerate(slots) if fallback_runs and slot == FLASH)
    # The outage plan counts attempted non-Flash runs, even if one failed.
    if flash_indices:
        slots = tuple(slot[:-1] if index in flash_indices else slot for index, slot in enumerate(slots))
    values, models, rationales, dropped = [], [], [], []
    transient = 0
    attempted_slots = [slot for index, slot in enumerate(slots) if index not in flash_indices]
    exhausted = False
    pool = ThreadPoolExecutor(max_workers=len(slots))
    futures = [pool.submit(_run, question, research, slot, tier, run_deps, _retry, index) for index, slot in enumerate(slots)]
    seconds = None if combine_at is None else max(0, combine_at - deps.clock.monotonic())
    done, pending = wait(futures, timeout=seconds)
    for future in pending:
        future.cancel()
    pool.shutdown(wait=False, cancel_futures=True)
    for index, future in enumerate(futures):
        if future in pending:
            dropped.append(f"run{index + 1}:deadline")
            continue
        try:
            value, model, text, reason = future.result()
            if reason:
                dropped.append(f"run{index + 1}:{reason}")
            else:
                values.append(value)
                if isinstance(value, numeric_path.Run) and value.built is None:
                    dropped.append(f'run{index + 1}:cdf')
                models.append(model)
                rationales.append(text)
                if model != slots[index][0]:
                    dropped.append(f"run{index + 1}:fallback")
        except CreditExhausted:
            exhausted = True
            dropped.append(f"run{index + 1}:exhausted")
        except ModelFailure as failure:
            dropped.append(f"run{index + 1}:failed")
            transient += int(failure.transient)
        except Exception:
            dropped.append(f"run{index + 1}:failed")
    if flash_indices and not exhausted and not pending:
        flash_failed = all(futures[index].exception() is not None for index in flash_indices)
        if flash_failed and (combine_at is None or deps.clock.monotonic() < combine_at):
            missing = [chain for chain, weight in fallback_runs]
            for chain in attempted_slots:
                if chain in missing:
                    missing.remove(chain)
            for index, chain in enumerate(missing):
                if combine_at is not None and deps.clock.monotonic() >= combine_at:
                    break
                try:
                    extra, model, text, reason = _run(question, research, chain, tier, run_deps, _retry, len(slots) + index)
                    if reason is None:
                        values.append(extra)
                        models.append(model)
                        rationales.append(text)
                        dropped.append("flash:unavailable")
                except CreditExhausted:
                    exhausted = True
                    break
                except Exception:
                    pass
    if post_deadline <= deps.clock.monotonic() or (not values and combine_at is not None and combine_at <= deps.clock.monotonic()):
        raise SkipQuestion("TOO_LATE")
    if exhausted:
        if tier != "BRIDGE" and question.target != "minibench" and enabled(deps.env, "USE_OPENAI_BRIDGE") and deps.env.get("OPENAI_API_KEY"):
            result = forecast(question, research, "BRIDGE", deps, _retry=_retry)
            result.summary += "\nFALLBACK OpenAI bridge after credit exhaustion"
            if len(result.summary) > 1000:
                raise SkipQuestion("INVALID_OUTPUT")
            return result
        raise SkipQuestion("EXHAUSTED")
    if not values:
        all_misread = len(dropped) == len(slots) and all(v.endswith(":misread") for v in dropped)
        if all_misread and not _retry:
            try:
                result = forecast(question, research, "C" if tier != "BRIDGE" else "BRIDGE", deps, _retry=True)
            except SkipQuestion as skip:
                if skip.reason in ("EXHAUSTED", "TOO_LATE"):
                    raise
                raise SkipQuestion("MISREAD_ALL") from None
            result.summary += "\nRETRY all initial runs misread; warning + one retry"
            if len(result.summary) > 1000:
                raise SkipQuestion("INVALID_OUTPUT")
            return result
        if all_misread:
            raise SkipQuestion("MISREAD_ALL")
        if transient and transient == len(dropped):  # R3F
            raise SkipQuestion("MODEL_TRANSIENT")
        raise SkipQuestion("ALL_MODELS_FAILED")
    legacy_values = [numeric_path.legacy(value) if isinstance(value, numeric_path.Run) else value for value in values]
    value = aggregate.combine(question.kind, legacy_values, question.options, [model_weight(model) for model in models])
    built = None
    if all(isinstance(value, numeric_path.Run) and value.built is not None for value in values):
        try:
            built = numeric.combine(question, [value.built.cdf for value in values])
        except Exception:
            deps.without.add('numeric')
    caps = []
    if question.kind == "binary":
        value, shifted = edge.shift(question, value, deps.env)
        value, caps = validate.cap_binary(value, values)
        caps = shifted + caps
    elif question.kind == "multiple_choice":
        repaired = validate.repair(value)
        if repaired != value:
            caps.append("option-floor/rounding")
        value = repaired
    result = Result(value, "", rationales, models=models, caps=caps, dropped=dropped, tier=tier, deadline=post_deadline)
    if built is not None:
        result.numeric_v1, result.cdf = True, list(built.cdf)
        notes = [note for item in values for note in item.built.notes] + list(built.notes)
        result.caps.extend(note for note in dict.fromkeys(notes) if note not in result.caps)
    elif question.kind in ('numeric', 'discrete') and numeric_path.configured(deps.env, deps.state):
        numeric_path.fallback(deps.state)
    result.caps.extend('without-' + name for name in sorted(deps.without | deps.state.without_modules))
    if deps.prepare is not None:
        try:
            result = deps.prepare(question, result)
        except CreditExhausted:
            if tier != "BRIDGE" and question.target != "minibench" and enabled(deps.env, "USE_OPENAI_BRIDGE") and deps.env.get("OPENAI_API_KEY"):
                result = forecast(question, research, "BRIDGE", deps, _retry=_retry)
                result.summary += "\nFALLBACK OpenAI bridge after parser credit exhaustion"
                if len(result.summary) > 1000:
                    raise SkipQuestion("INVALID_OUTPUT")
                return result
            raise SkipQuestion("EXHAUSTED") from None
    validate.require(question, result.value, result.cdf, result.numeric_v1)
    if question.kind in ('numeric', 'discrete'):
        import logging
        if result.numeric_v1:
            deps.state.counts['numeric_v1'] += 1
        logging.getLogger('fbot').info('NUMERIC qid=%s runs=%s/%s fallback=%s notes=%s',
            question.qid, len(models), len(slots), int(not result.numeric_v1 and numeric_path.configured(deps.env, deps.state)), len(result.caps))
    if deps.evidence:
        result.research_label = evidence.call('research2', research2.research_line, None, deps.state,
                                               research.available, research.articles, deps.evidence[0])
    result = comment.build(question, result, len(slots), research, deps.env)
    return result


_limiters = WeakKeyDictionary()


async def forecast_async(question, research, tier, deps, **kwargs):
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = schedule.PriorityLimiter()
        _limiters[loop] = limiter
    async with limiter.slot(schedule.priority_key(question)):
        try:
            granted = schedule.budget(question, deps.clock, deps.state.job_started)
            deps = replace(deps, deadline=granted.deadline, timeout=granted.timeout)
            if deps.granted:
                deps.granted(granted.deadline)
        except SkipQuestion:
            raise
        except Exception as error:
            import logging
            logging.getLogger('fbot').info('BUDGET error=%s', type(error).__name__)
            deps.without.add('schedule')
        return await asyncio.to_thread(forecast, question, research, tier, deps, **kwargs)
