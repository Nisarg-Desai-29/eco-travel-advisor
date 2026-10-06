# Rasa server (NLU + dialogue). Trains the model while building the image.
FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt && python -m spacy download en_core_web_md
COPY . .
RUN rasa train
EXPOSE 5005
CMD ["rasa", "run", "--enable-api", "--cors", "*", "--endpoints", "endpoints.docker.yml", "--port", "5005"]
