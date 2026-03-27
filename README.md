# KI-Dokumenten-Archiv

Modulares, Docker-basiertes System zur automatischen Klassifizierung, Archivierung
und semantischen Suche von gescannten Dokumenten. Die gesamte Konfiguration erfolgt
über eine externe `config.yaml` ohne Hardcoding von Pfaden oder Hardware-IDs im Code.

## Inhaltsverzeichnis

1. [Architektur-Überblick](#architektur-überblick)
2. [Hardware-Voraussetzungen](#hardware-voraussetzungen)
3. [Software-Voraussetzungen](#software-voraussetzungen)
4. [Schnellstart](#schnellstart)
5. [Detaillierte Installation](#detaillierte-installation)
6. [Konfiguration](#konfiguration)
7. [Dokumente scannen – Workflow](#dokumente-scannen--workflow)
8. [Patch-T Code Trennseiten erzeugen](#patch-t-code-trennseiten-erzeugen)
9. [RAG-Chat API](#rag-chat-api)
10. [Datenbankschema](#datenbankschema)
11. [Dienste im Detail](#dienste-im-detail)
12. [Fehlerbehebung](#fehlerbehebung)

---

## Architektur-Überblick

```
Scanner (600 DPI)
      │
      ▼
┌─────────────┐     Patch-T      ┌──────────────┐
│   Watcher   │──────erkannt────▶│  PDF-Splitter │
│ /data/scans │                  └──────┬───────┘
└─────────────┘                         │
                                        ▼
                               ┌────────────────┐   GPU 0
                               │  AI-Analyzer   │◀──(P100)
                               │ OCR + LLM Meta │
                               └───────┬────────┘
                                       │
                                       ▼
                               ┌────────────────┐
                               │  Logic-Gate    │
                               │ Ich/Partner    │
                               └───────┬────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
              ┌──────────────────┐         ┌──────────────────┐
              │  Vector-Ingest   │  GPU 1  │ Paperless-Push   │
              │ 768-Dim HNSW     │◀─(P4)   │ REST-API         │
              └─────────┬────────┘         └──────────────────┘
                        │
                        ▼
              ┌──────────────────┐
              │  MariaDB 11.7    │
              │  HNSW-Index      │
              └─────────┬────────┘
                        │
                        ▼
              ┌──────────────────┐   GPU 0
              │   RAG-Chat API   │◀──(P100)
              │   FastAPI :8080  │
              └──────────────────┘
```

---

## Hardware-Voraussetzungen

| Komponente | Mindest-Anforderung | Empfohlen |
|---|---|---|
| CPU | 4 Kerne | 8+ Kerne |
| RAM | 16 GB | 32+ GB |
| GPU 0 (Vision/LLM) | NVIDIA mit 8 GB VRAM | Tesla P100 (16 GB) |
| GPU 1 (Embeddings) | NVIDIA mit 4 GB VRAM | Tesla P4 (8 GB) |
| Festplatte | 100 GB SSD | 500 GB+ NVMe |
| Betriebssystem | Ubuntu 22.04 LTS | Ubuntu 22.04/24.04 LTS |

> **Einzel-GPU-Betrieb:** Setze `VISION_GPU_ID=0` und `EMBEDDING_GPU_ID=0`
> in der `.env`-Datei. Beide Dienste teilen sich dann GPU 0.

---

## Software-Voraussetzungen

- Docker Engine ≥ 26.0
- Docker Compose ≥ 2.27
- NVIDIA Container Toolkit (für GPU-Passthrough)
- NVIDIA-Treiber ≥ 525

### NVIDIA Container Toolkit installieren

```bash
# Ubuntu/Debian
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Testen
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

---

## Schnellstart

```bash
# 1. Repository klonen
git clone https://github.com/icebear74/Dokumentenmanagement.git
cd Dokumentenmanagement

# 2. Konfigurationsdateien erstellen
cp config.yaml.example config.yaml
cp .env.example .env

# 3. Konfiguration anpassen
nano config.yaml   # Pfade, GPU-IDs, Personennamen, etc.
nano .env          # Passwörter und API-Token

# 4. Datenverzeichnisse erstellen
sudo mkdir -p /data/{scans,split,analyzed,quarantine,mariadb,tmp,logs}
sudo chown -R $USER:$USER /data

# 5. Patch-T Trennseiten erzeugen und drucken (optional)
pip install reportlab
python app/generate_patchcode_pdf.py --count 10 --output trennseiten.pdf

# 6. System starten
docker compose up -d

# 7. Status prüfen
docker compose ps
docker compose logs -f
```

---

## Detaillierte Installation

### Schritt 1: Verzeichnisstruktur

```
/data/
├── scans/          ← Scanner-Ausgabe (600 DPI PDF/TIFF)
├── split/          ← Nach Patch-T Splitting
├── analyzed/       ← Nach KI-Analyse + Empfänger-Zuweisung
├── quarantine/     ← Dokumente unter confidence_threshold
├── mariadb/        ← MariaDB Datenbankdaten (persistent)
├── tmp/            ← Temporäre Arbeitsdateien
└── logs/           ← Anwendungs-Logdateien
```

```bash
sudo mkdir -p /data/{scans,split,analyzed,quarantine,mariadb,tmp,logs}
sudo chown -R 1000:1000 /data   # UID des Docker-Users
```

### Schritt 2: Konfigurationsdatei

```bash
cp config.yaml.example config.yaml
```

Mindest-Anpassungen in `config.yaml`:

```yaml
persons:
  me:
    - "Dein Vollständiger Name"     # ← ANPASSEN
  partner:
    - "Name der Freundin"           # ← ANPASSEN

ai:
  llm_model_id: "mistralai/Mistral-7B-Instruct-v0.2"
  embedding_model_id: "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
```

### Schritt 3: Umgebungsvariablen

```bash
cp .env.example .env
nano .env
```

Pflichtfelder:
- `DB_ROOT_PASSWORD` – sicheres Datenbankpasswort für root
- `DB_PASSWORD` – sicheres Passwort für den Archiv-Benutzer
- `PAPERLESS_API_TOKEN` – API-Token aus Paperless-ngx (Admin → Token)

### Schritt 4: System starten

```bash
# Alle Dienste starten
docker compose up -d

# Logs verfolgen
docker compose logs -f watcher ai_analyzer

# Einen einzelnen Dienst neu starten
docker compose restart ai_analyzer

# System stoppen
docker compose down

# System stoppen und alle Volumes löschen (Achtung: löscht Datenbank!)
docker compose down -v
```

### Schritt 5: Erster Start verifizieren

```bash
# Datenbankverbindung testen
docker compose exec mariadb mariadb -u archive_user -p document_archive \
  -e "SHOW TABLES;"

# RAG-Chat API testen
curl http://localhost:8080/health
# Erwartet: {"status":"ok","service":"rag_chat"}

# Testdokument einlesen
cp /pfad/zu/test.pdf /data/scans/
# Nach ~30 Sekunden sollte das Dokument in /data/analyzed/ erscheinen
```

---

## Konfiguration

Alle Einstellungen befinden sich in `config.yaml`. Die Datei wird als
Read-Only-Volume in jeden Container gemountet:

```yaml
# config.yaml (Auszug der wichtigsten Felder)

paths:
  scan_input_dir: /data/scans          # Scanner-Eingangsordner

gpu:
  vision_gpu_id: 0                     # GPU für Vision/LLM
  embedding_gpu_id: 1                  # GPU für Embeddings

ai:
  confidence_threshold: 0.75           # Mindestkonfidenz für Archivierung
  llm_model_id: "mistralai/Mistral-7B-Instruct-v0.2"
  embedding_model_id: "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

persons:
  me: ["Max Mustermann"]               # Empfänger "Ich"
  partner: ["Erika Musterfrau"]        # Empfänger "Partner"

tag_mapping:
  Rechnung: "Rechnung"
  Kontoauszug: "Kontoauszug"
```

### Umgebungsvariablen (überschreiben config.yaml)

| Variable | Beschreibung | Beispiel |
|---|---|---|
| `CONFIG_PATH` | Pfad zur config.yaml | `/app/config.yaml` |
| `DB_PASSWORD` | Datenbankpasswort | `geheim123` |
| `PAPERLESS_API_TOKEN` | Paperless API-Token | `abc123...` |
| `VISION_GPU_ID` | GPU für Vision/LLM | `0` |
| `EMBEDDING_GPU_ID` | GPU für Embeddings | `1` |
| `SCAN_INPUT_DIR` | Scan-Eingangsordner | `/data/scans` |
| `LOG_LEVEL` | Log-Level | `INFO` |

---

## Dokumente scannen – Workflow

### Empfohlener Scan-Workflow

```
┌─────────────────────────────────────────────────────────────┐
│                    SCAN-WORKFLOW                             │
│                                                             │
│  1. VORBEREITUNG                                            │
│     ├─ Trennseiten (Patch-T) drucken                        │
│     ├─ Dokumente sortieren (pro Stapel ein Thema)           │
│     └─ Dokumente glätten, Klammern entfernen                │
│                                                             │
│  2. STAPEL AUFBAUEN                                         │
│     ┌─────────────────────────────┐                         │
│     │ [Patch-T Trennseite]        │  ← Stapel-Beginn        │
│     │ Dokument 1 (alle Seiten)    │                         │
│     │ [Patch-T Trennseite]        │  ← Nächstes Dokument    │
│     │ Dokument 2 (alle Seiten)    │                         │
│     │ [Patch-T Trennseite]        │  ← Nächstes Dokument    │
│     │ Dokument 3 (alle Seiten)    │                         │
│     └─────────────────────────────┘                         │
│                                                             │
│  3. SCANNER-EINSTELLUNGEN                                   │
│     ├─ Auflösung: 600 DPI (Farbe oder Graustufen)           │
│     ├─ Format: PDF (mehrseitig) oder TIFF                   │
│     ├─ Ausgabeordner: /data/scans/                          │
│     └─ Patch-Code-Erkennung: Typ T aktivieren (falls       │
│        unterstützt – alternativ erledigt der Watcher dies)  │
│                                                             │
│  4. AUTOMATISCHE VERARBEITUNG                               │
│     ├─ Watcher erkennt neue Datei                           │
│     ├─ Patch-T Codes werden gefunden → PDF wird gesplittet │
│     ├─ AI-Analyzer extrahiert Metadaten                    │
│     ├─ Logic-Gate weist Empfänger zu                        │
│     ├─ Vector-Ingest erstellt Embeddings                    │
│     └─ Paperless-Push archiviert das Dokument               │
│                                                             │
│  5. KONTROLLE                                               │
│     ├─ Paperless-ngx: Dokument prüfen und ggf. korrigieren  │
│     ├─ Quarantäne: /data/quarantine/ prüfen                 │
│     └─ RAG-Chat: http://localhost:8080/docs für Suche       │
└─────────────────────────────────────────────────────────────┘
```

### Scanner-Empfehlungen

**Optimale Einstellungen:**
- **Auflösung:** 600 DPI (Mindest: 300 DPI für Barcode-Erkennung)
- **Farbmodus:** Graustufen (für Dokumente), Farbe (für Fotos/Formulare)
- **Dateiformat:** PDF (bevorzugt) oder mehrseitiges TIFF
- **Komprimierung:** LZW (TIFF) oder Standard-PDF-Komprimierung

**Patch-T Trennseiten:**
- Trennseiten auf normalem weißem Papier drucken
- Kein Recyclingpapier (zu grau → schlechtere Erkennung)
- Hochqualitätsdruck, kein Draft-Modus
- Trennseiten nicht knicken oder verschmutzen
- Trennseiten können mehrfach verwendet werden

**Stapelreihenfolge im ADF (Automatic Document Feeder):**
```
Oben im Stapel (zuerst eingezogen):
  [Patch-T Seite]     ← Erster Trenner
  Dokument A Seite 1
  Dokument A Seite 2
  [Patch-T Seite]     ← Zweiter Trenner
  Dokument B Seite 1
  ...
```

### Qualitätskontrolle nach dem Scan

```bash
# Dokumente in der Quarantäne anzeigen
ls -la /data/quarantine/

# Logs auf Fehler prüfen
docker compose logs ai_analyzer | grep -E "ERROR|WARNING"

# Datenbankübersicht
docker compose exec mariadb mariadb -u archive_user -p document_archive \
  -e "SELECT document_type, recipient, COUNT(*) as n, AVG(confidence) as avg_conf
      FROM documents
      GROUP BY document_type, recipient
      ORDER BY n DESC;"
```

---

## Patch-T Code Trennseiten erzeugen

### Standalone (ohne Docker)

```bash
# Abhängigkeit installieren
pip install reportlab

# Eine Trennseite
python app/generate_patchcode_pdf.py

# 10 Trennseiten mit heutigem Datum
python app/generate_patchcode_pdf.py --count 10 --output trennseiten_stapel.pdf

# Trennseiten für ein bestimmtes Datum
python app/generate_patchcode_pdf.py --count 5 --date 2024-06-15 \
  --output trennseiten_juni.pdf
```

### Per Docker

```bash
docker compose run --rm watcher python generate_patchcode_pdf.py \
  --count 10 --output /data/tmp/trennseiten.pdf
```

### Was das PDF enthält

Jede erzeugte Seite enthält:
- **3 Patch-T Code Gruppen** (oben, Mitte, unten) – jede aus 5 Wiederholungen des Musters
- **Erstellungsdatum** (prominent sichtbar) – für die Nachvollziehbarkeit
- **Beschriftung** als "PATCH-T TRENNSEITE / SEPARATOR PAGE"
- **Hinweis**, dass die Seite nicht archiviert wird

Das Datum im PDF dient dazu, generierten Patchcode-Sätze zeitlich zuzuordnen,
da das physische Alter der Trennseiten bei der Langzeitarchivierung eine Rolle
spielen kann.

---

## RAG-Chat API

Die RAG-Chat API läuft auf Port 8080 und bietet folgende Endpunkte:

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| GET | `/health` | Liveness-Probe |
| GET | `/docs` | Swagger UI |
| GET | `/documents` | Dokumentenliste |
| GET | `/document/{uuid}` | Einzeldokument |
| POST | `/search` | Semantische Suche |
| POST | `/chat` | RAG-Chat |

### Beispiele

```bash
# Semantische Suche
curl -X POST http://localhost:8080/search \
  -H "Content-Type: application/json" \
  -d '{"query": "Stromrechnung Januar 2024", "top_k": 5}'

# RAG-Chat
curl -X POST http://localhost:8080/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "Welche Versicherungen sind für mich registriert?"}'

# Dokumente filtern
curl "http://localhost:8080/documents?recipient=me&document_type=Rechnung&limit=20"
```

### Swagger UI

Öffne im Browser: `http://localhost:8080/docs`

---

## Datenbankschema

### Tabellen

| Tabelle | Beschreibung |
|---|---|
| `documents` | Alle Metadaten (Absender, Datum, Typ, Empfänger, Status) |
| `document_vectors` | 768-Dim Embeddings mit HNSW-Index |
| `patch_events` | Protokoll erkannter Patch-T Codes |
| `processing_log` | Detailliertes Pipeline-Protokoll |

### Vektorsuche direkt in MariaDB

```sql
-- Ähnlichste Dokumente zu einem Beispiel-Vektor (hier gekürzt)
SELECT
  d.filename,
  d.sender,
  d.document_type,
  VEC_DISTANCE_COSINE(dv.embedding, VEC_FromText('[0.1, 0.2, ...]')) AS dist
FROM document_vectors dv
JOIN documents d ON d.id = dv.document_id
ORDER BY dist ASC
LIMIT 5;

-- Übersicht
SELECT * FROM v_document_overview LIMIT 20;
```

---

## Dienste im Detail

| Dienst | GPU | Funktion |
|---|---|---|
| `mariadb` | – | MariaDB 11.7 mit HNSW-Vektorindex |
| `watcher` | – | Überwacht /data/scans, erkennt Patch-T, splittet PDFs |
| `ai_analyzer` | GPU 0 | OCR + LLM-Metadaten-Extraktion |
| `logic_gate` | – | Empfängerzuweisung (me/partner) via Config-Namen |
| `vector_ingest` | GPU 1 | Sentence-Embeddings + MariaDB-Ingest |
| `paperless_push` | – | Übergabe an Paperless-ngx REST-API |
| `rag_chat` | GPU 0 | FastAPI Such- und Chat-Interface |

---

## Fehlerbehebung

### GPU nicht erkannt

```bash
# NVIDIA-Treiber prüfen
nvidia-smi

# Docker GPU-Zugriff testen
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi

# Container-GPU prüfen
docker compose exec ai_analyzer python -c \
  "import torch; print('CUDA:', torch.cuda.is_available(), \
   '– Geräte:', torch.cuda.device_count())"
```

### Patch-T Code nicht erkannt

- Trennseiten auf weißem Papier drucken (kein Grauton)
- Scanner-Auflösung auf mindestens 300 DPI erhöhen
- Trennseite auf Verschmutzungen oder Knicke prüfen
- Barcode-Bibliotheken prüfen: `docker compose logs watcher | grep -i barcode`

### Dokument landet in Quarantäne

```bash
# Quarantäne-JSON ansehen
cat /data/quarantine/*.json | python -m json.tool | grep confidence

# Schwellenwert anpassen (in config.yaml)
# ai.confidence_threshold: 0.60  ← niedriger setzen
```

### MariaDB VECTOR-Typ Fehler

Stelle sicher, dass MariaDB **11.7** (oder neuer) läuft:

```bash
docker compose exec mariadb mariadb --version
# Erwartet: ... 11.7 ...
```

### Logs

```bash
# Alle Logs
docker compose logs -f

# Einzelner Dienst
docker compose logs -f ai_analyzer

# Fehler-Filter
docker compose logs | grep -E "ERROR|CRITICAL"

# Datei-Log
tail -f /data/logs/archive.log
```