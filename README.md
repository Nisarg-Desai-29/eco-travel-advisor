# EcoTravel Advisor — Rasa Sustainable Trip Planning Chatbot

## What this is
A Rasa Open Source chatbot that helps travellers plan lower-carbon trips:
carbon footprint estimates per transport mode, eco-certified hotel
suggestions, weighted ranking of options, and escalation to a human travel
advisor with full conversation context.

## Project layout
```
domain.yml, config.yml          Rasa Core/NLU configuration
data/nlu.yml, stories.yml, rules.yml   Training data
actions/actions.py              Custom action server (Climatiq/Amadeus + fallback)
mock_data/                      Offline static data used when no API key is set
tests/test_actions.py           Pytest unit tests (16 tests, all passing offline)
frontend/src/ChatWidget.jsx     Minimal React chat UI, Rasa REST channel
Dockerfile, docker-compose.yml  Containerised deployment
```

## Local setup
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real API keys, or leave blank to use mock data

rasa train
rasa run actions &          # starts the action server on :5055
rasa shell                  # or: rasa run --enable-api --cors "*"
```

## Running the unit tests
```bash
pytest tests/test_actions.py -v
```
16/16 tests pass without any live API keys, because `CarbonCalculator` and
`HotelFetcher` fall back to `mock_data/*.json` whenever a request fails or
no key is configured.

## Rasa-native testing
```bash
rasa test nlu --nlu data/nlu.yml --cross-validation
rasa test core --stories tests/test_stories.yml
```

## Docker deployment
```bash
docker compose up --build
```
Rasa Core → `localhost:5005`, Action server → `localhost:5055`,
React frontend → `localhost:3000`.

