"""
logic_gate.py – Empfänger-, Tag- und Korrespondenten-Zuweisung.

Liest JSON-Sidecar-Dateien aus analyzed_output_dir und wendet die
datenbankbasierten Zuweisungsregeln an (Tabelle assignment_rules).

Alle Regeln – Empfängernamen, Tag-Mappings, Korrespondenten-Normalisierungen –
kommen ausschließlich aus der Datenbank. Änderungen wirken nach dem nächsten
Cache-Refresh (ttl_seconds aus config.yaml) ohne Container-Neustart.

DB-Regeln verwalten:
  -- Neue Person als "me" anlegen:
  INSERT INTO assignment_rules
    (rule_type, match_field, match_value, match_mode, assign_value, priority)
  VALUES ('recipient','recipient_name','Max Mustermann','exact','me',100);

  -- Regel deaktivieren:
  UPDATE assignment_rules SET is_active=0 WHERE id=<id>;

  -- Alle aktiven Regeln anzeigen:
  SELECT * FROM assignment_rules WHERE is_active=1 ORDER BY priority DESC;
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import mysql.connector

from config_loader import AppConfig, configure_logging, load_config
from db_rules import RulesCache, apply_rules, ensure_defaults

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Zuweisung via DB-Regelwerk
# ---------------------------------------------------------------------------

def assign_document(
    sidecar_data: dict,
    cache: RulesCache,
    ocr_text: str = "",
) -> dict[str, list[str]]:
    """Wendet alle aktiven DB-Regeln auf ein Dokument an.

    Args:
        sidecar_data: Inhalt der JSON-Sidecar-Datei.
        cache:        RulesCache-Instanz mit TTL-basiertem Refresh.
        ocr_text:     OCR-Text für keyword-Regeln (optional, aus DB geladen).

    Returns:
        Dict mit 'recipient' (Liste, nur erster Treffer relevant),
        'tags' und 'correspondents' (alle Treffer).
    """
    doc_fields = {
        "sender":          sidecar_data.get("sender") or "",
        "recipient_names": sidecar_data.get("recipient_names") or [],
        "document_type":   sidecar_data.get("document_type") or "",
        "organizations":   sidecar_data.get("organizations") or [],
        "ocr_text":        ocr_text,
    }
    return apply_rules(doc_fields, cache.get_rules())


def _load_ocr_text(
    conn: mysql.connector.MySQLConnection,
    doc_uuid: str,
) -> str:
    """Lädt den OCR-Text eines Dokuments aus der Datenbank."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ocr_text FROM documents WHERE uuid = %s LIMIT 1",
            (doc_uuid,),
        )
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row and row[0] else ""
    except Exception as exc:
        logger.debug("OCR-Text konnte nicht geladen werden (%s): %s", doc_uuid, exc)
        return ""


# ---------------------------------------------------------------------------
# Datenbankoperationen
# ---------------------------------------------------------------------------

def _update_in_db(
    conn: mysql.connector.MySQLConnection,
    doc_uuid: str,
    recipient: str,
    tags: list[str],
    correspondent: str | None,
) -> None:
    """Schreibt Zuweisung in die documents-Tabelle."""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE documents
        SET recipient    = %s,
            paperless_tag = %s,
            status       = 'logic_assigned'
        WHERE uuid   = %s
          AND status = 'analyzed'
        """,
        (
            recipient,
            ",".join(tags) if tags else None,
            doc_uuid,
        ),
    )
    conn.commit()
    cursor.close()


# ---------------------------------------------------------------------------
# Hauptverarbeitungsschleife
# ---------------------------------------------------------------------------

def process_analyzed_files(
    config: AppConfig,
    conn: mysql.connector.MySQLConnection,
    cache: RulesCache,
) -> None:
    """Verarbeitet alle JSON-Sidecar-Dateien im analyzed_output_dir."""
    analyzed_dir = Path(config.paths.analyzed_output_dir)

    for sidecar_path in analyzed_dir.glob("*.json"):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if data.get("status") != "analyzed":
            continue

        doc_uuid = data.get("uuid", "")
        ocr_text = _load_ocr_text(conn, doc_uuid) if doc_uuid else ""

        assignments = assign_document(data, cache, ocr_text=ocr_text)

        recipient = assignments["recipient"][0] if assignments["recipient"] else "unknown"
        tags = assignments["tags"]
        correspondent = assignments["correspondents"][0] if assignments["correspondents"] else None

        logger.info(
            "%s → Empfänger: %s | Tags: %s | Korrespondent: %s",
            data.get("filename", sidecar_path.stem),
            recipient,
            tags or "–",
            correspondent or "–",
        )

        # Sidecar aktualisieren
        data["recipient"] = recipient
        data["tags"] = tags
        data["correspondent"] = correspondent
        data["status"] = "logic_assigned"
        sidecar_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if doc_uuid:
            _update_in_db(conn, doc_uuid, recipient, tags, correspondent)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
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

    # Standard-Regeln und Prompts in DB sicherstellen
    if config.rules_cache.seed_defaults_on_startup:
        ensure_defaults(
            conn,
            persons_me=config.persons.me,
            persons_partner=config.persons.partner,
            tag_mapping=config.tag_mapping,
        )

    # RulesCache mit konfigurierbarem TTL
    cache = RulesCache(
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

    rule_count = len(cache.get_rules())
    logger.info(
        "Logic-Gate gestartet. %d aktive Regeln aus DB geladen. "
        "Cache-TTL: %ds.",
        rule_count,
        config.rules_cache.ttl_seconds,
    )

    try:
        while True:
            process_analyzed_files(config, conn, cache)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Logic-Gate beendet.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

