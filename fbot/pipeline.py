import asyncio
from weakref import WeakKeyDictionary
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from . import CreditExhausted, ModelFailure, SkipQuestion
from . import aggregate, comment, guards, parse, prompts, validate
from .config import SLOTS, enabled
from .types import Result
from .priority import PriorityLimiter


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


def _run(question, prompt, slot, tier, deps):
    text, model = deps.client.slot(slot, prompt, bridge=tier == "BRIDGE",
                                  deadline=deps.deadline, timeout=deps.timeout)
    if guards.misread(text):
        return None, model, text, "misread"
    try:
        value = parse.parse(question, text)
    except ValueError:
        if deps.parser is None:
            return None, model, text, "parse"
        try:
            value = deps.parser(question, text, tier)
        except CreditExhausted:
            raise
        except Exception:
            return None, model, text, "parse"
    base_rate = parse.base_rate(text)
    if base_rate is None:
        import logging
        logging.getLogger("fbot").info("no-base-rate qid=%s", question.qid)
    if question.kind in ("numeric", "discrete") and not guards.units_ok(question, value):
        return None, model, text, "units"
    return value, model, text, None


def forecast(question, research, tier, deps, _retry=False):
    if question.kind not in ("binary", "multiple_choice", "numeric", "discrete"):
        raise SkipQuestion("UNHANDLED_TYPE")
    if deps.deadline is not None and deps.deadline <= deps.clock.monotonic():
        raise SkipQuestion("TOO_LATE")
    prompt = prompts.build(question, research, deps.clock.now(), retry=_retry)
    slots = SLOTS[tier]
    values, models, rationales, dropped = [], [], [], []
    exhausted = False
    pool = ThreadPoolExecutor(max_workers=len(slots))
    futures = [pool.submit(_run, question, prompt, slot, tier, deps) for slot in slots]
    seconds = None if deps.deadline is None else max(0, deps.deadline - deps.clock.monotonic())
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
                models.append(model)
                rationales.append(text)
                if model != slots[index][0]:
                    dropped.append(f"run{index + 1}:fallback")
        except CreditExhausted:
            exhausted = True
            dropped.append(f"run{index + 1}:exhausted")
        except Exception:
            dropped.append(f"run{index + 1}:failed")
    if deps.deadline is not None and deps.deadline <= deps.clock.monotonic():
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
        raise SkipQuestion("ALL_MODELS_FAILED")
    value = aggregate.combine(question.kind, values, question.options)
    caps = []
    if question.kind == "binary":
        value, caps = validate.cap_binary(value, values)
    elif question.kind == "multiple_choice":
        repaired = validate.repair(value)
        if repaired != value:
            caps.append("option-floor/rounding")
        value = repaired
    result = Result(value, "", rationales, models=models, caps=caps, dropped=dropped, tier=tier, deadline=deps.deadline)
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
    validate.require(question, result.value, result.cdf)
    result = comment.build(question, result, len(slots), research, deps.env)
    return result


_limiters = WeakKeyDictionary()


async def forecast_async(question, *args, **kwargs):
    loop = asyncio.get_running_loop()
    limiter = _limiters.setdefault(loop, PriorityLimiter(5))
    async with limiter.slot(question):
        return await asyncio.to_thread(forecast, question, *args, **kwargs)
