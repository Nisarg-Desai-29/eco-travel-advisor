"""Unit tests for the pure-Python logic. No Rasa and no internet needed."""
from datetime import date

import pytest
import requests

from actions import services as s

TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for key in ("CLIMATIQ_API_KEY", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET", "HANDOVER_WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HANDOVER_DIR", str(tmp_path / "queue"))
    s._EMISSION_CACHE.clear()


# ---- cities
@pytest.mark.parametrize("text,expected", [
    ("Lisbon", "Lisbon"), ("to lisboa please", "Lisbon"), ("LISBON!", "Lisbon"),
    ("costa rica", "San Jose"), ("Lisbn", "Lisbon"), ("München", "Munich"),
])
def test_resolve_city_ok(text, expected):
    assert s.resolve_city(text)["name"] == expected


@pytest.mark.parametrize("text", ["", None, "Atlantis", "asdfgh"])
def test_resolve_city_unknown(text):
    assert s.resolve_city(text) is None


def test_distance_berlin_lisbon():
    d = s.city_distance_km(s.resolve_city("Berlin"), s.resolve_city("Lisbon"))
    assert 2300 < d < 2400


# ---- parsing
@pytest.mark.parametrize("text,value", [
    ("900", 900.0), ("1,200 euros", 1200.0), ("1.200", 1200.0), ("€750", 750.0),
    ("1.5k", 1500.0), ("2k", 2000.0), ("1200.50", 1200.5), (450, 450.0),
])
def test_parse_budget_ok(text, value):
    assert s.parse_budget(text) == value


@pytest.mark.parametrize("text", ["lots", "", None, "0", -5])
def test_parse_budget_bad(text):
    assert s.parse_budget(text) is None


def test_parse_date_variants():
    assert s.parse_date("2027-03-15", TODAY)["start"] == "2027-03-15"
    assert s.parse_date("15/03/2027", TODAY)["start"] == "2027-03-15"
    assert s.parse_date("15 March", TODAY)["start"] == "2027-03-15"
    assert s.parse_date("March 2027", TODAY)["start"] == "2027-03-01"
    r = s.parse_date("from 10 to 15 December", TODAY)
    assert r["start"] == "2026-12-10" and r["nights"] == 5
    assert s.parse_date("next weekend", TODAY)["nights"] == 2
    assert s.parse_date("tomorrow", TODAY)["start"] == "2026-10-06"


def test_parse_date_past_and_junk():
    assert s.parse_date("2020-01-01", TODAY)["past"] is True
    assert s.parse_date("whenever", TODAY) is None
    assert s.parse_date("2027-13-45", TODAY) is None


@pytest.mark.parametrize("text,level", [
    ("high", "high"), ("as green as possible", "high"), ("low priority", "low"),
    ("price matters more", "low"), ("balanced", "medium"), ("somewhere in between", "medium"),
    ("banana", None),
])
def test_normalise_level(text, level):
    assert s.normalise_level(text) == level


# ---- carbon
def test_offline_emissions_and_ordering():
    ber, lis = s.resolve_city("Berlin"), s.resolve_city("Lisbon")
    opts = s.build_transport_options(ber, lis)
    modes = [o["mode"] for o in opts]
    assert "flight" in modes and "train" in modes
    train = next(o for o in opts if o["mode"] == "train")
    flight = next(o for o in opts if o["mode"] == "flight")
    assert train["co2_kg"] < flight["co2_kg"]
    assert opts[0]["co2_kg"] <= opts[-1]["co2_kg"]
    assert flight["source"].startswith("Offline")


def test_long_haul_only_flight():
    opts = s.build_transport_options(s.resolve_city("Berlin"), s.resolve_city("Kyoto"))
    assert [o["mode"] for o in opts] == ["flight"]


def test_bands():
    assert s.emission_band(10) == "green"
    assert s.emission_band(100) == "amber"
    assert s.emission_band(400) == "red"


def test_climatiq_failure_falls_back(monkeypatch):
    monkeypatch.setenv("CLIMATIQ_API_KEY", "dummy")

    def boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(s.requests, "post", boom)
    r = s.estimate_emissions("train", 1000)
    assert r["source"].startswith("Offline") and r["co2_kg"] > 0


def test_climatiq_success(monkeypatch):
    monkeypatch.setenv("CLIMATIQ_API_KEY", "dummy")

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"co2e": 12.34}

    monkeypatch.setattr(s.requests, "post", lambda *a, **k: Resp())
    r = s.estimate_emissions("train", 777)
    assert r == {"co2_kg": 12.3, "source": "Climatiq API"}


def test_amadeus_failure_is_safe(monkeypatch):
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "x")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "y")

    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(s.requests, "post", boom)
    monkeypatch.setattr(s.requests, "get", boom)
    s._AMADEUS_TOKEN.update({"value": None, "expires": 0})
    assert s.amadeus_cheapest_flight("BER", "LIS", "2030-05-10") is None
    assert s.amadeus_market_hotels("LIS", "2030-05-10", 3) == []


# ---- ranking
def test_rank_changes_with_level():
    opts = [
        {"name": "cheap-dirty", "price": 50, "co2": 100},
        {"name": "dear-clean", "price": 200, "co2": 10},
    ]
    assert s.rank_options(opts, "high", "price", "co2")[0]["name"] == "dear-clean"
    assert s.rank_options(opts, "low", "price", "co2")[0]["name"] == "cheap-dirty"


def test_rank_edge_cases():
    assert s.rank_options([], "high", "price", "co2") == []
    same = [{"price": 10, "co2": 5}, {"price": 10, "co2": 5}]
    assert len(s.rank_options(same, "medium", "price", "co2")) == 2


def test_plan_respects_budget_and_flags_overbudget():
    ber, lis = s.resolve_city("Berlin"), s.resolve_city("Lisbon")
    t = s.build_transport_options(ber, lis)
    h = s.get_hotel_options("Lisbon", None, 3, None)
    plan = s.recommend_plan(t, h, 3, "high", 5000)
    assert plan["best"]["price"] <= 5000 and not plan["over_budget"]
    assert plan["best"]["transport"]["mode"] in ("train", "coach")
    tiny = s.recommend_plan(t, h, 3, "high", 50)
    assert tiny["over_budget"] is True
    assert s.recommend_plan([], h, 3, "high", None) is None


def test_every_city_has_hotels_and_experiences():
    for key, city in s.all_cities().items():
        assert len(s.load_json("eco_hotels.json")[key]) >= 3
        assert len(s.experiences_for(city["name"])) >= 3


def test_offsets():
    q = s.offset_quotes(500)
    assert len(q) == 3 and q[0]["cost_eur"] == round(0.5 * q[0]["price_per_tonne"], 2)


# ---- handover
def test_handover_file(tmp_path):
    pkg = s.build_handover_package("u1", {"destination": "Lisbon"}, [{"speaker": "user", "text": "hi"}], "user_requested")
    assert s.deliver_handover(pkg) is True
    files = list((tmp_path / "queue").glob("ECO-*.json"))
    assert len(files) == 1 and "Lisbon" in files[0].read_text()
