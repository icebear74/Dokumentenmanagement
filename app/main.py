"""
main.py – RAG-Chat FastAPI-Anwendung für das KI-Dokumenten-Archiv.

Endpunkte:
  GET  /health            – Liveness-Probe
  GET  /documents         – Dokumentenliste mit Filter
  POST /search            – Semantische Vektorsuche (RAG)
  POST /chat              – RAG-Chat mit LLM-Antwort
  GET  /document/{uuid}   – Einzeldokument abrufen
"""

from __future__ import annotations

import logging
import uuid as uuid_module
from contextlib import asynccontextmanager
from typing import Any

import mysql.connector
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globale Konfiguration – beim Start geladen, danach unveränderlich
# ---------------------------------------------------------------------------
_config: AppConfig | None = None
_db_pool: Any = None  # mysql.connector.pooling.MySQLConnectionPool


# ---------------------------------------------------------------------------
# Lifespan – Start- und Shutdown-Logik
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lädt Config und baut Datenbank-Pool beim Start auf."""
    global _config, _db_pool

    _config = load_config()
    configure_logging(_config)

    logger.info("RAG-Chat API startet …")

    _db_pool = mysql.connector.pooling.MySQLConnectionPool(
        pool_name="rag_pool",
        pool_size=_config.database.pool_size,
        host=_config.database.host,
        port=_config.database.port,
        database=_config.database.name,
        user=_config.database.user,
        password=_config.database.password,
        charset="utf8mb4",
        autocommit=True,
    )

    logger.info(
        "Datenbankverbindung hergestellt: %s:%d/%s",
        _config.database.host,
        _config.database.port,
        _config.database.name,
    )

    yield

    logger.info("RAG-Chat API fährt herunter.")


# ---------------------------------------------------------------------------
# FastAPI-App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="KI-Dokumenten-Archiv RAG-Chat",
    description=(
        "Semantische Suche und Chat-Interface für das Dokumenten-Archiv. "
        "Basiert auf MariaDB HNSW-Vektorindex und Sentence-Transformers."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS – Ursprünge aus der Konfiguration
@app.on_event("startup")
async def _setup_cors():
    origins_str: str = (
        _config.rag_chat.cors_origins if _config else "*"
    )
    origins = (
        ["*"]
        if origins_str.strip() == "*"
        else [o.strip() for o in origins_str.split(",")]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# Pydantic-Schemas für Request / Response
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None
    recipient_filter: str | None = None
    document_type_filter: str | None = None


class SearchResult(BaseModel):
    document_uuid: str
    filename: str
    sender: str | None
    document_type: str | None
    document_date: str | None
    recipient: str | None
    confidence: float | None
    chunk_text: str | None
    similarity_score: float


class ChatRequest(BaseModel):
    question: str
    top_k: int | None = None
    recipient_filter: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[SearchResult]
    request_id: str


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _get_connection():
    """Gibt eine Verbindung aus dem Pool zurück."""
    if _db_pool is None:
        raise RuntimeError("Datenbankverbindung nicht initialisiert.")
    return _db_pool.get_connection()


def _get_embedding(text: str) -> list[float]:
    """Generiert einen 768-dim Embedding-Vektor für den Suchtext.

    Verwendet die GPU aus der Konfiguration (embedding_gpu_id).
    Lazy-Import, um den Modell-Download beim Start zu vermeiden.
    """
    import torch
    from sentence_transformers import SentenceTransformer

    gpu_id = _config.gpu.embedding_gpu_id if _config else 1
    device = f"cuda:{gpu_id}" if torch.cuda.is_available() else "cpu"

    model_id = _config.ai.embedding_model_id if _config else (
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
    )

    # Modell wird pro Prozess gecacht (kein erneutes Laden bei jeder Anfrage)
    if not hasattr(_get_embedding, "_model"):
        logger.info("Lade Embedding-Modell %s auf %s …", model_id, device)
        _get_embedding._model = SentenceTransformer(model_id, device=device)

    vector: list[float] = _get_embedding._model.encode(
        text, normalize_embeddings=True
    ).tolist()
    return vector


# ---------------------------------------------------------------------------
# Endpunkte
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health_check():
    """Liveness-Probe: Gibt 200 OK zurück, wenn der Dienst läuft."""
    return {"status": "ok", "service": "rag_chat"}


@app.get("/documents", tags=["Dokumente"])
def list_documents(
    status: str | None = Query(None, description="Filter nach Status"),
    recipient: str | None = Query(None, description="Filter nach Empfänger"),
    document_type: str | None = Query(None, description="Filter nach Dokumenttyp"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Gibt eine gefilterte Liste aller Dokumente zurück."""
    conn = _get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        conditions = []
        params: list[Any] = []

        if status:
            conditions.append("d.status = %s")
            params.append(status)
        if recipient:
            conditions.append("d.recipient = %s")
            params.append(recipient)
        if document_type:
            conditions.append("d.document_type = %s")
            params.append(document_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT d.uuid, d.filename, d.scan_date, d.document_date,
                   d.sender, d.document_type, d.reference_number,
                   d.recipient, d.confidence, d.status, d.paperless_id
            FROM documents d
            {where}
            ORDER BY d.scan_date DESC
            LIMIT %s OFFSET %s
        """
        params += [limit, offset]
        cursor.execute(query, params)
        rows = cursor.fetchall()
        # Datumsfelder serialisieren
        for row in rows:
            for key in ("scan_date", "document_date"):
                if row.get(key) is not None:
                    row[key] = str(row[key])
        return {"documents": rows, "count": len(rows)}
    finally:
        conn.close()


@app.get("/document/{doc_uuid}", tags=["Dokumente"])
def get_document(doc_uuid: str):
    """Gibt Details zu einem einzelnen Dokument zurück."""
    conn = _get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM documents WHERE uuid = %s LIMIT 1",
            (doc_uuid,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dokument nicht gefunden.")
        for key in ("scan_date", "document_date", "created_at", "updated_at"):
            if row.get(key) is not None:
                row[key] = str(row[key])
        return row
    finally:
        conn.close()


@app.post("/search", response_model=list[SearchResult], tags=["Suche"])
def semantic_search(req: SearchRequest):
    """Führt eine semantische Vektorsuche durch und gibt ähnliche Dokumente zurück."""
    top_k = req.top_k or (_config.rag_chat.top_k_results if _config else 5)
    query_vector = _get_embedding(req.query)

    # MariaDB VEC_DISTANCE_COSINE für Ähnlichkeitssuche
    vector_str = "[" + ",".join(f"{v:.8f}" for v in query_vector) + "]"

    conditions = []
    params: list[Any] = []
    if req.recipient_filter:
        conditions.append("d.recipient = %s")
        params.append(req.recipient_filter)
    if req.document_type_filter:
        conditions.append("d.document_type = %s")
        params.append(req.document_type_filter)

    where = ("AND " + " AND ".join(conditions)) if conditions else ""

    conn = _get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        # VEC_DISTANCE_COSINE gibt 0 für identische Vektoren zurück
        sql = f"""
            SELECT
                d.uuid        AS document_uuid,
                d.filename,
                d.sender,
                d.document_type,
                d.document_date,
                d.recipient,
                d.confidence,
                dv.chunk_text,
                VEC_DISTANCE_COSINE(dv.embedding, VEC_FromText(%s)) AS distance
            FROM document_vectors dv
            JOIN documents d ON d.id = dv.document_id
            WHERE 1=1 {where}
            ORDER BY distance ASC
            LIMIT %s
        """
        params = [vector_str] + params + [top_k]
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        results = []
        for row in rows:
            results.append(
                SearchResult(
                    document_uuid=row["document_uuid"],
                    filename=row["filename"],
                    sender=row.get("sender"),
                    document_type=row.get("document_type"),
                    document_date=str(row["document_date"]) if row.get("document_date") else None,
                    recipient=row.get("recipient"),
                    confidence=float(row["confidence"]) if row.get("confidence") else None,
                    chunk_text=row.get("chunk_text"),
                    similarity_score=float(1.0 - row["distance"]),
                )
            )
        return results
    finally:
        conn.close()


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def rag_chat(req: ChatRequest):
    """RAG-Chat: Beantwortet eine Frage basierend auf den gefundenen Dokumenten."""
    search_req = SearchRequest(
        query=req.question,
        top_k=req.top_k,
        recipient_filter=req.recipient_filter,
    )
    sources = semantic_search(search_req)

    if not sources:
        return ChatResponse(
            answer="Keine relevanten Dokumente gefunden.",
            sources=[],
            request_id=str(uuid_module.uuid4()),
        )

    # Kontext aus den Top-Ergebnissen zusammensetzen
    context_parts = []
    for i, src in enumerate(sources, 1):
        context_parts.append(
            f"[{i}] Absender: {src.sender or 'unbekannt'}, "
            f"Typ: {src.document_type or 'unbekannt'}, "
            f"Datum: {src.document_date or 'unbekannt'}\n"
            f"{src.chunk_text or ''}"
        )
    context = "\n\n".join(context_parts)

    prompt = (
        f"Du bist ein Assistent für ein Dokumenten-Archiv.\n"
        f"Beantworte die folgende Frage auf Basis der bereitgestellten Dokument-Auszüge.\n\n"
        f"Frage: {req.question}\n\n"
        f"Dokument-Auszüge:\n{context}\n\n"
        f"Antwort:"
    )

    # LLM-Inferenz (lazy import, GPU aus Config)
    try:
        import torch
        from transformers import pipeline

        gpu_id = _config.gpu.vision_gpu_id if _config else 0
        device = f"cuda:{gpu_id}" if torch.cuda.is_available() else -1

        model_id = _config.ai.llm_model_id if _config else "mistralai/Mistral-7B-Instruct-v0.2"
        max_tokens = _config.ai.llm_max_tokens if _config else 512

        if not hasattr(rag_chat, "_llm_pipe"):
            logger.info("Lade LLM %s …", model_id)
            rag_chat._llm_pipe = pipeline(
                "text-generation",
                model=model_id,
                device=device,
                max_new_tokens=max_tokens,
            )

        output = rag_chat._llm_pipe(prompt, return_full_text=False)
        answer = output[0]["generated_text"].strip()
    except Exception as exc:
        logger.warning("LLM nicht verfügbar, gebe Kontext zurück: %s", exc)
        answer = f"LLM nicht verfügbar. Relevante Dokument-Auszüge:\n\n{context}"

    return ChatResponse(
        answer=answer,
        sources=sources,
        request_id=str(uuid_module.uuid4()),
    )


# ---------------------------------------------------------------------------
# Direktstart (z. B. python main.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    configure_logging(cfg)

    uvicorn.run(
        "main:app",
        host=cfg.rag_chat.host,
        port=cfg.rag_chat.port,
        reload=False,
        log_level=cfg.logging.level.lower(),
    )
