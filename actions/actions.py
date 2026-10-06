"""
Rasa custom actions for the EcoTravel Advisor (thin wrappers around services.py).

Every action that calls an external service is wrapped so that a failed API
call never crashes the conversation: services.py falls back to offline data
and these classes show a clear fallback message if nothing is available.

Custom JSON payloads (dispatcher json_message) drive the Streamlit UI:
  emission_cards | carousel | recommendation | alert | handover
"""

import json
import logging
from typing import Any, Dict, List, Text

from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.events import ActiveLoop, SlotSet
from rasa_sdk.executor import CollectingDispatcher

from actions import services

logger = logging.getLogger(__name__)

TOOLTIP_CO2E = (
    "CO2e = carbon dioxide equivalent: all greenhouse gases expressed as the "
    "amount of CO2 with the same warming effect. Figures are per person."
)
DATA_NOTICE = (
    "Hotel, experience and offset entries are illustrative demo data. "
    "Check any eco-label with the issuing body before booking."
)


# ------------------------------------------------------------------ helpers
def payload(intent: str, **entities: Any) -> str:
    """Build a button payload such as /inform_destination{"destination": "Lisbon"}."""
    return "/" + intent + (json.dumps(entities) if entities else "")


def button(title: str, intent: str, **entities: Any) -> Dict[str, str]:
    return {"title": title, "payload": payload(intent, **entities)}


def advisor_button() -> Dict[str, str]:
    return button("Talk to a human advisor", "request_human_advisor")


def trip_context(tracker: Tracker) -> Dict[str, Any]:
    nights = tracker.get_slot("trip_nights")
    return {
        "origin": tracker.get_slot("origin"),
        "destination": tracker.get_slot("destination"),
        "travel_date": tracker.get_slot("travel_date"),
        "trip_start": tracker.get_slot("trip_start"),
        "nights": int(nights) if nights else services.DEFAULT_NIGHTS,
        "budget": services.parse_budget(tracker.get_slot("budget")),
        "sustainability_level": tracker.get_slot("sustainability_level") or "medium",
    }


def load_cities(ctx: Dict[str, Any]):
    return services.resolve_city(ctx["origin"]), services.resolve_city(ctx["destination"])


def transcript_from(tracker: Tracker, limit: int = 30) -> List[Dict[str, str]]:
    lines = []
    for event in tracker.events:
        if event.get("event") in ("user", "bot") and event.get("text"):
            lines.append({"speaker": event["event"], "text": event["text"]})
    return lines[-limit:]


def perform_handover(dispatcher: CollectingDispatcher, tracker: Tracker, reason: str) -> List[Dict[Text, Any]]:
    """Package the full context and hand over. Used by the handover action and by stage 2 of the fallback."""
    ctx = trip_context(tracker)
    package = services.build_handover_package(tracker.sender_id, ctx, transcript_from(tracker), reason)
    try:
        ok = services.deliver_handover(package)
    except Exception as exc:  # noqa: BLE001 - handover must never crash the bot
        logger.error("Handover failed: %s", exc)
        ok = False
    if ok:
        dispatcher.utter_message(
            text=(
                f"I'm handing you over to a human travel advisor (reference {package['reference']}). "
                "They receive your trip details and our chat so you won't have to repeat yourself."
            ),
            json_message={"type": "handover", "status": "escalated", "reference": package["reference"], "reason": reason},
        )
        return [SlotSet("handover_status", "escalated"), SlotSet("clarification_attempts", 0.0),
                ActiveLoop(None), SlotSet("requested_slot", None)]
    dispatcher.utter_message(
        text=(
            "I couldn't reach the advisor queue just now. Please email advisors@ecotravel.example "
            "and mention this chat, and I'll keep your trip details here."
        ),
        json_message={"type": "handover", "status": "failed"},
    )
    return [SlotSet("handover_status", "failed")]


# --------------------------------------------------- dynamic quick replies
class ActionAskDestination(Action):
    def name(self) -> Text:
        return "action_ask_destination"

    def run(self, dispatcher, tracker, domain):
        buttons = [button(c, "inform_destination", destination=c) for c in services.popular_destinations()]
        dispatcher.utter_message(text="Where would you like to go? Pick a city or type another one.", buttons=buttons)
        return []


class ActionAskOrigin(Action):
    def name(self) -> Text:
        return "action_ask_origin"

    def run(self, dispatcher, tracker, domain):
        buttons = [button(c, "inform_origin", origin=c) for c in services.popular_origins()]
        dispatcher.utter_message(
            text="Which city will you travel from? (GPS is not used in this demo, so please pick or type your city.)",
            buttons=buttons,
        )
        return []


class ActionAskSustainabilityLevel(Action):
    def name(self) -> Text:
        return "action_ask_sustainability_level"

    def run(self, dispatcher, tracker, domain):
        buttons = [
            button("Price matters most", "inform_sustainability_level", sustainability_level="low"),
            button("Balanced", "inform_sustainability_level", sustainability_level="medium"),
            button("As green as possible", "inform_sustainability_level", sustainability_level="high"),
        ]
        dispatcher.utter_message(text="How important is a low carbon footprint for this trip?", buttons=buttons)
        return []


# ------------------------------------------------------------- form checks
class ValidateTripForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_form"

    @staticmethod
    def _same_city_check(dispatcher, city, other_slot_value):
        other = services.resolve_city(other_slot_value)
        if other and other["name"] == city["name"]:
            dispatcher.utter_message(text="Your start and destination are the same city. Please choose a different one.")
            return True
        return False

    def validate_destination(self, slot_value, dispatcher, tracker, domain):
        city = services.resolve_city(slot_value)
        if not city:
            dispatcher.utter_message(text=f"Sorry, I don't have verified data for \"{slot_value}\" yet.")
            return {"destination": None}
        if self._same_city_check(dispatcher, city, tracker.get_slot("origin")):
            return {"destination": None}
        return {"destination": city["name"], "clarification_attempts": 0.0}

    def validate_origin(self, slot_value, dispatcher, tracker, domain):
        city = services.resolve_city(slot_value)
        if not city:
            dispatcher.utter_message(text=f"Sorry, I don't know \"{slot_value}\" as a starting point yet.")
            return {"origin": None}
        if self._same_city_check(dispatcher, city, tracker.get_slot("destination")):
            return {"origin": None}
        return {"origin": city["name"], "clarification_attempts": 0.0}

    def validate_travel_date(self, slot_value, dispatcher, tracker, domain):
        info = services.parse_date(slot_value)
        if not info:
            dispatcher.utter_message(text="I couldn't read that date. Try for example 2027-03-15, 15 March, or next weekend.")
            return {"travel_date": None}
        if info["past"]:
            dispatcher.utter_message(text="That date is in the past. Please give me a future date.")
            return {"travel_date": None}
        return {
            "travel_date": info["text"],
            "trip_start": info["start"],
            "trip_nights": info["nights"] or services.DEFAULT_NIGHTS,
            "clarification_attempts": 0.0,
        }

    def validate_budget(self, slot_value, dispatcher, tracker, domain):
        value = services.parse_budget(slot_value)
        if value is None or value < 30:
            dispatcher.utter_message(text="Please give me your total budget in euros as a number, for example 900.")
            return {"budget": None}
        return {"budget": value, "clarification_attempts": 0.0}

    def validate_sustainability_level(self, slot_value, dispatcher, tracker, domain):
        level = services.normalise_level(slot_value)
        if not level:
            dispatcher.utter_message(text="Please choose one of the options: price first, balanced, or as green as possible.")
            return {"sustainability_level": None}
        return {"sustainability_level": level, "clarification_attempts": 0.0}


# ----------------------------------------------------------- result actions
class ActionFetchCarbonFootprint(Action):
    def name(self) -> Text:
        return "action_fetch_carbon_footprint"

    def run(self, dispatcher, tracker, domain):
        ctx = trip_context(tracker)
        origin, dest = load_cities(ctx)
        if not origin or not dest:
            dispatcher.utter_message(text="I need a valid start and destination first.", buttons=[button("Plan a trip", "plan_trip")])
            return []
        try:
            options = services.build_transport_options(origin, dest, ctx["trip_start"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Transport lookup failed: %s", exc)
            dispatcher.utter_message(text="I couldn't calculate the footprint just now.", buttons=[advisor_button()])
            return []

        lines = [f"Carbon footprint {origin['name']} to {dest['name']} (per person, one way). {TOOLTIP_CO2E}"]
        for o in options:
            lines.append(
                f"{o['label']}: {o['co2_kg']} kg CO2e ({o['band'].upper()} emission), about {o['hours']} h, "
                f"return fare about {o['price_return']} EUR. Source: {o['source']}."
            )
        dispatcher.utter_message(text="\n".join(lines), json_message={"type": "emission_cards", "items": options, "tooltip": TOOLTIP_CO2E})

        worst, best = max(options, key=lambda o: o["co2_kg"]), min(options, key=lambda o: o["co2_kg"])
        if worst["band"] == "red" and worst is not best:
            times = round(worst["co2_kg"] / best["co2_kg"]) if best["co2_kg"] else 0
            alert = (
                f"High-emission option: the {worst['label'].lower()} emits {worst['co2_kg']} kg CO2e one way"
                + (f", about {times} times the {best['label'].lower()}." if times > 1 else ".")
            )
            dispatcher.utter_message(text=alert, json_message={"type": "alert", "level": "high", "text": alert})
        return []


class ActionFetchEcoHotels(Action):
    def name(self) -> Text:
        return "action_fetch_eco_hotels"

    def run(self, dispatcher, tracker, domain):
        ctx = trip_context(tracker)
        dest = services.resolve_city(ctx["destination"])
        if not dest:
            dispatcher.utter_message(text="Tell me your destination first.", buttons=[button("Plan a trip", "plan_trip")])
            return []
        try:
            hotels = services.get_hotel_options(dest["name"], ctx["trip_start"], ctx["nights"], dest["iata"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Hotel lookup failed: %s", exc)
            hotels = []
        if not hotels:
            dispatcher.utter_message(text=f"I couldn't find verified eco-hotels for {dest['name']} right now.", buttons=[advisor_button()])
            return []
        ranked = services.rank_options(hotels, ctx["sustainability_level"], "price_per_night", "co2_kg_per_night")[:4]
        lines = [f"Stays in {dest['name']}, best match first ({ctx['nights']} nights assumed):"]
        for h in ranked:
            lines.append(
                f"{h['name']}: {h['price_per_night']} EUR/night, {h['co2_kg_per_night']} kg CO2e/night "
                f"({h['band'].upper()}), label: {h['eco_label']}."
            )
        lines.append(DATA_NOTICE)
        dispatcher.utter_message(text="\n".join(lines), json_message={"type": "carousel", "items": ranked})
        return []


class ActionRankOptions(Action):
    def name(self) -> Text:
        return "action_rank_options"

    def run(self, dispatcher, tracker, domain):
        ctx = trip_context(tracker)
        origin, dest = load_cities(ctx)
        if not origin or not dest:
            return []
        try:
            transport = services.build_transport_options(origin, dest, ctx["trip_start"])
            hotels = services.get_hotel_options(dest["name"], ctx["trip_start"], ctx["nights"], dest["iata"])
            plan = services.recommend_plan(transport, hotels, ctx["nights"], ctx["sustainability_level"], ctx["budget"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Ranking failed: %s", exc)
            plan = None
        if not plan:
            dispatcher.utter_message(text="I couldn't build a recommendation for this trip.", buttons=[advisor_button()])
            return []

        best = plan["best"]
        text = (
            f"My recommendation: {best['transport']['label']} plus {best['hotel']['name']} for {plan['nights']} nights. "
            f"Total about {best['price']} EUR and {best['co2_kg']} kg CO2e per person"
        )
        if plan["saving_kg"] > 0:
            text += f", which is {plan['saving_kg']} kg less than the highest-emission combination"
        text += "."
        if plan["over_budget"]:
            text += f" Note: every combination is above your budget of {ctx['budget']:.0f} EUR; an advisor can look for savings."
        dispatcher.utter_message(
            text=text,
            json_message={"type": "recommendation", "plan": plan, "level": ctx["sustainability_level"]},
            buttons=[advisor_button()] if plan["over_budget"] else None,
        )
        return [SlotSet("trip_co2_kg", best["co2_kg"])]


class ActionShowOffsets(Action):
    def name(self) -> Text:
        return "action_show_offsets"

    def run(self, dispatcher, tracker, domain):
        co2 = tracker.get_slot("trip_co2_kg")
        if not co2:
            dispatcher.utter_message(text="Plan a trip first and I'll price offsets for its footprint.", buttons=[button("Plan a trip", "plan_trip")])
            return []
        lines = [f"Offsetting about {co2} kg CO2e would cost roughly:"]
        for q in services.offset_quotes(float(co2)):
            lines.append(f"{q['name']} ({q['type']}): about {q['cost_eur']} EUR.")
        lines.append("Offsets are a last step. Reducing emissions first (for example by train) is more reliable than compensating afterwards. " + DATA_NOTICE)
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionFetchExperiences(Action):
    def name(self) -> Text:
        return "action_fetch_experiences"

    def run(self, dispatcher, tracker, domain):
        dest = services.resolve_city(tracker.get_slot("destination"))
        if not dest:
            buttons = [button(c, "ask_experiences", destination=c) for c in services.popular_destinations()]
            dispatcher.utter_message(text="Which destination should I look at?", buttons=buttons)
            return []
        items = services.experiences_for(dest["name"])
        if not items:
            dispatcher.utter_message(text=f"I have no local experiences listed for {dest['name']} yet.", buttons=[advisor_button()])
            return []
        lines = [f"Community-friendly experiences in {dest['name']}:"]
        for e in items:
            lines.append(f"{e['name']}: {e['price']} EUR, {e['co2_kg']} kg CO2e. {e['community_benefit']}")
        lines.append(DATA_NOTICE)
        dispatcher.utter_message(text="\n".join(lines))
        return []


# --------------------------------------------------- handover and fallback
class ActionHumanHandover(Action):
    def name(self) -> Text:
        return "action_human_handover"

    def run(self, dispatcher, tracker, domain):
        return perform_handover(dispatcher, tracker, reason="user_requested")


class ActionDefaultFallback(Action):
    """Two-stage clarification: stage 1 re-asks with quick replies, stage 2 escalates."""

    def name(self) -> Text:
        return "action_default_fallback"

    def run(self, dispatcher, tracker, domain):
        attempts = int(float(tracker.get_slot("clarification_attempts") or 0)) + 1
        if attempts >= 2:
            return perform_handover(dispatcher, tracker, reason="repeated_nlu_fallback")
        dispatcher.utter_message(
            text="Sorry, I didn't understand that. What would you like to do?",
            buttons=[
                button("Plan a trip", "plan_trip"),
                button("Compare transport emissions", "ask_carbon_footprint"),
                advisor_button(),
            ],
        )
        return [SlotSet("clarification_attempts", float(attempts))]
