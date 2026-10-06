"""Tests for the Rasa action wrappers. Needs rasa-sdk (installed with requirements.txt)."""
import pytest

pytest.importorskip("rasa_sdk")

from rasa_sdk import Tracker  # noqa: E402
from rasa_sdk.executor import CollectingDispatcher  # noqa: E402

from actions import actions as a  # noqa: E402
from actions import services  # noqa: E402

SLOTS = {
    "destination": "Lisbon", "origin": "Berlin", "travel_date": "2030-05-10",
    "trip_start": "2030-05-10", "trip_nights": 3, "budget": 900.0,
    "sustainability_level": "high", "clarification_attempts": 0.0,
}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for key in ("CLIMATIQ_API_KEY", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET", "HANDOVER_WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HANDOVER_DIR", str(tmp_path / "queue"))
    services._EMISSION_CACHE.clear()


def make_tracker(slots=None, events=None):
    return Tracker.from_dict({
        "sender_id": "test-user",
        "slots": slots if slots is not None else dict(SLOTS),
        "latest_message": {"intent": {"name": "greet"}, "entities": [], "text": "hi"},
        "events": events or [],
        "paused": False,
        "followup_action": None,
        "active_loop": {},
        "latest_action_name": None,
    })


def custom_types(dispatcher):
    return [m.get("custom", {}).get("type") for m in dispatcher.messages if m.get("custom")]


def test_carbon_footprint_cards_and_alert():
    d = CollectingDispatcher()
    a.ActionFetchCarbonFootprint().run(d, make_tracker(), {})
    types = custom_types(d)
    assert "emission_cards" in types and "alert" in types
    cards = next(m["custom"] for m in d.messages if m.get("custom", {}).get("type") == "emission_cards")
    assert {c["band"] for c in cards["items"]} <= {"green", "amber", "red"}


def test_hotels_carousel():
    d = CollectingDispatcher()
    a.ActionFetchEcoHotels().run(d, make_tracker(), {})
    assert "carousel" in custom_types(d)


def test_rank_sets_co2_slot():
    d = CollectingDispatcher()
    events = a.ActionRankOptions().run(d, make_tracker(), {})
    assert "recommendation" in custom_types(d)
    assert events and events[0]["name"] == "trip_co2_kg" and events[0]["value"] > 0


def test_missing_slots_do_not_crash():
    d = CollectingDispatcher()
    assert a.ActionFetchCarbonFootprint().run(d, make_tracker(slots={}), {}) == []
    assert a.ActionFetchEcoHotels().run(d, make_tracker(slots={}), {}) == []
    assert a.ActionRankOptions().run(d, make_tracker(slots={}), {}) == []
    assert a.ActionShowOffsets().run(d, make_tracker(slots={}), {}) == []


def test_experiences_and_offsets():
    d = CollectingDispatcher()
    a.ActionFetchExperiences().run(d, make_tracker(), {})
    assert "Lisbon" in d.messages[-1]["text"]
    d2 = CollectingDispatcher()
    a.ActionShowOffsets().run(d2, make_tracker(dict(SLOTS, trip_co2_kg=120.0)), {})
    assert "EUR" in d2.messages[-1]["text"]


def test_handover_packages_context(tmp_path):
    events = [{"event": "user", "text": "I need help"}, {"event": "bot", "text": "Sure"}]
    d = CollectingDispatcher()
    out = a.ActionHumanHandover().run(d, make_tracker(events=events), {})
    msg = next(m for m in d.messages if m.get("custom"))
    assert msg["custom"]["status"] == "escalated"
    files = list((tmp_path / "queue").glob("ECO-*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "Lisbon" in text and "I need help" in text
    assert any(e.get("name") == "handover_status" and e.get("value") == "escalated" for e in out)


def test_handover_failure_message(monkeypatch):
    monkeypatch.setattr(services, "deliver_handover", lambda p: False)
    d = CollectingDispatcher()
    a.ActionHumanHandover().run(d, make_tracker(), {})
    assert d.messages[-1]["custom"]["status"] == "failed"


def test_fallback_two_stages():
    d1 = CollectingDispatcher()
    ev1 = a.ActionDefaultFallback().run(d1, make_tracker(dict(SLOTS, clarification_attempts=0.0)), {})
    assert d1.messages[-1]["buttons"] and ev1[0]["value"] == 1.0
    d2 = CollectingDispatcher()
    a.ActionDefaultFallback().run(d2, make_tracker(dict(SLOTS, clarification_attempts=1.0)), {})
    assert d2.messages[-1]["custom"]["type"] == "handover"


def test_dynamic_buttons():
    d = CollectingDispatcher()
    a.ActionAskDestination().run(d, make_tracker(), {})
    btn = d.messages[-1]["buttons"][0]
    assert btn["payload"].startswith('/inform_destination{"destination":')


def run_validator(name, value, slots=None):
    d = CollectingDispatcher()
    out = getattr(a.ValidateTripForm(), name)(value, d, make_tracker(slots), {})
    return out, d


def test_validators():
    assert run_validator("validate_destination", "to lisboa", {})[0]["destination"] == "Lisbon"
    assert run_validator("validate_destination", "Atlantis", {})[0] == {"destination": None}
    assert run_validator("validate_destination", "Berlin", {"origin": "Berlin"})[0] == {"destination": None}
    assert run_validator("validate_origin", "Hamburg", {})[0]["origin"] == "Hamburg"
    assert run_validator("validate_travel_date", "2030-05-10")[0]["trip_start"] == "2030-05-10"
    assert run_validator("validate_travel_date", "2020-01-01")[0] == {"travel_date": None}
    assert run_validator("validate_travel_date", "whenever")[0] == {"travel_date": None}
    assert run_validator("validate_budget", "1,200 euros")[0]["budget"] == 1200.0
    assert run_validator("validate_budget", "lots")[0] == {"budget": None}
    assert run_validator("validate_sustainability_level", "as green as possible")[0]["sustainability_level"] == "high"
    assert run_validator("validate_sustainability_level", "banana")[0] == {"sustainability_level": None}
