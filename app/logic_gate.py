"""
logic_gate.py – Empfängerzuweisung für gescannte Dokumente.

Liest JSON-Sidecar-Dateien aus analyzed_output_dir, gleicht die
recipient_names gegen die Personenliste aus der Konfiguration ab
und schreibt den Empfänger ("me", "partner", oder "unknown") zurück.

Alle Namen kommen aus config.yaml – kein Hardcoding.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import mysql.connector

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Namensabgleich
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Normalisiert einen Namen für den Vergleich (Kleinschreibung, Strip)."""
    return name.strip().lower()


def determine_recipient(
    recipient_names: list[str],
    config: AppConfig,
) -> str:
    """Bestimmt den Empfänger anhand der erkannten Namen im Dokument.

    Vergleicht die vom LLM extrahierten Namen mit den Konfigurationslisten.
    Groß-/Kleinschreibung wird ignoriert.

    Args:
        recipient_names: Vom LLM extrahierte Namen aus dem Dokument.
        config: Anwendungskonfiguration mit persons.me / persons.partner.

    Returns:
        "me" | "partner" | "unknown"
    """
    normalized_input = {_normalize(n) for n in recipient_names}

    me_names = {_normalize(n) for n in config.persons.me}
    partner_names = {_normalize(n) for n in config.persons.partner}

    if normalized_input & me_names:
        return "me"
    if normalized_input & partner_names:
        return "partner"
    return "unknown"


# ---------------------------------------------------------------------------
# Datenbankoperationen
# ---------------------------------------------------------------------------

def update_recipient_in_db(
    conn: mysql.connector.MySQLConnection,
    doc_uuid: str,
    recipient: str,
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE documents
        SET recipient = %s,
            status = 'logic_assigned'
        WHERE uuid = %s
          AND status = 'analyzed'
        """,
        (recipient, doc_uuid),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hauptverarbeitungsschleife
# ---------------------------------------------------------------------------

def process_analyzed_files(
    config: AppConfig,
    conn: mysql.connector.MySQLConnection,
) -> None:
    """Verarbeitet alle JSON-Sidecar-Dateien im analyzed_output_dir."""
    analyzed_dir = Path(config.paths.analyzed_output_dir)

    for sidecar_path in analyzed_dir.glob("*.json"):
        # Prüfe ob bereits zugewiesen (has_recipient-Flag)
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))

        if data.get("status") != "analyzed":
            continue  # Bereits verarbeitet oder Fehler

        recipient_names: list[str] = data.get("recipient_names", [])
        recipient = determine_recipient(recipient_names, config)

        logger.info(
            "Empfänger für %s: %s (erkannte Namen: %s)",
            data.get("filename", sidecar_path.stem),
            recipient,
            recipient_names,
        )

        # Sidecar aktualisieren
        data["recipient"] = recipient
        data["status"] = "logic_assigned"
        sidecar_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Datenbank aktualisieren
        doc_uuid = data.get("uuid")
        if doc_uuid:
            update_recipient_in_db(conn, doc_uuid, recipient)


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

    logger.info(
        "Logic-Gate gestartet.\n"
        "  'me' Namen: %s\n"
        "  'partner' Namen: %s",
        config.persons.me,
        config.persons.partner,
    )

    try:
        while True:
            process_analyzed_files(config, conn)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Logic-Gate beendet.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
