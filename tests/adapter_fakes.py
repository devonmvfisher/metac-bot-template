"""SDK boundary fakes load the real main.py; they do not certify the live SDK."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from fbot.config import SOL
from fbot.postgate import Gate
from fbot.state import RunState
from fbot.types import Research
from .fakes import FakeClock, TextClient, narration


class Object:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def load_adapter():
    sdk = ModuleType("forecasting_tools")
    for name in ("ForecastBot", "BinaryQuestion", "MultipleChoiceQuestion", "NumericQuestion",
                 "DateQuestion", "ConditionalQuestion", "DiscreteQuestion", "Percentile",
                 "PredictedOptionList", "PredictedOption", "BinaryPrediction", "ReasonedPrediction", "GeneralLlm"):
        setattr(sdk, name, type(name, (Object,), {}))
    class Distribution:
        @classmethod
        def from_question(cls, percentiles, raw):
            raw.seen_percentiles = percentiles
            return SimpleNamespace(get_cdf=lambda: [SimpleNamespace(percentile=v) for v in raw.cdf])
    sdk.NumericDistribution = Distribution
    async def forbidden(*args, **kwargs):
        raise AssertionError("unexpected structure_output call")
    sdk.structure_output = forbidden
    helpers = ModuleType("bot_helpers")
    helpers.check_environment = lambda **kwargs: None
    spec = importlib.util.spec_from_file_location("adapter_under_test", Path(__file__).resolve().parents[1] / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, forecasting_tools=sdk, bot_helpers=helpers):
        spec.loader.exec_module(module)
    return module, sdk


def raw_question(sdk, kind="BinaryQuestion", **kwargs):
    values = dict(id_of_question=8101, id_of_post=9101, question_text="Synthetic open question",
                  background_info="", resolution_criteria="Fixture criteria", fine_print="",
                  close_time=FakeClock().now().replace(hour=1), scheduled_resolution_time=FakeClock().now())
    if kind in ("NumericQuestion", "DiscreteQuestion"):
        values.update(lower_bound=0, upper_bound=100, open_lower_bound=False, open_upper_bound=False,
                      zero_point=None, unit_of_measure="", inbound_outcome_count=200,
                      cdf=[i / 200 for i in range(201)])
    if kind == "MultipleChoiceQuestion":
        values["options"] = ["red", "blue"]
    values.update(kwargs)
    return getattr(sdk, kind)(**values)


def bot_for(module, loop=None, value=None, test=False):
    clock = FakeClock()
    state = RunState(clock)
    client = TextClient({SOL[0]: narration("Probability: 37%") if value is None else value})
    bot = module.FBot()
    bot.setup({}, state, client, SimpleNamespace(tier=lambda *a, **k: "C"),
              SimpleNamespace(get=lambda *a: Research()), Gate(state, readback=lambda *a: False),
              test=test, main_loop=loop)
    return bot
