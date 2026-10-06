"""
Business logic for the EcoTravel Advisor.

This module has NO Rasa imports on purpose: everything here can be unit
tested with plain pytest. actions.py is a thin Rasa wrapper around it.

Contents
  1. Data loading and city resolution
  2. Input parsing (budget, dates, sustainability level)
  3. Carbon estimates (Climatiq API with offline fallback)
  4. Amadeus sandbox (flight price, hotel prices) with offline fallback
  5. Weighted scoring / ranking
  6. Human-advisor handover packaging
"""

import concurrent.futures
import difflib
import json
import logging
import math
import os
import re
import uuid
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

MOCK_DIR = Path(__file__).resolve().parent.parent / "mock_data"

# (connect, read) seconds. Keeps the whole turn under the 3 s latency target.
REQUEST_TIMEOUT = (1.5, 2.5)

CLIMATIQ_URL = "https://api.climatiq.io/data/v1/estimate"
# Activity IDs must be checked in the Climatiq explorer for your data version.
# Override any of them with env vars CLIMATIQ_ID_FLIGHT / _TRAIN / _COACH / _CAR.
# If a call fails for ANY reason the bot silently uses the offline factors.
DEFAULT_CLIMATIQ_IDS = {
    "flight": "passenger_flight-route_type_outside_uk-aircraft_type_na-distance_na-class_economy-rf_included",
    "train": "passenger_train-route_type_international_rail-fuel_source_na",
    "coach": "passenger_vehicle-vehicle_type_bus-fuel_source_na-distance_na-engine_size_na",
    "car": "passenger_vehicle-vehicle_type_car-fuel_source_na-engine_size_medium-vehicle_age_na-vehicle_weight_na",
}

# Emission bands, kg CO2e per person, ONE WAY (transport) or PER NIGHT (hotel)
TRANSPORT_BANDS = (50.0, 150.0)  # <50 green, <150 amber, else red
HOTEL_BANDS = (12.0, 25.0)

SUSTAINABILITY_WEIGHTS = {"low": 0.25, "medium": 0.5, "high": 0.8}
DEFAULT_NIGHTS = 3
UNVERIFIED_HOTEL_CO2 = 22.0  # assumed kg CO2e/night when a hotel has no data

MODE_LABELS = {"flight": "Flight", "train": "Train", "coach": "Coach", "car": "Car"}
MODE_SPEED_KMH = {"flight": 700.0, "train": 110.0, "coach": 70.0, "car": 85.0}
MODE_FIXED_HOURS = {"flight": 3.0, "train": 0.5, "coach": 0.5, "car": 0.0}
# EUR per km (return fare is 2x). Flight also has a fixed part.
MODE_PRICE_PER_KM = {"flight": 0.09, "train": 0.14, "coach": 0.07, "car": 0.12}
FLIGHT_FIXED_PRICE = 40.0


# ---------------------------------------------------------------- 1. data
@lru_cache(maxsize=None)
def load_json(filename: str) -> Dict[str, Any]:
    with open(MOCK_DIR / filename, "r", encoding="utf-8") as handle:
        return json.load(handle)


def all_cities() -> Dict[str, Dict[str, Any]]:
    return load_json("cities.json")


def popular_destinations() -> List[str]:
    return ["Lisbon", "Barcelona", "Amsterdam", "Copenhagen", "Vienna", "Reykjavik"]


def popular_origins() -> List[str]:
    return ["Berlin", "Hamburg", "Munich", "London", "Paris"]


def resolve_city(text: Any) -> Optional[Dict[str, Any]]:
    """Map free text such as 'to Lisboa please' to a known city record."""
    if text is None:
        return None
    cleaned = re.sub(r"[^\w\s'-]", " ", str(text), flags=re.UNICODE).lower().strip()
    if not cleaned:
        return None
    cities = all_cities()
    lookup: Dict[str, str] = {}
    for key, city in cities.items():
        lookup[key] = key
        for alias in city.get("aliases", []):
            lookup[alias.lower()] = key
    if cleaned in lookup:
        return cities[lookup[cleaned]]
    # whole-word / phrase match inside a longer sentence (longest names first)
    for name in sorted(lookup, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", cleaned):
            return cities[lookup[name]]
    close = difflib.get_close_matches(cleaned, list(lookup), n=1, cutoff=0.82)
    if close:
        return cities[lookup[close[0]]]
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def city_distance_km(origin: Dict[str, Any], dest: Dict[str, Any]) -> float:
    return haversine_km(origin["lat"], origin["lon"], dest["lat"], dest["lon"])


# ------------------------------------------------------------- 2. parsing
def parse_budget(text: Any) -> Optional[float]:
    """'1,200 euros' -> 1200.0 ; '1.5k' -> 1500.0 ; 'abc' -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        value = float(text)
        return value if 0 < value < 1_000_000 else None
    match = re.search(r"(\d[\d.,]*)\s*(k\b)?", str(text).lower())
    if not match:
        return None
    number, k_suffix = match.group(1).rstrip(".,"), match.group(2)
    if "." in number and "," in number:
        decimal_sep = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        number = number.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in number or "." in number:
        sep = "," if "," in number else "."
        parts = number.split(sep)
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and not k_suffix):
            number = "".join(parts)  # thousands separator: 1,200 / 1.200
        else:
            number = parts[0] + "." + parts[1]
    try:
        value = float(number)
    except ValueError:
        return None
    if k_suffix:
        value *= 1000
    return value if 0 < value < 1_000_000 else None


MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


def _future_date(month: int, day: int, year: Optional[int], today: date) -> Optional[date]:
    try:
        if year:
            return date(year, month, day)
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def parse_date(text: Any, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    """
    Understand common ways of giving a travel date.
    Returns {"text", "start" (ISO or None), "nights" (int or None), "past" (bool)}
    or None if nothing usable was found.
    """
    if text is None:
        return None
    today = today or date.today()
    raw = str(text).strip()
    low = raw.lower()
    if not low:
        return None

    def result(start: date, nights: Optional[int], label: Optional[str] = None) -> Dict[str, Any]:
        return {
            "text": label or start.isoformat(),
            "start": start.isoformat(),
            "nights": nights,
            "past": start < today,
        }

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", low)
    if m:
        try:
            return result(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None)
        except ValueError:
            return None

    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", low)  # European d/m/Y
    if m:
        try:
            return result(date(int(m.group(3)), int(m.group(2)), int(m.group(1))), None)
        except ValueError:
            return None

    m = re.search(rf"\b(\d{{1,2}})\s*(?:-|to|until|till)\s*(\d{{1,2}})\s+(?:of\s+)?({_MONTH_RE})\b(?:\s+(\d{{4}}))?", low)
    if m:
        d1, d2, month = int(m.group(1)), int(m.group(2)), MONTHS[m.group(3)]
        start = _future_date(month, d1, int(m.group(4)) if m.group(4) else None, today)
        if start and d2 > d1:
            return result(start, d2 - d1, f"{start.isoformat()} ({d2 - d1} nights)")

    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b(?:\s+(\d{{4}}))?", low)
    if not m:
        m2 = re.search(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\d)(?:,?\s+(\d{{4}}))?", low)
        if m2:
            start = _future_date(MONTHS[m2.group(1)], int(m2.group(2)), int(m2.group(3)) if m2.group(3) else None, today)
            if start:
                return result(start, None)
    else:
        start = _future_date(MONTHS[m.group(2)], int(m.group(1)), int(m.group(3)) if m.group(3) else None, today)
        if start:
            return result(start, None)

    m = re.search(rf"\b({_MONTH_RE})\b(?:\s+(\d{{4}}))?", low)  # 'March 2027', 'in March'
    if m:
        month = MONTHS[m.group(1)]
        start = _future_date(month, 1, int(m.group(2)) if m.group(2) else None, today)
        if start:
            return result(start, None, f"{start.strftime('%B %Y')}")

    if "tomorrow" in low:
        return result(today + timedelta(days=1), None)
    if "weekend" in low:
        days_ahead = (5 - today.weekday()) % 7 or 7
        if "next" in low:
            days_ahead += 7 if days_ahead < 7 else 0
        return result(today + timedelta(days=days_ahead), 2, "next weekend")
    if "next month" in low:
        year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return result(date(year, month, 1), None, "next month")
    if "next week" in low:
        return result(today + timedelta(days=(7 - today.weekday())), None, "next week")
    return None


def normalise_level(text: Any) -> Optional[str]:
    """Map free text to 'low' | 'medium' | 'high'."""
    if text is None:
        return None
    low = str(text).lower().strip()
    if low in SUSTAINABILITY_WEIGHTS:
        return low
    if re.search(r"\b(not|n't|low|little|less|cheap|cheapest|price|cost|budget|don't mind|dont mind)\b", low):
        return "low"
    if re.search(r"\b(high|most|greenest|as green|eco|max|maximum|very|strict|zero|neutral|important)\b", low):
        return "high"
    if re.search(r"\b(medium|mid|balanced|balance|between|moderate|some|middle|both)\b", low):
        return "medium"
    return None


# ------------------------------------------------------- 3. carbon / Climatiq
def emission_band(value: float, bands=TRANSPORT_BANDS) -> str:
    if value < bands[0]:
        return "green"
    if value < bands[1]:
        return "amber"
    return "red"


_EMISSION_CACHE: Dict[Any, Dict[str, Any]] = {}


def _climatiq_estimate(mode: str, distance_km: float) -> Optional[float]:
    key = os.environ.get("CLIMATIQ_API_KEY", "").strip()
    if not key:
        return None
    activity = os.environ.get(f"CLIMATIQ_ID_{mode.upper()}", DEFAULT_CLIMATIQ_IDS.get(mode, ""))
    if not activity:
        return None
    params: Dict[str, Any] = {"distance": round(distance_km, 1), "distance_unit": "km"}
    if mode in ("flight", "train"):
        params["passengers"] = 1
    body = {
        "emission_factor": {
            "activity_id": activity,
            "data_version": os.environ.get("CLIMATIQ_DATA_VERSION", "^21"),
        },
        "parameters": params,
    }
    resp = requests.post(
        CLIMATIQ_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return float(resp.json()["co2e"])


def estimate_emissions(mode: str, distance_km: float) -> Dict[str, Any]:
    """kg CO2e for ONE passenger, ONE way. Climatiq first, offline factors second."""
    cache_key = (mode, round(distance_km))
    if cache_key in _EMISSION_CACHE:  # saves free-tier API calls (500 / month)
        return _EMISSION_CACHE[cache_key]
    try:
        co2 = _climatiq_estimate(mode, distance_km)
        if co2 is not None:
            out = {"co2_kg": round(co2, 1), "source": "Climatiq API"}
            _EMISSION_CACHE[cache_key] = out
            return out
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        logger.warning("Climatiq call failed for %s (%s). Using offline factors.", mode, exc)

    factors = load_json("emission_factors.json")
    if mode == "flight":
        factor = factors["flight_long"] if distance_km > 3700 else factors["flight_short"]
    else:
        factor = factors[mode]
    return {"co2_kg": round(factor * distance_km, 1), "source": "Offline estimate (approximate)"}


# -------------------------------------------------------- 4. Amadeus sandbox
_AMADEUS_TOKEN: Dict[str, Any] = {"value": None, "expires": 0.0}


def _amadeus_base() -> str:
    return os.environ.get("AMADEUS_BASE_URL", "https://test.api.amadeus.com")


def _amadeus_token() -> Optional[str]:
    cid = os.environ.get("AMADEUS_CLIENT_ID", "").strip()
    secret = os.environ.get("AMADEUS_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        return None
    now = datetime.now().timestamp()
    if _AMADEUS_TOKEN["value"] and _AMADEUS_TOKEN["expires"] > now:
        return _AMADEUS_TOKEN["value"]
    resp = requests.post(
        f"{_amadeus_base()}/v1/security/oauth2/token",
        data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    _AMADEUS_TOKEN["value"] = data["access_token"]
    _AMADEUS_TOKEN["expires"] = now + float(data.get("expires_in", 1500)) - 60
    return _AMADEUS_TOKEN["value"]


def amadeus_cheapest_flight(origin_iata: str, dest_iata: str, depart_iso: Optional[str]) -> Optional[float]:
    """Cheapest one-way fare in EUR from the Amadeus sandbox, or None."""
    if not depart_iso:
        return None
    try:
        token = _amadeus_token()
        if not token:
            return None
        resp = requests.get(
            f"{_amadeus_base()}/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode": origin_iata,
                "destinationLocationCode": dest_iata,
                "departureDate": depart_iso,
                "adults": 1,
                "currencyCode": "EUR",
                "max": 5,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        prices = [float(o["price"]["grandTotal"]) for o in resp.json().get("data", [])]
        return min(prices) if prices else None
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        logger.warning("Amadeus flight search failed (%s). Using estimated fare.", exc)
        return None


def amadeus_market_hotels(city_iata: str, check_in_iso: Optional[str], nights: int, limit: int = 2) -> List[Dict[str, Any]]:
    """Real sandbox hotels. They carry NO eco data, so they are flagged unverified."""
    if not check_in_iso:
        return []
    try:
        token = _amadeus_token()
        if not token:
            return []
        headers = {"Authorization": f"Bearer {token}"}
        listing = requests.get(
            f"{_amadeus_base()}/v1/reference-data/locations/hotels/by-city",
            headers=headers, params={"cityCode": city_iata}, timeout=REQUEST_TIMEOUT,
        )
        listing.raise_for_status()
        ids = [h["hotelId"] for h in listing.json().get("data", [])[:20]]
        if not ids:
            return []
        check_out = (date.fromisoformat(check_in_iso) + timedelta(days=nights)).isoformat()
        offers = requests.get(
            f"{_amadeus_base()}/v3/shopping/hotel-offers",
            headers=headers,
            params={"hotelIds": ",".join(ids), "checkInDate": check_in_iso, "checkOutDate": check_out,
                    "adults": 1, "currency": "EUR"},
            timeout=REQUEST_TIMEOUT,
        )
        offers.raise_for_status()
        found = []
        for item in offers.json().get("data", []):
            offer = item["offers"][0]["price"]
            if offer.get("currency", "EUR") != "EUR":
                continue
            found.append({
                "name": str(item["hotel"]["name"]).title(),
                "eco_label": "Not verified",
                "eco_verified": False,
                "price_per_night": round(float(offer["total"]) / max(nights, 1)),
                "co2_kg_per_night": UNVERIFIED_HOTEL_CO2,
                "co2_estimated": True,
                "rating": None,
                "source": "Amadeus sandbox (price) / assumed CO2",
            })
            if len(found) >= limit:
                break
        return found
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError) as exc:
        logger.warning("Amadeus hotel search failed (%s). Using demo data only.", exc)
        return []


# ---------------------------------------------------- transport + hotel options
def build_transport_options(origin: Dict[str, Any], dest: Dict[str, Any], depart_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """All realistic ways to get from origin to dest, with carbon, price, time."""
    straight = city_distance_km(origin, dest)
    same_continent = origin["continent"] == dest["continent"]
    europe = same_continent and origin["continent"] == "Europe"
    factors = load_json("emission_factors.json")["route_factor"]

    modes = []
    if straight >= 200:
        modes.append("flight")
    if europe and straight <= 2500:
        modes.append("train")
    if europe and straight <= 2000:
        modes.append("coach")
    if same_continent and straight <= 2500:
        modes.append("car")
    if not modes:
        modes = ["flight"]

    distances = {m: straight * factors[m] for m in modes}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        estimates = dict(zip(modes, pool.map(lambda m: estimate_emissions(m, distances[m]), modes)))
        flight_fare = None
        if "flight" in modes:
            flight_fare = pool.submit(amadeus_cheapest_flight, origin["iata"], dest["iata"], depart_iso).result()

    options = []
    for mode in modes:
        km = distances[mode]
        price_one_way = MODE_PRICE_PER_KM[mode] * km + (FLIGHT_FIXED_PRICE if mode == "flight" else 0.0)
        price_source = "Estimated fare"
        if mode == "flight" and flight_fare:
            price_one_way, price_source = flight_fare, "Amadeus sandbox fare"
        hours = km / MODE_SPEED_KMH[mode] + MODE_FIXED_HOURS[mode]
        co2 = estimates[mode]["co2_kg"]
        options.append({
            "mode": mode,
            "label": MODE_LABELS[mode],
            "distance_km": round(km),
            "co2_kg": co2,
            "co2_return_kg": round(co2 * 2, 1),
            "price_one_way": round(price_one_way),
            "price_return": round(price_one_way * 2),
            "hours": round(hours, 1),
            "band": emission_band(co2),
            "source": estimates[mode]["source"],
            "price_source": price_source,
        })
    return sorted(options, key=lambda o: o["co2_kg"])


def get_hotel_options(dest_key: str, check_in_iso: Optional[str], nights: int, iata: Optional[str]) -> List[Dict[str, Any]]:
    hotels = [dict(h, source="Demo dataset", co2_estimated=False) for h in load_json("eco_hotels.json").get(dest_key.lower(), [])]
    if iata:
        hotels += amadeus_market_hotels(iata, check_in_iso, nights)
    for h in hotels:
        h["band"] = emission_band(h["co2_kg_per_night"], HOTEL_BANDS)
    return hotels


# ------------------------------------------------------------- 5. ranking
def _normalise(values: List[float]) -> List[float]:
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


def rank_options(options: List[Dict[str, Any]], level: str, price_key: str, co2_key: str) -> List[Dict[str, Any]]:
    """Weighted score in [0, 1], higher is better. Carbon weight rises with the user's level."""
    if not options:
        return []
    w_carbon = SUSTAINABILITY_WEIGHTS.get(level, 0.5)
    carbon_n = _normalise([float(o[co2_key]) for o in options])
    price_n = _normalise([float(o[price_key]) for o in options])
    ranked = []
    for opt, c, p in zip(options, carbon_n, price_n):
        score = w_carbon * (1 - c) + (1 - w_carbon) * (1 - p)
        ranked.append({**opt, "score": round(score, 3)})
    return sorted(ranked, key=lambda o: o["score"], reverse=True)


def recommend_plan(
    transport: List[Dict[str, Any]],
    hotels: List[Dict[str, Any]],
    nights: int,
    level: str,
    budget: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Pick the best transport + hotel combination for carbon, price and budget."""
    if not transport or not hotels:
        return None
    combos = []
    for t in transport:
        for h in hotels:
            combos.append({
                "transport": t,
                "hotel": h,
                "price": round(t["price_return"] + h["price_per_night"] * nights),
                "co2_kg": round(t["co2_return_kg"] + h["co2_kg_per_night"] * nights, 1),
            })
    within = [c for c in combos if budget is None or c["price"] <= budget]
    over_budget = bool(budget is not None and not within)
    pool = within or combos
    ranked = rank_options(pool, level, "price", "co2_kg")
    baseline = max(combos, key=lambda c: c["co2_kg"])
    best = ranked[0]
    return {
        "best": best,
        "alternatives": ranked[1:3],
        "over_budget": over_budget,
        "nights": nights,
        "baseline_co2_kg": baseline["co2_kg"],
        "saving_kg": round(max(baseline["co2_kg"] - best["co2_kg"], 0.0), 1),
    }


def offset_quotes(co2_kg: float) -> List[Dict[str, Any]]:
    quotes = []
    for p in load_json("offsets.json")["programmes"]:
        quotes.append({**p, "cost_eur": round(co2_kg / 1000.0 * p["price_per_tonne"], 2)})
    return quotes


def experiences_for(dest_key: str) -> List[Dict[str, Any]]:
    return load_json("experiences.json").get(dest_key.lower(), [])


# --------------------------------------------------------------- 6. handover
def build_handover_package(sender_id: str, slots: Dict[str, Any], transcript: List[Dict[str, str]], reason: str) -> Dict[str, Any]:
    return {
        "reference": "ECO-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4].upper(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "sender_id": sender_id,
        "reason": reason,
        "trip": slots,
        "transcript": transcript,
    }


def deliver_handover(package: Dict[str, Any]) -> bool:
    """
    Store the package in HANDOVER_DIR (default ./handover_queue) and, if
    HANDOVER_WEBHOOK_URL is set, POST it there too (CRM / helpdesk stand-in).
    Returns True when at least one channel accepted it.
    """
    delivered = False
    try:
        folder = Path(os.environ.get("HANDOVER_DIR", "handover_queue"))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{package['reference']}.json").write_text(
            json.dumps(package, indent=2, default=str), encoding="utf-8"
        )
        delivered = True
    except OSError as exc:
        logger.error("Could not write handover file: %s", exc)
    hook = os.environ.get("HANDOVER_WEBHOOK_URL", "").strip()
    if hook:
        try:
            requests.post(hook, json=package, timeout=REQUEST_TIMEOUT).raise_for_status()
            delivered = True
        except requests.RequestException as exc:
            logger.error("Handover webhook failed: %s", exc)
    return delivered
