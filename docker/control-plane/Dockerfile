# M6-07 NAS Control Plane image.
# Contains the control plane HTTP API, scheduler, and the NAS local worker
# (KNOWLEDGE_FINALIZE / STORE_INGEST). It intentionally does NOT include GPU,
# Chrome, LM Studio, or llama.cpp runtimes.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Non-root runtime user (container-level; real volume permissions are the
# operator's responsibility - see the runbook).
RUN groupadd --gid 1000 pkp \
    && useradd --uid 1000 --gid pkp --create-home --shell /usr/sbin/nologin pkp

WORKDIR /opt/pkp

# Minimal runtime deps for the control plane / NAS local worker stages.
# GPU/OCR/ASR deps are Windows-side only.
COPY docker/control-plane/requirements-docker.txt ./requirements-docker.txt
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY src ./src

# Local dirs the control plane may write (logs). Ops DB / M5 / artifacts come
# from mounted volumes.
RUN mkdir -p /var/log/pkp /var/lib/pkp/operations /var/lib/pkp/knowledge \
    && chown -R pkp:pkp /opt/pkp /var/log/pkp /var/lib/pkp

USER pkp

EXPOSE 8765

# Healthcheck uses the Python stdlib to call /health/live (no curl needed).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/health/live', timeout=4); sys.exit(0)"]

ENTRYPOINT ["python", "-m", "src.operations.control_plane"]
CMD ["--config", "/opt/pkp/control-plane.json"]