"""
Streamlit chat UI for the EcoTravel Advisor (talks to the Rasa REST channel).

Run:  streamlit run frontend/app.py
Env:  RASA_URL (default http://localhost:5005)

UI elements required by the brief, all driven by real bot data:
  * quick-reply buttons   (from `buttons` returned by custom actions)
  * colour-coded cards    (green / amber / red from the Climatiq-based carbon score)
  * hotel carousel
  * high-emission alert
  * human handover indicator
Colours are always paired with a text label (LOW / MODERATE / HIGH) so the
meaning does not depend on colour alone, and every card has a plain-text
twin in the chat message for screen readers.
"""

import html
import os
import time
import uuid

import requests
import streamlit as st

RASA_URL = os.environ.get("RASA_URL", "http://localhost:5005").rstrip("/")
REST = f"{RASA_URL}/webhooks/rest/webhook"

BANDS = {
    "green": {"bg": "#E8F5E9", "fg": "#1B5E20", "label": "LOW emission"},
    "amber": {"bg": "#FFF3E0", "fg": "#8A4B00", "label": "MODERATE emission"},
    "red": {"bg": "#FFEBEE", "fg": "#B71C1C", "label": "HIGH emission"},
}

st.set_page_config(page_title="EcoTravel Advisor", page_icon="🌍", layout="centered")


# ------------------------------------------------------------------ state
def new_session():
    st.session_state.sender = "web-" + uuid.uuid4().hex[:10]
    st.session_state.messages = []
    st.session_state.handover = None
    st.session_state.latency = []
    st.session_state.error = None


if "sender" not in st.session_state:
    new_session()


def send(payload: str, shown: str = None):
    """Send a message (typed text or a button payload) and store the replies."""
    st.session_state.messages.append({"role": "user", "text": shown or payload})
    started = time.perf_counter()
    try:
        resp = requests.post(REST, json={"sender": st.session_state.sender, "message": payload}, timeout=30)
        resp.raise_for_status()
        replies = resp.json()
        st.session_state.error = None
    except (requests.RequestException, ValueError) as exc:
        st.session_state.error = f"Cannot reach the bot at {RASA_URL} ({exc.__class__.__name__}). Is Rasa running?"
        return
    st.session_state.latency.append(time.perf_counter() - started)
    for reply in replies:
        custom = reply.get("custom") or {}
        if custom.get("type") == "handover" and custom.get("status") == "escalated":
            st.session_state.handover = custom.get("reference")
        st.session_state.messages.append({
            "role": "bot",
            "text": reply.get("text"),
            "buttons": reply.get("buttons") or [],
            "custom": custom,
        })


# -------------------------------------------------------------- renderers
def badge(band: str) -> str:
    b = BANDS.get(band, BANDS["amber"])
    return (f"<span style='background:{b['fg']};color:#fff;padding:2px 8px;border-radius:10px;"
            f"font-size:0.75rem;font-weight:600'>{b['label']}</span>")


def card(inner: str, band: str) -> str:
    b = BANDS.get(band, BANDS["amber"])
    return (f"<div role='group' style='background:{b['bg']};color:{b['fg']};border-left:8px solid {b['fg']};"
            f"border-radius:8px;padding:10px 14px;margin:6px 0'>{inner}</div>")


def render_custom(custom: dict):
    kind = custom.get("type")
    if kind == "emission_cards":
        st.caption(f"ℹ️ {custom.get('tooltip', '')}")
        for o in custom["items"]:
            inner = (
                f"<b>{html.escape(o['label'])}</b> {badge(o['band'])}<br>"
                f"<span style='font-size:1.4rem;font-weight:700'>{o['co2_kg']} kg CO₂e</span> one way "
                f"({o['co2_return_kg']} kg return)<br>"
                f"≈ {o['hours']} h · return fare ≈ €{o['price_return']}<br>"
                f"<small>Emissions: {html.escape(o['source'])} · Price: {html.escape(o['price_source'])}</small>"
            )
            st.markdown(card(inner, o["band"]), unsafe_allow_html=True)
    elif kind == "alert":
        st.markdown(card(f"⚠️ <b>{html.escape(custom['text'])}</b>", "red"), unsafe_allow_html=True)
    elif kind == "carousel":
        items = custom["items"]
        cols = st.columns(min(len(items), 2))
        for i, h in enumerate(items):
            verified = "✔ Eco label (demo)" if h.get("eco_verified") else "✖ Not verified"
            rating = f"★ {h['rating']}" if h.get("rating") else ""
            inner = (
                f"<b>{html.escape(h['name'])}</b><br>{badge(h['band'])}<br>"
                f"€{h['price_per_night']}/night · {h['co2_kg_per_night']} kg CO₂e/night<br>"
                f"{html.escape(h['eco_label'])} {rating}<br><small>{verified} · score {h['score']}</small>"
            )
            cols[i % len(cols)].markdown(card(inner, h["band"]), unsafe_allow_html=True)
    elif kind == "recommendation":
        plan = custom["plan"]
        best = plan["best"]
        band = best["transport"]["band"]
        inner = (
            f"<b>Recommended plan</b><br>{html.escape(best['transport']['label'])} + "
            f"{html.escape(best['hotel']['name'])}<br>"
            f"<span style='font-size:1.3rem;font-weight:700'>≈ €{best['price']} · {best['co2_kg']} kg CO₂e</span> per person<br>"
            f"<small>Saves {plan['saving_kg']} kg versus the highest-emission combination. "
            f"Weighting: {html.escape(custom.get('level', 'medium'))} sustainability priority.</small>"
        )
        st.markdown(card(inner, band), unsafe_allow_html=True)
    elif kind == "handover":
        if custom.get("status") == "escalated":
            st.success(f"🤝 Handed over to a human advisor · reference {custom.get('reference')}")
        else:
            st.warning("Handover could not be delivered. Please use the e-mail address in the message.")


# ------------------------------------------------------------------- page
with st.sidebar:
    st.header("EcoTravel Advisor")
    st.markdown(
        "**Colour guide**\n\n"
        "🟢 LOW: under 50 kg CO₂e one way\n\n🟠 MODERATE: 50 to 150 kg\n\n🔴 HIGH: over 150 kg"
    )
    st.caption("Footprints are estimates. Hotels, experiences and offsets are demo data.")
    st.caption("Privacy: the chat is kept in memory only. Type “delete my data” to clear it.")
    if st.session_state.latency:
        last = st.session_state.latency[-1]
        st.metric("Last response time", f"{last:.2f} s", delta="OK" if last < 3 else "slow", delta_color="off")
    if st.button("Start a new chat"):
        new_session()
        st.rerun()

st.title("🌍 EcoTravel Advisor")
if st.session_state.handover:
    st.info(f"🤝 **Human advisor engaged** · reference {st.session_state.handover}")

if st.session_state.error:
    st.error(st.session_state.error)

if not st.session_state.messages:
    send("/greet", shown="Hi")

typed = st.chat_input("Type your message…")
if typed:
    send(typed)

last_bot_index = max((i for i, m in enumerate(st.session_state.messages) if m["role"] == "bot"), default=-1)
for idx, msg in enumerate(st.session_state.messages):
    with st.chat_message("user" if msg["role"] == "user" else "assistant"):
        if msg.get("text"):
            st.write(msg["text"].replace("\n", "  \n"))
        if msg.get("custom"):
            render_custom(msg["custom"])
        buttons = msg.get("buttons") or []
        if buttons:
            cols = st.columns(min(len(buttons), 3))
            for j, b in enumerate(buttons):
                cols[j % len(cols)].button(
                    b["title"], key=f"btn-{idx}-{j}", disabled=idx != last_bot_index,
                    on_click=send, args=(b["payload"], b["title"]),
                )
