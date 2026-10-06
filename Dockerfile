# Small production image for the web app and CLI.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY jobcopilot ./jobcopilot
COPY config ./config
COPY profile.example ./profile.example

# data/ (SQLite), profile/ (your files) and output/ (generated resumes) are volumes.
RUN mkdir -p data profile output && useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["sh", "-c", "uvicorn jobcopilot.web.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"]
