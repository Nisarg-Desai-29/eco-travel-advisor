#!/usr/bin/env bash
# Starts the action server, Rasa and the Streamlit UI in one container.
set -e
rasa run actions --port 5055 &
rasa run --enable-api --cors "*" --endpoints endpoints.docker.yml --port 5005 &
exec streamlit run frontend/app.py --server.port 7860 --server.address 0.0.0.0 \
     --server.enableCORS false --server.enableXsrfProtection false
