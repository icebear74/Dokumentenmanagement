-- =============================================================================
-- KI-Dokumenten-Archiv – MariaDB 11.7 Datenbankschema
-- Erfordert: MariaDB >= 11.7 (VECTOR-Typ und HNSW-Index)
-- Ausführung: automatisch beim ersten Container-Start via
--             /docker-entrypoint-initdb.d/
-- =============================================================================

CREATE DATABASE IF NOT EXISTS `document_archive`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `document_archive`;

-- ---------------------------------------------------------------------------
-- Tabelle: documents
-- Speichert alle Metadaten zu einem gescannten Dokument.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `documents` (
  `id`                 BIGINT       UNSIGNED NOT NULL AUTO_INCREMENT,
  `uuid`               CHAR(36)     NOT NULL,
  `filename`           VARCHAR(512) NOT NULL,
  `scan_date`          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `document_date`      DATE         NULL COMMENT 'Datum auf dem Dokument (von KI extrahiert)',
  `sender`             VARCHAR(255) NULL COMMENT 'Absender (von KI extrahiert)',
  `document_type`      VARCHAR(100) NULL COMMENT 'Dokumenttyp (Rechnung, Vertrag, ...)',
  `reference_number`   VARCHAR(255) NULL COMMENT 'Aktenzeichen / Referenznummer',
  `recipient`          VARCHAR(100) NULL COMMENT 'Empfänger (me, partner, company, ...)',
  `confidence`         DECIMAL(5,4) NULL COMMENT 'KI-Konfidenz (0.0000 – 1.0000)',
  `paperless_id`       INT          UNSIGNED NULL COMMENT 'Dokument-ID in Paperless-ngx',
  `paperless_tag`      VARCHAR(100) NULL,
  `ocr_text`           LONGTEXT     NULL COMMENT 'Volltext nach OCR',
  `raw_json`           JSON         NULL COMMENT 'Vollständiges KI-Analyse-Ergebnis als JSON',
  `status`             ENUM(
                         'pending',
                         'analyzed',
                         'logic_assigned',
                         'ingested',
                         'pushed',
                         'quarantine',
                         'error'
                       ) NOT NULL DEFAULT 'pending',
  `error_message`      TEXT         NULL,
  `created_at`         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                             ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_uuid` (`uuid`),
  KEY `idx_status`        (`status`),
  KEY `idx_document_type` (`document_type`),
  KEY `idx_recipient`     (`recipient`),
  KEY `idx_scan_date`     (`scan_date`),
  KEY `idx_document_date` (`document_date`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Metadaten aller gescannten und analysierten Dokumente';


-- ---------------------------------------------------------------------------
-- Tabelle: document_vectors
-- Speichert 768-dimensionale Sentence-Embedding-Vektoren.
-- VECTOR-Typ und HNSW-Index sind MariaDB-11.7-Features.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `document_vectors` (
  `id`          BIGINT   UNSIGNED NOT NULL AUTO_INCREMENT,
  `document_id` BIGINT   UNSIGNED NOT NULL,
  -- 768-dimensionaler Float32-Vektor (Sentence-Transformer Ausgabe)
  `embedding`   VECTOR(768)       NOT NULL,
  `chunk_index` SMALLINT UNSIGNED NOT NULL DEFAULT 0
                  COMMENT 'Index des Text-Chunks (0 = Gesamtdokument)',
  `chunk_text`  TEXT              NULL
                  COMMENT 'Der Text-Chunk, aus dem der Vektor erstellt wurde',
  `created_at`  DATETIME          NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_document_id` (`document_id`),
  -- HNSW-Vektorindex für schnelle Ähnlichkeitssuche
  VECTOR INDEX `hnsw_embedding` (`embedding`)
    COMMENT 'HNSW-Index, M=16, ef_search=64',
  CONSTRAINT `fk_dv_document`
    FOREIGN KEY (`document_id`)
    REFERENCES `documents` (`id`)
    ON DELETE CASCADE
    ON UPDATE CASCADE
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='768-Dim Embedding-Vektoren mit HNSW-Index für RAG-Suche';


-- ---------------------------------------------------------------------------
-- Tabelle: patch_events
-- Protokolliert erkannte Patch-T Codes (Scanner-Trennseiten).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `patch_events` (
  `id`           BIGINT   UNSIGNED NOT NULL AUTO_INCREMENT,
  `filename`     VARCHAR(512)      NOT NULL,
  `patch_type`   VARCHAR(20)       NOT NULL DEFAULT 'T'
                   COMMENT 'Patch-Code-Typ (T, I, II, III, IV, VI)',
  `page_number`  SMALLINT UNSIGNED NOT NULL,
  `detected_at`  DATETIME          NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_filename` (`filename`(255))
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Protokoll erkannter Patch-Code-Trennseiten';


-- ---------------------------------------------------------------------------
-- Tabelle: processing_log
-- Detailliertes Prozess-Protokoll für Debugging und Audit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `processing_log` (
  `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `document_id` BIGINT UNSIGNED NULL,
  `service`     VARCHAR(50)     NOT NULL COMMENT 'Welcher Dienst hat geloggt',
  `level`       ENUM('DEBUG','INFO','WARNING','ERROR','CRITICAL')
                NOT NULL DEFAULT 'INFO',
  `message`     TEXT            NOT NULL,
  `details`     JSON            NULL,
  `logged_at`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_document_id` (`document_id`),
  KEY `idx_service`     (`service`),
  KEY `idx_level`       (`level`),
  KEY `idx_logged_at`   (`logged_at`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Prozess-Protokoll aller Pipeline-Schritte';


-- ---------------------------------------------------------------------------
-- Beispiel-View: aktuelle Dokumenten-Übersicht
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW `v_document_overview` AS
SELECT
  d.id,
  d.uuid,
  d.filename,
  d.scan_date,
  d.document_date,
  d.sender,
  d.document_type,
  d.reference_number,
  d.recipient,
  d.confidence,
  d.paperless_id,
  d.status,
  COUNT(dv.id) AS vector_chunks
FROM `documents` d
LEFT JOIN `document_vectors` dv ON dv.document_id = d.id
GROUP BY d.id;
