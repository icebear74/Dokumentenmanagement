"""
paperless_push.py – Übergibt verarbeitete Dokumente an die Paperless-ngx REST-API.

Liest JSON-Sidecar-Dateien mit Status "ingested", lädt das zugehörige PDF
via REST-API hoch und aktualisiert Metadaten (Tags, Dokumenttyp, Empfänger).

Alle Verbindungsdaten kommen aus config.yaml / Umgebungsvariablen.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import mysql.connector
import requests

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paperless-ngx API-Client
# ---------------------------------------------------------------------------

class PaperlessClient:
    """Einfacher Client für die Paperless-ngx REST-API."""

    def __init__(self, base_url: str, api_token: str) -> None:
        if not api_token:
            raise ValueError(
                "Paperless API-Token fehlt. Setze PAPERLESS_API_TOKEN als "
                "Umgebungsvariable oder 'paperless.api_token' in config.yaml."
            )
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {api_token}"}
        )

    def upload_document(
        self,
        pdf_path: Path,
        title: str | None = None,
        document_type_name: str | None = None,
        tag_names: list[str] | None = None,
        correspondent_name: str | None = None,
        created_date: str | None = None,
    ) -> int | None:
        """Lädt ein Dokument zu Paperless-ngx hoch.

        Returns:
            Paperless-Dokument-ID oder None bei Fehler.
        """
        url = f"{self.base_url}/api/documents/post_document/"

        with pdf_path.open("rb") as fh:
            files = {"document": (pdf_path.name, fh, "application/pdf")}
            data: dict[str, str] = {}

            if title:
                data["title"] = title
            if correspondent_name:
                data["correspondent"] = correspondent_name
            if created_date:
                data["created"] = created_date
            # Tags als kommaseparierte Liste
            if tag_names:
                data["tags"] = ",".join(tag_names)

            response = self.session.post(url, files=files, data=data, timeout=120)

        if response.status_code in (200, 201):
            try:
                task_id = response.json().get("task_id") or response.text.strip()
                logger.info(
                    "Dokument hochgeladen: %s (task_id=%s)",
                    pdf_path.name,
                    task_id,
                )
                return task_id
            except Exception:
                logger.info("Dokument hochgeladen: %s", pdf_path.name)
                return None
        else:
            logger.error(
                "Hochladen fehlgeschlagen für %s: HTTP %d – %s",
                pdf_path.name,
                response.status_code,
                response.text[:500],
            )
            return None


# ---------------------------------------------------------------------------
# Hauptverarbeitungsschleife
# ---------------------------------------------------------------------------

def process_ingested_documents(
    config: AppConfig,
    client: PaperlessClient,
    conn: mysql.connector.MySQLConnection,
) -> None:
    """Verarbeitet Sidecar-Dateien mit Status 'ingested'."""
    analyzed_dir = Path(config.paths.analyzed_output_dir)

    for sidecar_path in analyzed_dir.glob("*.json"):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))

        if data.get("status") != "ingested":
            continue

        filename = data.get("filename", "")
        pdf_path = analyzed_dir / filename

        if not pdf_path.exists():
            logger.warning("PDF nicht gefunden: %s – überspringe.", pdf_path)
            continue

        doc_type = data.get("document_type", config.paperless.default_document_type)
        sender = data.get("sender")
        recipient = data.get("recipient", "unknown")
        doc_date = data.get("document_date")

        # Tag aus Mapping ermitteln
        tag_name = config.tag_mapping.get(doc_type, "Unsortiert")
        # Empfänger als zusätzlichen Tag hinzufügen
        tags = [tag_name]
        if recipient == "me":
            tags.append("Ich")
        elif recipient == "partner":
            tags.append("Partner")

        # Titel zusammensetzen
        title_parts = []
        if sender:
            title_parts.append(sender)
        if doc_date:
            title_parts.append(doc_date)
        if doc_type:
            title_parts.append(doc_type)
        title = " – ".join(title_parts) if title_parts else pdf_path.stem

        logger.info(
            "Sende an Paperless: %s (Typ=%s, Tags=%s, Empfänger=%s)",
            filename,
            doc_type,
            tags,
            recipient,
        )

        task_id = client.upload_document(
            pdf_path=pdf_path,
            title=title,
            tag_names=tags,
            correspondent_name=sender,
            created_date=doc_date,
        )

        # Sidecar und DB aktualisieren
        data["status"] = "pushed"
        data["paperless_task_id"] = task_id
        sidecar_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if data.get("uuid"):
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE documents
                SET status = 'pushed',
                    paperless_tag = %s
                WHERE uuid = %s
                """,
                (tag_name, data["uuid"]),
            )
            conn.commit()

        logger.info("Erfolgreich an Paperless übergeben: %s", filename)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    configure_logging(config)

    if not config.paperless.api_token:
        logger.error(
            "PAPERLESS_API_TOKEN nicht gesetzt – Paperless-Push deaktiviert."
        )
        return

    client = PaperlessClient(
        base_url=config.paperless.base_url,
        api_token=config.paperless.api_token,
    )

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
        "Paperless-Push gestartet. Ziel: %s",
        config.paperless.base_url,
    )

    try:
        while True:
            process_ingested_documents(config, client, conn)
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Paperless-Push beendet.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
