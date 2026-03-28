"""
ai_analyzer.py – Metadaten-Extraktion aus gescannten Dokumenten.

Überwacht split_output_dir, führt OCR + LLM-Analyse durch und
speichert Ergebnisse als JSON-Sidecar sowie in der Datenbank.

Der LLM-Prompt kommt aus der Datenbank (Tabelle llm_prompts,
service='ai_analyzer'). Änderungen am Prompt wirken nach dem nächsten
Cache-Refresh ohne Container-Neustart.

GPU: vision_gpu_id aus der Konfiguration (Standard: GPU 0, Tesla P100).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import fitz  # PyMuPDF
import mysql.connector

from config_loader import AppConfig, configure_logging, load_config
from db_rules import (
    RulesCache,
    ensure_defaults,
    _DEFAULT_AI_ANALYZER_SYSTEM,
    _DEFAULT_AI_ANALYZER_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

# The prompt template is now loaded from the database (llm_prompts table).
# _DEFAULT_AI_ANALYZER_USER_TEMPLATE is the fallback used if the DB entry
# is missing or the DB is unavailable.

# Module-level cache reference – set during main() startup.
_rules_cache: RulesCache | None = None


def _get_prompt_template() -> str:
    """Returns the user prompt template, preferring the DB version."""
    if _rules_cache is not None:
        return _rules_cache.get_prompt(
            "ai_analyzer", "default", "user_template",
            default=_DEFAULT_AI_ANALYZER_USER_TEMPLATE,
        )
    return _DEFAULT_AI_ANALYZER_USER_TEMPLATE



# ---------------------------------------------------------------------------
# Datumsnormalisierung
# ---------------------------------------------------------------------------

_GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    # Kurzformen
    "jan": 1, "feb": 2, "mär": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}

def normalize_date(raw: str | None) -> str | None:
    """Normalisiert Datumsangaben aus LLM-Output oder OCR auf YYYY-MM-DD.

    Akzeptiert u.a.:
      • "27. März 2026" / "27.März 2026"
      • "27.03.2026" / "27.3.2026"
      • "2026-03-27"  (bereits ISO)
      • "März 2026"   → "2026-03-01"
      • "03/2026"     → "2026-03-01"
      • "2026"        → "2026-01-01"

    Returns:
        ISO-Datumsstring (YYYY-MM-DD) oder None wenn nicht erkennbar.
    """
    if not raw:
        return None
    s = str(raw).strip()

    # Bereits ISO YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass

    # DD.MM.YYYY oder DD.M.YYYY
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass

    # DD. Monatsname YYYY oder DD.Monatsname YYYY
    m = re.match(r"(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", s)
    if m:
        month_name = m.group(2).lower()
        month_num = _GERMAN_MONTHS.get(month_name)
        if month_num:
            try:
                return date(int(m.group(3)), month_num, int(m.group(1))).isoformat()
            except ValueError:
                pass

    # Monatsname YYYY  (kein Tag → 1. des Monats)
    m = re.match(r"([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", s)
    if m:
        month_name = m.group(1).lower()
        month_num = _GERMAN_MONTHS.get(month_name)
        if month_num:
            try:
                return date(int(m.group(2)), month_num, 1).isoformat()
            except ValueError:
                pass

    # MM/YYYY oder MM.YYYY
    m = re.fullmatch(r"(\d{1,2})[./](\d{4})", s)
    if m:
        try:
            return date(int(m.group(2)), int(m.group(1)), 1).isoformat()
        except ValueError:
            pass

    # Nur Jahr
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return f"{year}-01-01"

    logger.debug("Datum konnte nicht normalisiert werden: %r", raw)
    return None


# ---------------------------------------------------------------------------
# Hilfsklassen
# ---------------------------------------------------------------------------

class AnalysisResult:
    def __init__(
        self,
        sender: str | None,
        document_date: str | None,
        document_type: str,
        reference_number: str | None,
        confidence: float,
        recipient_names: list[str],
        topic_tags: list[str],
        organizations: list[str],
        ocr_text: str,
        raw_json: dict,
    ) -> None:
        self.sender = sender
        self.document_date = document_date      # ISO YYYY-MM-DD or None
        self.document_type = document_type
        self.reference_number = reference_number
        self.confidence = confidence
        self.recipient_names = recipient_names
        self.topic_tags = topic_tags            # LLM-extracted semantic tags
        self.organizations = organizations      # named orgs from document
        self.ocr_text = ocr_text
        self.raw_json = raw_json


# ---------------------------------------------------------------------------
# OCR-Extraktion
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extrahiert Text aus einer PDF-Datei via PyMuPDF.

    Gibt den zusammengesetzten Text aller Seiten zurück.
    """
    doc = fitz.open(str(pdf_path))
    pages_text: list[str] = []
    for page in doc:
        pages_text.append(page.get_text("text"))
    doc.close()
    return "\n".join(pages_text).strip()


# ---------------------------------------------------------------------------
# LLM-gestützte Metadaten-Extraktion
# ---------------------------------------------------------------------------

_llm_pipe = None


def _get_llm_pipeline(config: AppConfig):
    """Gibt die LLM-Pipeline zurück (Lazy-Loading, einmalig pro Prozess)."""
    global _llm_pipe
    if _llm_pipe is not None:
        return _llm_pipe

    import torch
    from transformers import pipeline

    gpu_id = config.gpu.vision_gpu_id
    device = f"cuda:{gpu_id}" if torch.cuda.is_available() else -1

    if not torch.cuda.is_available() and not config.gpu.allow_cpu_fallback:
        raise RuntimeError(
            f"CUDA nicht verfügbar und allow_cpu_fallback=false. "
            f"Erwartete GPU {gpu_id}."
        )

    logger.info("Lade LLM '%s' auf %s …", config.ai.llm_model_id, device)
    _llm_pipe = pipeline(
        "text-generation",
        model=config.ai.llm_model_id,
        device=device,
        max_new_tokens=config.ai.llm_max_tokens,
        return_full_text=False,
    )
    logger.info("LLM geladen.")
    return _llm_pipe


def analyze_document(pdf_path: Path, config: AppConfig) -> AnalysisResult:
    """Analysiert ein Dokument und extrahiert Metadaten.

    Args:
        pdf_path: Pfad zur PDF-Datei.
        config: Anwendungskonfiguration.

    Returns:
        AnalysisResult mit allen extrahierten Metadaten.
    """
    ocr_text = extract_text_from_pdf(pdf_path)

    # Begrenze Text für den Prompt
    prompt_text = ocr_text[:4000] if len(ocr_text) > 4000 else ocr_text
    # Prompt-Template aus DB laden (Fallback auf hardcodierten Standard)
    prompt = _get_prompt_template().format(text=prompt_text)

    raw_json: dict = {}
    try:
        pipe = _get_llm_pipeline(config)
        output = pipe(prompt)
        generated = output[0]["generated_text"].strip()

        # JSON aus der LLM-Ausgabe extrahieren
        json_match = re.search(r"\{.*\}", generated, re.DOTALL)
        if json_match:
            raw_json = json.loads(json_match.group())
        else:
            logger.warning("Kein JSON in LLM-Antwort gefunden für %s", pdf_path.name)
            raw_json = {}
    except Exception as exc:
        logger.error("LLM-Analyse fehlgeschlagen für %s: %s", pdf_path.name, exc)
        raw_json = {}

    return AnalysisResult(
        sender=raw_json.get("sender"),
        document_date=normalize_date(raw_json.get("document_date")),
        document_type=raw_json.get("document_type", "Sonstiges"),
        reference_number=raw_json.get("reference_number"),
        confidence=float(raw_json.get("confidence", 0.0)),
        recipient_names=[
            str(n) for n in raw_json.get("recipient_names", []) if n
        ],
        topic_tags=[
            str(t).strip() for t in raw_json.get("topic_tags", []) if t
        ],
        organizations=[
            str(o) for o in raw_json.get("organizations", []) if o
        ],
        ocr_text=ocr_text,
        raw_json=raw_json,
    )


# ---------------------------------------------------------------------------
# Datenbankoperationen
# ---------------------------------------------------------------------------

def save_to_db(
    conn: mysql.connector.MySQLConnection,
    pdf_path: Path,
    result: AnalysisResult,
    doc_uuid: str,
    status: str,
) -> int:
    """Speichert Analyse-Ergebnisse in der Datenbank.

    Returns:
        Datenbankzeilen-ID des neuen Eintrags.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO documents
          (uuid, filename, document_date, sender, document_type,
           reference_number, confidence, ocr_text, raw_json, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            doc_uuid,
            pdf_path.name,
            result.document_date or None,
            result.sender,
            result.document_type,
            result.reference_number,
            result.confidence,
            result.ocr_text,
            json.dumps(result.raw_json, ensure_ascii=False),
            status,
        ),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Hauptverarbeitungsschleife
# ---------------------------------------------------------------------------

def process_pending_files(config: AppConfig, conn: mysql.connector.MySQLConnection) -> None:
    """Verarbeitet alle PDFs im split_output_dir."""
    split_dir = Path(config.paths.split_output_dir)
    analyzed_dir = Path(config.paths.analyzed_output_dir)
    quarantine_dir = Path(config.paths.quarantine_dir)

    analyzed_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    for pdf_path in split_dir.glob("*.pdf"):
        sidecar = pdf_path.with_suffix(".json")
        if sidecar.exists():
            continue  # bereits verarbeitet

        logger.info("Analysiere: %s", pdf_path.name)
        doc_uuid = str(uuid.uuid4())

        try:
            result = analyze_document(pdf_path, config)
        except Exception as exc:
            logger.error("Analyse fehlgeschlagen für %s: %s", pdf_path.name, exc)
            _move_file(pdf_path, quarantine_dir)
            continue

        if result.confidence < config.ai.confidence_threshold:
            logger.warning(
                "Konfidenz zu niedrig (%.2f < %.2f) für %s – Quarantäne.",
                result.confidence,
                config.ai.confidence_threshold,
                pdf_path.name,
            )
            sidecar_data = _build_sidecar(doc_uuid, pdf_path, result, "quarantine")
            sidecar.write_text(
                json.dumps(sidecar_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            save_to_db(conn, pdf_path, result, doc_uuid, "quarantine")
            _move_file(pdf_path, quarantine_dir)
            sidecar.unlink(missing_ok=True)
        else:
            sidecar_data = _build_sidecar(doc_uuid, pdf_path, result, "analyzed")
            sidecar.write_text(
                json.dumps(sidecar_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            save_to_db(conn, pdf_path, result, doc_uuid, "analyzed")
            _move_file(pdf_path, analyzed_dir)
            dest_sidecar = analyzed_dir / sidecar.name
            sidecar.rename(dest_sidecar)
            logger.info(
                "Analyse abgeschlossen: %s (Typ=%s, Konfidenz=%.2f)",
                pdf_path.name,
                result.document_type,
                result.confidence,
            )


def _build_sidecar(
    doc_uuid: str,
    pdf_path: Path,
    result: AnalysisResult,
    status: str,
) -> dict:
    return {
        "uuid": doc_uuid,
        "filename": pdf_path.name,
        "analyzed_at": datetime.utcnow().isoformat(),
        "status": status,
        "sender": result.sender,
        "document_date": result.document_date,   # ISO YYYY-MM-DD or null
        "document_type": result.document_type,
        "reference_number": result.reference_number,
        "confidence": result.confidence,
        "recipient_names": result.recipient_names,
        "topic_tags": result.topic_tags,          # LLM-extracted semantic tags
        "organizations": result.organizations,    # named orgs from document
        "raw": result.raw_json,
    }


def _move_file(src: Path, dest_dir: Path) -> None:
    dest = dest_dir / src.name
    src.rename(dest)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    global _rules_cache

    config = load_config()
    configure_logging(config)

    conn = mysql.connector.connect(
        host=config.database.host,
        port=config.database.port,
        database=config.database.name,
        user=config.database.user,
        password=config.database.password,
        charset="utf8mb4",
        autocommit=False,
    )

    # Standard-Prompts und Regeln beim Start sicherstellen
    if config.rules_cache.seed_defaults_on_startup:
        ensure_defaults(
            conn,
            persons_me=config.persons.me,
            persons_partner=config.persons.partner,
            tag_mapping=config.tag_mapping,
        )

    # RulesCache für Prompt-Ladefunktionen initialisieren
    _rules_cache = RulesCache(
        conn_factory=lambda: mysql.connector.connect(
            host=config.database.host,
            port=config.database.port,
            database=config.database.name,
            user=config.database.user,
            password=config.database.password,
            charset="utf8mb4",
        ),
        ttl_seconds=config.rules_cache.ttl_seconds,
    )

    logger.info(
        "AI-Analyzer gestartet. Prompt-Quelle: DB (service=ai_analyzer). "
        "Überwache %s …",
        config.paths.split_output_dir,
    )
    try:
        while True:
            process_pending_files(config, conn)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("AI-Analyzer beendet.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
