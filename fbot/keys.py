"""Key selection contains identifiers only; credentials stay in the client."""
import logging
from .config import COSTS, FLOOR_CREDIT, enabled

NAMES = {"router": "OPENROUTER_API_KEY", "own": "OPENROUTER_API_KEY_OWN", "bridge": "OPENAI_API_KEY"}


def mode(env, events=None):
    value = (env.get("OWN_KEY_MODE") or "off").strip().lower()
    if value not in ("off", "insurance", "primary"):
        logging.getLogger("fbot").info("CONFIG OWN_KEY_MODE invalid; using off")
        if events:
            events("OWN_KEY_CONFIG_INVALID")
        return "off"
    return value


def enabled_keys(env, own_mode):
    result = {"router"} if env.get(NAMES["router"]) else set()
    if own_mode != "off" and env.get(NAMES["own"]):
        result.add("own")
    if enabled(env, "USE_OPENAI_BRIDGE") and env.get(NAMES["bridge"]):
        result.add("bridge")
    return result


def order(env, own_mode, exhausted, target, balances=None, minimum=None, cost_c=None):
    """Sponsored first. Insurance is eligible only after sponsored C is spent."""
    balances = balances or {}
    cost_c = COSTS["C"] if cost_c is None else cost_c
    minimum = cost_c if minimum is None else minimum
    def fits(provider, cost):
        remaining = balances.get(provider)
        return remaining is None or remaining - FLOOR_CREDIT >= cost - 1e-9
    sponsor = bool(env.get(NAMES["router"])) and "router" not in exhausted
    own = (target != "minibench" and own_mode != "off"
           and bool(env.get(NAMES["own"])) and "own" not in exhausted)
    insured = not sponsor or not fits("router", cost_c)
    result = ["router"] if sponsor and fits("router", minimum) else []
    if own and fits("own", minimum) and (own_mode == "primary" or insured):
        result.append("own")
    return result
