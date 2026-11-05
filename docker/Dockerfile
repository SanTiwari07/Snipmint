# Snipmint: web app + API in one image (CPU).
#   docker compose up --build            (recommended, see docker-compose.yml)
#   docker build -f docker/Dockerfile -t snipmint .
#   docker run -p 8000:8000 -v ./data:/data -v snipmint-models:/models snipmint
# The log prints a link with an access token; open it in your browser.

# --- 1. build the web app -------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- 2. the Python app ----------------------------------------------------------------------
FROM python:3.12-slim AS app

# ffmpeg (with libass) cuts and renders; espeak-ng is needed by the offline voices;
# libgl1/libglib2.0-0 are needed by OpenCV/MediaPipe; curl for the health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng libsndfile1 libgl1 libglib2.0-0 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_LICENSES.md ./
COPY snipmint ./snipmint
COPY --from=web /web/dist ./snipmint/server/static
ARG EXTRAS="server,offline,transcribe,scenes,reframe,gemini"
RUN pip install --no-cache-dir ".[${EXTRAS}]"

RUN mkdir -p /usr/share/doc/snipmint-licenses \
 && cp /usr/share/doc/espeak-ng/copyright /usr/share/doc/snipmint-licenses/espeak-ng.copyright \
 && cp /usr/share/doc/ffmpeg/copyright /usr/share/doc/snipmint-licenses/ffmpeg.copyright

ENV SNIPMINT_HOME=/data \
    SNIPMINT_PORT=8000 \
    HF_HOME=/models/huggingface \
    XDG_CACHE_HOME=/models/cache \
    PYTHONUNBUFFERED=1
VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8000/api/health || exit 1

# The server refuses to listen beyond localhost without a token, so make one if none is given.
CMD ["sh", "-c", "export SNIPMINT_TOKEN=${SNIPMINT_TOKEN:-$(python -c 'import secrets; print(secrets.token_urlsafe(12))')}; echo \"Snipmint: open http://localhost:${SNIPMINT_PORT}/?token=${SNIPMINT_TOKEN}\"; exec snipmint serve --host 0.0.0.0 --port ${SNIPMINT_PORT}"]
