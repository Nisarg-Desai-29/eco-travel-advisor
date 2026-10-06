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

