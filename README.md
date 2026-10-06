# EcoTravel Advisor (Rasa 3.6)

A Rasa chatbot that plans lower-carbon trips: asks for destination, start city,
dates, budget and sustainability level, then shows transport emissions
(green / amber / red), eco-hotel carousel, a recommended plan, offsets,
local experiences, and hands over to a human advisor with full context.

## 1. Set up (Windows PowerShell, Python 3.10)

1. `cd` into this folder.
2. `py -3.10 -m venv .venv-rasa`
3. `.\.venv-rasa\Scripts\Activate.ps1`
4. `pip install -r requirements.txt`
5. `python -m spacy download en_core_web_md`
   (if you get a 404, install the model wheel you already have)
6. `copy .env.example .env` (keys are optional, see section 6)
7. `rasa train`

## 2. Run the bot (3 windows)

Easy way: `.\scripts\start_local.ps1`

Manual way:
1. Window 1 (venv `.venv-rasa`): `rasa run actions`
2. Window 2 (venv `.venv-rasa`): `rasa run --enable-api --cors "*"`
3. Window 3 (second venv):
   `py -3.10 -m venv .venv-ui` then `.\.venv-ui\Scripts\Activate.ps1`
   then `pip install -r requirements-ui.txt` then `streamlit run frontend/app.py`
4. Open http://localhost:8501

Quick text test without the UI: `rasa shell` (needs window 1 running).

## 3. Run the tests (outputs for your report)

Keep `rasa run actions` running in another window for step 3 and 4.

1. Check the data: `rasa data validate`
2. Unit tests (no internet needed): `pytest tests -v`
3. Dialogue tests: `rasa test core --stories tests/test_stories.yml`
   (results go to `results/`: failed_test_stories.yml, confusion matrix)
4. NLU 80/20 split:
   `rasa data split nlu` then `rasa test nlu --nlu train_test_split/test_data.yml`
5. NLU cross-validation:
   `rasa test nlu --nlu data/nlu.yml --cross-validation --folds 5`
   (intent_confusion_matrix.png, intent_histogram.png, reports in `results/`)
6. Latency (needs Rasa + actions running): `python tools/latency_check.py --runs 5`

## 4. Optional DistilBERT comparison

1. `pip install "rasa[transformers]==3.6.*"`
2. `rasa train --config config_distilbert.yml --fixed-model-name distilbert`
3. `rasa shell -m models/distilbert.tar.gz`, then run the latency tool again.

The brief names `HFTransformersNLP`; that component only exists in Rasa 2.x.
In Rasa 3.x the same thing is `LanguageModelFeaturizer`.

## 5. Docker and Hugging Face

Docker Compose:
1. `copy .env.example .env`
2. `docker compose up --build`
3. UI: http://localhost:8501 (the action server is not exposed to the host)

Hugging Face Spaces (Docker SDK, free):
1. Create a new Space, SDK = Docker.
2. Copy the whole project into the Space repo.
3. Copy `deploy/huggingface_README_header.md` over the Space `README.md`.
4. Rename `Dockerfile.hf` to `Dockerfile` (replace the existing one).
5. Add your API keys under Settings > Secrets (never commit `.env`).
6. Push. The first build takes several minutes (it trains the model).

## 6. API keys and honesty about data

- **Climatiq** (carbon): free key. Without it, offline factors are used and the
  card says "Offline estimate". The activity IDs in `actions/services.py`
  (`DEFAULT_CLIMATIQ_IDS`) must be checked in the Climatiq explorer; override
  with env vars `CLIMATIQ_ID_FLIGHT`, `_TRAIN`, `_COACH`, `_CAR`.
- **Amadeus sandbox**: free keys. Used for flight fares and extra hotel prices.
  Amadeus has no eco data, so those hotels are shown as "Not verified".
- **Eco hotels, experiences, offsets** are fictional demo data
  (names end with "(demo)"). No free API exists for eco-certification.
  Never present them as real certifications.

## 7. Project map (brief requirement to file)

| Brief item | Where |
|---|---|
| Trip intake form, multi-turn | `domain.yml` (trip_form), `data/rules.yml`, `ValidateTripForm` |
| Climatiq carbon per mode | `services.estimate_emissions` |
| Amadeus hotels / flights | `services.amadeus_*` |
| Weighted scoring | `services.rank_options`, `recommend_plan` |
| Human handover with context | `perform_handover`, `services.build_handover_package` |
| Two-stage fallback | `ActionDefaultFallback` + `FallbackClassifier` in `config.yml` |
| Quick replies from actions | `ActionAsk*` classes |
| Colour cards, carousel, alert, handover indicator | `frontend/app.py` |
| GDPR | in-memory tracker, `utter_privacy`, "delete my data", handover only on request |
| Accessibility | text twin of every card, colour + label, no colour-only meaning |
| Latency < 3 s | 2.5 s API timeouts, parallel calls, cache, `tools/latency_check.py` |

Handover packages are written to `handover_queue/` (or POSTed to
`HANDOVER_WEBHOOK_URL`). Open one to screenshot the "full context" for the report.

## 8. Not included (optional in the brief)

Voice input, multiple languages and GPS. Location is entered manually.
