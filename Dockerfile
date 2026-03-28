# KI-Dokumenten-Archiv – Dockerfile
# Multi-Stage-Build: ein Image mit gemeinsamer Basis, je ein Target pro Dienst.
# Alle Konfiguration über config.yaml (Volume) und .env.

ARG PYTHON_VERSION=3.11

# =============================================================================
# Stage 1: Gemeinsame Basis (Python + System-Abhängigkeiten)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS base

# Systemabhängigkeiten für PyMuPDF, pdf2image, pyzbar
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzbar0 \
        libzbar-dev \
        poppler-utils \
        libgl1-mesa-glx \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Abhängigkeiten installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code kopieren
COPY app/ .

# Standardmäßig keine GPU (wird per docker-compose-Umgebungsvariable gesetzt)
ENV NVIDIA_VISIBLE_DEVICES=none
ENV CONFIG_PATH=/app/config.yaml

# =============================================================================
# Stage 2: Watcher
# =============================================================================
FROM base AS watcher
CMD ["python", "watcher.py"]

# =============================================================================
# Stage 3: AI-Analyzer (GPU 0 – P100)
# =============================================================================
FROM base AS ai_analyzer
CMD ["python", "ai_analyzer.py"]

# =============================================================================
# Stage 4: Logic-Gate (kein GPU benötigt)
# =============================================================================
FROM base AS logic_gate
CMD ["python", "logic_gate.py"]

# =============================================================================
# Stage 5: Vector-Ingest (GPU 1 – P4)
# =============================================================================
FROM base AS vector_ingest
CMD ["python", "vector_ingest.py"]

# =============================================================================
# Stage 6: Paperless-Push (kein GPU benötigt)
# =============================================================================
FROM base AS paperless_push
CMD ["python", "paperless_push.py"]

# =============================================================================
# Stage 7: RAG-Chat API (GPU 0 – P100)
# =============================================================================
FROM base AS rag_chat
EXPOSE 8080
CMD ["python", "main.py"]

# =============================================================================
# Stage 8: Pre-Classifier (Voranalyse & Tag/Kontakt-Generierung)
# Profil: preclass – wird nur manuell gestartet, nicht Teil der Pipeline.
# GPU: nicht benötigt (LLM läuft in Ollama-Container)
# =============================================================================
FROM base AS pre_classifier
CMD ["python", "pre_classifier.py"]
