"""
vector_ingest.py – Erstellt 768-dim Embeddings und speichert sie in MariaDB.

Überwacht analyzed_output_dir auf Sidecar-Dateien mit Status "logic_assigned",
generiert Sentence-Embeddings via sentence-transformers und schreibt sie
in die document_vectors-Tabelle mit HNSW-Index.

GPU: embedding_gpu_id aus der Konfiguration (Standard: GPU 1, Tesla P4).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import mysql.connector

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)

_embedding_model = None


# ---------------------------------------------------------------------------
# Embedding-Modell (Lazy-Loading, einmalig pro Prozess)
# ---------------------------------------------------------------------------

def _get_embedding_model(config: AppConfig):
    """Gibt das Sentence-Transformer-Modell zurück (gecacht)."""
    global _embedding_model
    if _embedding_model is not None:
        return _embedding_model

    import torch
    from sentence_transformers import SentenceTransformer

    gpu_id = config.gpu.embedding_gpu_id
    if torch.cuda.is_available():
        device = f"cuda:{gpu_id}"
    elif config.gpu.allow_cpu_fallback:
        device = "cpu"
        logger.warning("CUDA nicht verfügbar – nutze CPU für Embeddings.")
    else:
        raise RuntimeError(
            f"CUDA nicht verfügbar und allow_cpu_fallback=false. "
            f"Erwartete Embedding-GPU {gpu_id}."
        )

    model_id = config.ai.embedding_model_id
    logger.info("Lade Embedding-Modell '%s' auf %s …", model_id, device)
    _embedding_model = SentenceTransformer(model_id, device=device)
    logger.info("Embedding-Modell bereit (Dimension: 768).")
    return _embedding_model


# ---------------------------------------------------------------------------
# Text-Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = 1000, overlap: int = 100) -> list[str]:
    """Teilt langen Text in überlappende Chunks.

    Args:
        text: Eingabetext.
        max_chars: Maximale Zeichenanzahl pro Chunk.
        overlap: Überlappung zwischen benachbarten Chunks.

    Returns:
        Liste der Text-Chunks.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        chunks.append(text[start:end])
        start += max_chars - overlap

    return chunks


# ---------------------------------------------------------------------------
# Datenbankoperationen
# ---------------------------------------------------------------------------

def insert_vectors(
    conn: mysql.connector.MySQLConnection,
    document_id: int,
    chunks: list[str],
    embeddings: list[list[float]],
) -> None:
    """Schreibt Embedding-Vektoren in document_vectors."""
    cursor = conn.cursor()
    for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        vector_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
        cursor.execute(
            """
            INSERT INTO document_vectors
              (document_id, embedding, chunk_index, chunk_text)
            VALUES (%s, VEC_FromText(%s), %s, %s)
            """,
            (document_id, vector_str, idx, chunk),
        )
    conn.commit()


def get_document_db_id(
    conn: mysql.connector.MySQLConnection,
    doc_uuid: str,
) -> int | None:
    """Gibt die Datenbank-ID für eine Dokument-UUID zurück."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM documents WHERE uuid = %s LIMIT 1",
        (doc_uuid,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def mark_as_ingested(
    conn: mysql.connector.MySQLConnection,
    doc_uuid: str,
) -> None:
    """Setzt den Dokument-Status auf 'ingested'."""
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE documents SET status = 'ingested' WHERE uuid = %s",
        (doc_uuid,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hauptverarbeitungsschleife
# ---------------------------------------------------------------------------

def process_pending_documents(
    config: AppConfig,
    conn: mysql.connector.MySQLConnection,
) -> None:
    """Verarbeitet alle Sidecar-Dateien mit Status 'logic_assigned'."""
    analyzed_dir = Path(config.paths.analyzed_output_dir)

    for sidecar_path in analyzed_dir.glob("*.json"):
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))

        if data.get("status") != "logic_assigned":
            continue

        doc_uuid = data.get("uuid")
        filename = data.get("filename", sidecar_path.stem)
        ocr_text = data.get("ocr_text", "")

        if not ocr_text:
            # Versuche OCR-Text aus Datenbank zu lesen
            db_id = get_document_db_id(conn, doc_uuid)
            if db_id:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT ocr_text FROM documents WHERE id = %s",
                    (db_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    ocr_text = row[0]

        if not ocr_text:
            logger.warning("Kein OCR-Text für %s – überspringe Embedding.", filename)
            continue

        logger.info("Erstelle Embeddings für: %s", filename)

        chunks = chunk_text(ocr_text)
        model = _get_embedding_model(config)

        embeddings = model.encode(
            chunks,
            batch_size=config.ai.embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).tolist()

        db_id = get_document_db_id(conn, doc_uuid)
        if not db_id:
            logger.error(
                "Dokument-ID für UUID %s nicht in DB gefunden – überspringe.", doc_uuid
            )
            continue

        insert_vectors(conn, db_id, chunks, embeddings)
        mark_as_ingested(conn, doc_uuid)

        # Sidecar aktualisieren
        data["status"] = "ingested"
        data["vector_chunks"] = len(chunks)
        sidecar_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "Vektoren gespeichert für %s: %d Chunks à 768 Dim.",
            filename,
            len(chunks),
        )


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
        "Vector-Ingest gestartet. Modell: %s, GPU: %d",
        config.ai.embedding_model_id,
        config.gpu.embedding_gpu_id,
    )

    try:
        while True:
            process_pending_documents(config, conn)
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Vector-Ingest beendet.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
