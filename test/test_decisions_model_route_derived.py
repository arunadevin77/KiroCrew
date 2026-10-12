"""``model.route`` derived tiers: targets read off the live advertised list.

With ``decisions.model_route_derive`` on, a tier the owner left empty takes a
target derived from the advertised models and their credit multipliers. These
tests pin the four promises that make that safe to ship off by default:

- the switch OFF routes exactly as the owner map alone does;
- an owner pin always wins over a derived target;
- every derived id is one the account advertises, and a cold list derives nothing;
- a derived choice is marked ``derived`` on its record.

The model ids here are fakes; test files are outside the tree the model-id gate
reads, and production code names none.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import model_registry
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT, DecisionsConfig
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions import outcomes
from kiro_crew.decisions.points import model_route as mr
from kiro_crew.decisions.types import Answer

#: A fake advertised list with prices: one cheap, two at the baseline, one dear.
RATES = {
    "fake-mini": 0.25,
    "fake-tiny": 0.5,
    "fake-standard": 1.0,
    "fake-also-standard": 1.0,
    "fake-frontier": 2.2,
}
ADVERTISED = ["auto", *RATES]


def _rate(model_id: str) -> float | None:
    return RATES.get(model_id)


EMPTY_MAP = {"simple": "", "medium": "", "complex": ""}


def _config(*, derive: bool, model_route: dict | None = None):
    return SimpleNamespace(
        decisions=DecisionsConfig(
            bucket=100,
            model_route=dict(EMPTY_MAP if model_route is None else model_route),
            model_route_derive=derive,
        )
    )


@pytest.fixture(autouse=True)
def consent(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT,
                "history_budget_chars": 0,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def clean_outcomes():
    outcomes.reset()
    yield
    outcomes.reset()


@pytest.fixture(autouse=True)
def log_home(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")


@pytest.fixture
def catalog_rates(monkeypatch):
    """The catalog's multipliers, as the one lookup derivation reads by default."""
    monkeypatch.setattr(model_registry, "_KIRO_RATES", dict(RATES))


class _Oracle:
    def __init__(self, value: str) -> None:
        self.value = value

    async def ask(self, state, questions):
        return {q.id: Answer(id=q.id, value=self.value, p=0.95) for q in questions}


@pytest.fixture
def route(monkeypatch):
    """Answer *tier* under *cfg* and return what ``routed_model`` decided."""
    import kiro_crew.decisions.gate as gate_mod
    import kiro_crew.decisions.impl_jev as impl_mod

    def _run(tier: str, cfg, *, current_model: str = "", advertised=ADVERTISED):
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: cfg)
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: _Oracle(tier))
        return asyncio.run(
            mr.routed_model(
                "do the thing",
                session_key="chat-derived",
                current_model=current_model,
                advertised=list(advertised),
            )
        )

    return _run


class TestDerivation:
    def test_the_three_targets_come_from_the_advertised_prices(self):
        derived = mr.derive_tier_models(ADVERTISED, "", _rate)
        assert derived == {"simple": "fake-mini", "medium": "", "complex": "fake-frontier"}

    def test_the_baseline_is_the_sessions_own_price(self):
        """A session already on a cheap model has nothing cheaper below it but the
        cheapest, and nothing it could call dear except what costs more than it."""
        assert mr.derive_tier_models(ADVERTISED, "fake-tiny", _rate) == {
            "simple": "fake-mini",
            "medium": "",
            "complex": "fake-frontier",
        }
        assert mr.derive_tier_models(ADVERTISED, "fake-frontier", _rate)["complex"] == ""
        assert mr.derive_tier_models(ADVERTISED, "fake-mini", _rate)["simple"] == ""

    def test_an_unpriced_baseline_reads_as_the_auto_rate(self):
        derived = mr.derive_tier_models(ADVERTISED, "fake-unlisted", _rate)
        assert derived["simple"] == "fake-mini"
        assert derived["complex"] == "fake-frontier"

    def test_a_cold_list_derives_nothing(self):
        assert mr.derive_tier_models([], "", _rate) == EMPTY_MAP
        assert mr.derive_tier_models(ADVERTISED, "", lambda _id: None) == EMPTY_MAP

    def test_only_advertised_ids_are_ever_derived(self):
        """A priced model the account is not offered is never a target."""
        derived = mr.derive_tier_models(["fake-standard", "fake-tiny"], "", _rate)
        assert derived == {"simple": "fake-tiny", "medium": "", "complex": ""}

    def test_the_auto_sentinel_is_never_a_target(self):
        derived = mr.derive_tier_models(["auto", "fake-standard"], "", lambda _id: 0.1)
        assert "auto" not in derived.values()

    def test_an_unbroken_price_tie_inherits(self):
        rates = {"fake-x": 3.0, "fake-y": 3.0}
        derived = mr.derive_tier_models(list(rates), "", rates.get)
        assert derived["complex"] == ""

    def test_a_price_tie_goes_to_the_larger_known_window(self, monkeypatch):
        rates = {"fake-x": 3.0, "fake-x-1m": 3.0}
        windows = {"fake-x": 200_000, "fake-x-1m": 1_000_000}
        monkeypatch.setattr(mr, "known_window", windows.get)
        derived = mr.derive_tier_models(list(rates), "", rates.get)
        assert derived["complex"] == "fake-x-1m"

    def test_the_default_lookup_reads_the_catalog(self, catalog_rates):
        assert mr.derive_tier_models(ADVERTISED)["complex"] == "fake-frontier"


class TestTheCatalogRates:
    def test_a_snapshot_carries_each_rows_price(self, monkeypatch):
        monkeypatch.setattr(model_registry, "_KIRO_RATES", {})
        monkeypatch.setattr(model_registry, "refresh_advertised_models", lambda *_a: False)
        monkeypatch.setattr(model_registry, "_replace_kiro_windows", lambda _w: False)
        model_registry.refresh_kiro_catalog(
            [
                {"model_id": "fake-a", "model_name": "fake-a", "rate_multiplier": 0.4},
                {"model_id": "fake-b", "model_name": "fake-b", "rate_multiplier": True},
                {"model_id": "fake-c", "model_name": "fake-c", "rate_multiplier": -1},
                {"model_id": "fake-d", "model_name": "fake-d"},
            ],
            "acp",
        )
        assert model_registry.rate_multiplier("fake-a") == 0.4
        assert model_registry.rate_multiplier("FAKE-A") == 0.4
        for unpriced in ("fake-b", "fake-c", "fake-d", ""):
            assert model_registry.rate_multiplier(unpriced) is None

    def test_a_spelling_fold_finds_the_price(self, monkeypatch):
        monkeypatch.setattr(model_registry, "_KIRO_RATES", {"fake-4.5": 1.3})
        assert model_registry.rate_multiplier("fake-4-5") == 1.3


class TestTheSwitch:
    def test_it_ships_off(self):
        assert DecisionsConfig().model_route_derive is False
        assert DecisionsConfig.from_raw({}).model_route_derive is False

    def test_only_a_real_bool_turns_it_on(self):
        assert DecisionsConfig.from_raw({"model_route_derive": True}).model_route_derive is True
        for value in ("true", 1, "yes", None):
            assert (
                DecisionsConfig.from_raw({"model_route_derive": value}).model_route_derive is False
            )

    def test_off_applies_the_owner_map_alone(self):
        mapping, derived = mr.effective_tier_models(
            EMPTY_MAP, advertised=ADVERTISED, derive=False, rate_of=_rate
        )
        assert mapping == EMPTY_MAP and derived == frozenset()


class TestRoutedWithTheSwitch:
    @pytest.mark.parametrize("tier", list(mr.TIERS))
    def test_off_routes_exactly_as_the_owner_map_does(self, route, catalog_rates, tier):
        """The shipped default. An empty owner map stays unpinned on every tier,
        whatever the advertised prices would derive."""
        routed = route(tier, _config(derive=False))
        assert routed is not None
        assert routed["model_chosen"] == mr.UNPINNED
        assert "source" not in routed

    def test_on_routes_an_empty_tier_to_its_derived_target(self, route, catalog_rates):
        routed = route("complex", _config(derive=True))
        assert routed["model_chosen"] == "fake-frontier"
        assert routed["source"] == mr.SOURCE_DERIVED

    def test_on_leaves_the_baseline_tier_on_the_sessions_model(self, route, catalog_rates):
        routed = route("medium", _config(derive=True), current_model="fake-standard")
        assert routed["model_chosen"] == mr.UNPINNED
        assert "source" not in routed

    def test_an_owner_pin_wins_over_the_derived_target(self, route, catalog_rates):
        cfg = _config(derive=True, model_route={"simple": "fake-tiny", "medium": "", "complex": ""})
        routed = route("simple", cfg)
        assert routed["model_chosen"] == "fake-tiny"
        assert "source" not in routed

    def test_a_cold_list_on_routes_nothing_new(self, route, catalog_rates):
        routed = route("complex", _config(derive=True), advertised=[])
        assert routed["model_chosen"] == mr.UNPINNED

    def test_the_record_says_derived(self, route, catalog_rates):
        routed = route("simple", _config(derive=True))
        row = mr.build_outcome(routed)
        assert row["source"] == mr.SOURCE_DERIVED
        assert row["model_chosen"] == "fake-mini"
        assert "source" not in mr.build_outcome({**routed, "source": None})
