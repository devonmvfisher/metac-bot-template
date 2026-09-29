"""Explicit targets, models and conservative output limits."""

VERSION = "v1"
SEASON_ID = 33121
SEASON_SLUG = "fall-futureeval-2026"
MINIBENCH_ID = "minibench"
TEST_ID = "bot-testing-area"
FORBIDDEN_TARGETS = (33022, "summer-futureeval-2026",
                     33021, "metaculus-cup-summer-2026",
                     33108, "metaculus-cup-fall-2026", "metaculus-cup")
SEASON_END_UTC = "2027-01-07T00:00:00Z"
OPUS = ("anthropic/claude-opus-5.5",)
SOL = ("openai/gpt-6-sol",)
FLASH = ("google/gemini-3.8-flash", "google/gemini-3.6-flash") + SOL
CHEAP = ("google/gemini-3.8-flash", "openai/gpt-6-luna")
BRIDGE = ("gpt-6-sol",)
# A tier addition changes this table, with its corresponding tests.
TIERS = {
    "A": {"runs": ((OPUS, 2), (OPUS, 2), (SOL, 2), (SOL, 2), (FLASH, 1)), "usd": 1.20},
    "B": {"runs": ((OPUS, 2), (SOL, 2), (FLASH, 1), (FLASH, 1)), "usd": 0.77},
    "C": {"runs": ((SOL, 2), (FLASH, 1), (FLASH, 1)), "usd": 0.36, "flash_down": ((SOL, 2), (SOL, 2))},
    "M": {"runs": ((FLASH, 1), (FLASH, 1), (FLASH, 1)), "usd": 0.24, "fast": True},
    "BRIDGE": {"runs": ((BRIDGE, 2),), "usd": 0.36, "bridge": True},
}
COST_SAFETY = 1.5
DEADLINE_ODDS_MULT = 0.8
SLOTS = {tier: tuple(chain for chain, weight in spec["runs"]) for tier, spec in TIERS.items()}
MODEL_WEIGHTS = {chain[0]: weight for spec in TIERS.values() for chain, weight in spec["runs"]}
def model_weight(model):
    return MODEL_WEIGHTS.get(model, MODEL_WEIGHTS[FLASH[0]])
SHORT = {OPUS[0]: "Opus", SOL[0]: "Sol", BRIDGE[0]: "Bridge", **{m: "Flash" for m in FLASH[:-1]}}
PROBES = tuple(dict.fromkeys(OPUS + SOL + FLASH + (CHEAP[1],)))
COSTS = {tier: spec["usd"] * COST_SAFETY for tier, spec in TIERS.items()}
FLOOR_CREDIT = 2.00
BINARY_CAP = 0.02
SINGLE_CAP = 0.05
MC_FLOOR = 0.01
PERCENTILES = (10, 20, 40, 60, 80, 90)
MAX_QUESTIONS = 5


def enabled(env, name):
    return env.get(name, "").strip().lower() == "true"


def researcher_slot(env):
    configured = env.get("ASKNEWS_API_KEY") or (
        env.get("ASKNEWS_CLIENT_ID") and env.get("ASKNEWS_SECRET"))
    return "asknews/news-summaries" if configured else "no_research"


def model_slots(env):
    direct = not env.get("OPENROUTER_API_KEY") and enabled(env, "USE_OPENAI_BRIDGE")
    return {"default": "openai/gpt-6-sol" if direct else "openrouter/openai/gpt-6-sol",
            "parser": "openai/gpt-6-sol" if direct else "openrouter/" + CHEAP[0],
            "summarizer": "openai/gpt-6-sol" if direct else "openrouter/" + CHEAP[0],
            "researcher": researcher_slot(env)}
