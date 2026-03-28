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
-- Tabelle: llm_prompts
-- Speichert alle LLM-System-Prompts und User-Templates pro Dienst.
-- Änderungen hier wirken sofort – kein Container-Neustart nötig.
-- Python-Dienste schreiben Standard-Einträge per INSERT IGNORE beim Start.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `llm_prompts` (
  `id`          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `service`     VARCHAR(50)  NOT NULL
                  COMMENT 'Dienst: ai_analyzer, pre_classifier, logic_gate …',
  `name`        VARCHAR(100) NOT NULL DEFAULT 'default'
                  COMMENT 'Prompt-Bezeichner: default, phase1, phase2 …',
  `prompt_type` ENUM('system','user_template') NOT NULL
                  COMMENT 'system = Rollen-Prompt; user_template = Prompt mit {placeholder}',
  `content`     MEDIUMTEXT   NOT NULL
                  COMMENT 'Prompt-Text; user_template darf {placeholder}-Felder enthalten',
  `is_active`   TINYINT(1)   NOT NULL DEFAULT 1,
  `version`     INT UNSIGNED NOT NULL DEFAULT 1
                  COMMENT 'Manuell hochzählen bei inhaltlichen Änderungen',
  `notes`       VARCHAR(500) NULL
                  COMMENT 'Erläuterung / Zweck dieses Prompts',
  `created_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_service_name_type` (`service`, `name`, `prompt_type`),
  KEY `idx_service`   (`service`),
  KEY `idx_is_active` (`is_active`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='LLM-Prompts aller Dienste – zentrale Verwaltung ohne Code-Änderungen';


-- ---------------------------------------------------------------------------
-- Tabelle: assignment_rules
-- Flexible Zuweisungsregeln für Empfänger, Tags und Korrespondenten.
-- Ersetzt die statischen persons.*-Einträge aus config.yaml vollständig.
-- Python-Dienste seeden Basis-Regeln aus config.yaml beim ersten Start.
--
-- Beispielregeln (werden von Python automatisch eingefügt):
--   recipient + recipient_name + "Max Mustermann" + exact   → "me"
--   recipient + recipient_name + "Erika Musterfrau" + exact → "partner"
--   tag       + document_type  + "Rechnung"        + exact  → "Rechnung"
--   tag       + organization   + "Techniker"       + contains → "Krankenversicherung"
--   tag       + keyword        + "mahnung"         + contains → "Mahnung"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `assignment_rules` (
  `id`           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `rule_type`    ENUM('recipient','tag','correspondent') NOT NULL
                   COMMENT 'Was wird zugewiesen?',
  `match_field`  ENUM('sender','recipient_name','document_type',
                      'organization','keyword','llm_tag') NOT NULL
                   COMMENT 'Gegen welches Dokumentfeld wird geprüft? llm_tag=LLM-extrahierte topic_tags',
  `match_value`  VARCHAR(255) NOT NULL
                   COMMENT 'Zu suchender Wert (case-insensitive)',
  `match_mode`   ENUM('exact','contains','startswith','regex') NOT NULL
                   DEFAULT 'contains'
                   COMMENT 'Vergleichs-Modus',
  `assign_value` VARCHAR(255) NOT NULL
                   COMMENT 'Zuzuweisender Wert (Empfänger-Name, Tag-Name …)',
  `priority`     INT          NOT NULL DEFAULT 0
                   COMMENT 'Höher = wird zuerst ausgewertet; bei recipient gewinnt der erste Treffer',
  `is_active`    TINYINT(1)   NOT NULL DEFAULT 1,
  `description`  VARCHAR(500) NULL
                   COMMENT 'Erklärung dieser Regel (für Menschen)',
  `created_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                        ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  -- Verhindert doppelte Einträge für identische Regeln (ermöglicht INSERT IGNORE).
  -- Präfix-Länge (80) nötig, da InnoDB in utf8mb4 keinen vollständigen
  -- VARCHAR(255)-Index in einem Composite-Key unterstützt (max. Zeilengröße).
  -- 80 Zeichen reichen für alle eingebauten Regelwerte; eigene Regeln mit
  -- längeren Werten werden anhand des vollständigen id-Felds unterschieden.
  UNIQUE KEY `uq_rule` (
    `rule_type`, `match_field`, `match_mode`,
    `match_value`(80), `assign_value`(80)
  ),
  KEY `idx_rule_type`   (`rule_type`),
  KEY `idx_match_field` (`match_field`),
  KEY `idx_priority`    (`priority` DESC),
  KEY `idx_is_active`   (`is_active`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Flexible Zuweisungsregeln: Empfänger / Tags / Korrespondenten aus der DB';


-- =============================================================================
-- EINGEBAUTE STANDARD-REGELN
-- Werden per INSERT IGNORE eingefügt – bestehende Anpassungen bleiben erhalten.
-- Eigene Regeln einfach mit höherer priority (> 50) in die Tabelle eintragen.
-- Regeln deaktivieren: UPDATE assignment_rules SET is_active=0 WHERE id=...;
--
-- DESIGN-ENTSCHEIDUNG: Keine firmenspezifischen Tag-Regeln.
-- Tags werden primär durch die KI (topic_tags) bestimmt. Die Regeln hier
-- ergänzen strukturell anhand von Dokumenttyp und Schlüsselwörtern.
-- Die Tag-Hierarchie-Expansion (tag_hierarchy) ergänzt automatisch alle
-- Vorfahren-Tags (z.B. Arztbrief → Gesundheit).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- A) TAG-REGELN: Dokumenttyp → Blatt-Tag  (priority 50, match: exact)
-- Mappt den vom LLM bestimmten Dokumenttyp auf einen konkreten Blatt-Tag.
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  ('tag','document_type','Rechnung',               'exact','Rechnung',               50,'Eingehende Rechnungen'),
  ('tag','document_type','Kontoauszug',             'exact','Kontoauszug',             50,'Bankkontoauszüge'),
  ('tag','document_type','Lohnabrechnung',          'exact','Lohnabrechnung',          50,'Lohnabrechnungen'),
  ('tag','document_type','Gehaltsabrechnung',       'exact','Lohnabrechnung',          50,'Gehaltsabrechnungen'),
  ('tag','document_type','Lohnsteuerbescheinigung', 'exact','Lohnsteuerbescheinigung', 50,'Lohnsteuerbescheinigungen'),
  ('tag','document_type','Kreditvertrag',           'exact','Kredit',                  50,'Kreditverträge'),
  ('tag','document_type','Darlehensvertrag',        'exact','Kredit',                  50,'Darlehensverträge'),
  ('tag','document_type','Mahnung',                 'exact','Mahnung',                 50,'Mahnungen'),
  ('tag','document_type','Mahnbescheid',            'exact','Mahnbescheid',            50,'Gerichtliche Mahnbescheide'),
  ('tag','document_type','Quittung',                'exact','Quittung',                50,'Zahlungsquittungen'),
  ('tag','document_type','Angebot',                 'exact','Angebot',                 50,'Kostenvoranschläge'),
  ('tag','document_type','Steuerbescheid',          'exact','Steuerbescheid',          50,'Steuerbescheide'),
  ('tag','document_type','Einkommensteuerbescheid', 'exact','Steuerbescheid',          50,'Einkommensteuerbescheide'),
  ('tag','document_type','Einkommensteuererklärung','exact','Steuererklärung',         50,'Steuererklärungen'),
  ('tag','document_type','Kirchensteuerbescheid',   'exact','Steuerbescheid',          50,'Kirchensteuerbescheide'),
  ('tag','document_type','Umsatzsteuerbescheid',    'exact','Steuerbescheid',          50,'Umsatzsteuerbescheide'),
  ('tag','document_type','Arztbrief',               'exact','Arztbrief',               50,'Arztbriefe'),
  ('tag','document_type','Befundbericht',           'exact','Befund',                  50,'Befundberichte'),
  ('tag','document_type','Krankenhausbericht',      'exact','Krankenhausbericht',      50,'Krankenhausberichte'),
  ('tag','document_type','Entlassungsbrief',        'exact','Entlassungsbrief',        50,'Entlassungsbriefe'),
  ('tag','document_type','Rezept',                  'exact','Rezept',                  50,'Arztrezepte'),
  ('tag','document_type','Überweisungsschein',      'exact','Überweisung',             50,'Arztüberweisungen'),
  ('tag','document_type','Krankenhausrechnung',     'exact','Rechnung',                50,'Krankenhausrechnungen'),
  ('tag','document_type','Therapiebericht',         'exact','Therapiebericht',         50,'Therapieberichte'),
  ('tag','document_type','Versicherungsschein',     'exact','Versicherungsschein',     50,'Versicherungsscheine'),
  ('tag','document_type','Versicherungspolice',     'exact','Versicherungsschein',     50,'Versicherungspolicen'),
  ('tag','document_type','Schadensregulierung',     'exact','Schadensregulierung',     50,'Schadensregulierungen'),
  ('tag','document_type','Schadensmeldung',         'exact','Schadensmeldung',         50,'Schadensmeldungen'),
  ('tag','document_type','Nachtragspolice',         'exact','Versicherungsschein',     50,'Nachtragspolicen'),
  ('tag','document_type','Mietvertrag',             'exact','Mietvertrag',             50,'Mietverträge'),
  ('tag','document_type','Nebenkostenabrechnung',   'exact','Nebenkostenabrechnung',   50,'Nebenkostenabrechnungen'),
  ('tag','document_type','Mieterhöhung',            'exact','Mieterhöhung',            50,'Mieterhöhungsschreiben'),
  ('tag','document_type','Kautionsquittung',        'exact','Kautionsquittung',        50,'Kautionsquittungen'),
  ('tag','document_type','Wohnungskündigung',       'exact','Kündigung',               50,'Wohnungskündigungen'),
  ('tag','document_type','Hausgeldabrechnung',      'exact','Hausgeldabrechnung',      50,'WEG-Hausgeldabrechnungen'),
  ('tag','document_type','Arbeitsvertrag',          'exact','Arbeitsvertrag',          50,'Arbeitsverträge'),
  ('tag','document_type','Zeugnis',                 'exact','Zeugnis',                 50,'Arbeitszeugnisse'),
  ('tag','document_type','Kündigungsschreiben',     'exact','Kündigung',               50,'Kündigungen'),
  ('tag','document_type','Abmahnung',               'exact','Abmahnung',               50,'Abmahnungen'),
  ('tag','document_type','Bescheid',                'exact','Bescheid',                50,'Behördenbescheide'),
  ('tag','document_type','Behördenpost',            'exact','Bescheid',                50,'Behördenpost'),
  ('tag','document_type','Bußgeldbescheid',         'exact','Bußgeldbescheid',         50,'Bußgeldbescheide'),
  ('tag','document_type','Gerichtsschreiben',       'exact','Gerichtsschreiben',       50,'Gerichtliche Korrespondenz'),
  ('tag','document_type','Vollstreckungsbescheid',  'exact','Vollstreckung',           50,'Vollstreckungsbescheide'),
  ('tag','document_type','Fahrzeugbrief',           'exact','Fahrzeugdokument',        50,'Fahrzeugbriefe'),
  ('tag','document_type','Fahrzeugschein',          'exact','Fahrzeugdokument',        50,'Fahrzeugscheine'),
  ('tag','document_type','HU-Bericht',              'exact','HU-TÜV',                  50,'Hauptuntersuchung'),
  ('tag','document_type','Kfz-Steuer',              'exact','KFZ-Steuer',              50,'Kraftfahrzeugsteuer'),
  ('tag','document_type','Mobilfunkvertrag',        'exact','Mobilfunk',               50,'Mobilfunkverträge'),
  ('tag','document_type','Internetvertrag',         'exact','Internet',                50,'Internetverträge'),
  ('tag','document_type','Vertrag',                 'exact','Vertrag',                 50,'Allgemeine Verträge'),
  ('tag','document_type','Kündigung',               'exact','Kündigung',               50,'Allgemeine Kündigungen'),
  ('tag','document_type','Bescheinigung',           'exact','Bescheinigung',           50,'Amtliche Bescheinigungen'),
  ('tag','document_type','Werbung',                 'exact','Werbung',                 50,'Werbung');


-- ---------------------------------------------------------------------------
-- B) TAG-REGELN: Schlüsselwort im OCR-Text → Blatt-Tag  (priority 30)
-- Semantisch, firmenunabhängig. Greift wenn der LLM-Typ nicht reicht.
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  ('tag','keyword','mahnung',             'contains','Mahnung',            30,'Mahnung via Volltext'),
  ('tag','keyword','zahlungserinnerung',  'contains','Mahnung',            30,'Zahlungserinnerung'),
  ('tag','keyword','inkasso',             'contains','Mahnung',            30,'Inkasso via Volltext'),
  ('tag','keyword','vollstreckung',       'contains','Vollstreckung',      30,'Vollstreckung via Volltext'),
  ('tag','keyword','zwangsvollstreckung', 'contains','Vollstreckung',      30,'Zwangsvollstreckung'),
  ('tag','keyword','insolvenz',           'contains','Insolvenz',          30,'Insolvenz via Volltext'),
  ('tag','keyword','kfz-steuer',          'contains','KFZ-Steuer',         30,'Kfz-Steuer via Volltext'),
  ('tag','keyword','kraftfahrzeugsteuer', 'contains','KFZ-Steuer',         30,'Kraftfahrzeugsteuer'),
  ('tag','keyword','hauptuntersuchung',   'contains','HU-TÜV',             30,'TÜV/HU via Volltext'),
  ('tag','keyword','kindergeld',          'contains','Kindergeld',         30,'Kindergeld'),
  ('tag','keyword','elterngeld',          'contains','Elterngeld',         30,'Elterngeld'),
  ('tag','keyword','unterhalt',           'contains','Unterhalt',          30,'Unterhalt'),
  ('tag','keyword','betreuungsgeld',      'contains','Elterngeld',         30,'Betreuungsgeld'),
  ('tag','keyword','rentenbescheid',      'contains','Rentenbescheid',     30,'Rentenbescheid'),
  ('tag','keyword','rentenanpassung',     'contains','Rentenbescheid',     30,'Rentenanpassung'),
  ('tag','keyword','wohngeld',            'contains','Wohngeld',           30,'Wohngeld'),
  ('tag','keyword','sozialhilfe',         'contains','Sozialhilfe',        30,'Sozialhilfe'),
  ('tag','keyword','datenschutz',         'contains','Datenschutz',        30,'Datenschutz-Schreiben'),
  ('tag','keyword','dsgvo',               'contains','Datenschutz',        30,'DSGVO'),
  ('tag','keyword','abmahnung',           'contains','Abmahnung',          30,'Abmahnung erkannt'),
  ('tag','keyword','krankenversicherung', 'contains','Krankenversicherung',30,'Krankenversicherung via Volltext'),
  ('tag','keyword','pflegeversicherung',  'contains','Pflegeversicherung', 30,'Pflegeversicherung'),
  ('tag','keyword','steuernummer',        'contains','Steuerbescheid',     30,'Steuernummer im Text'),
  ('tag','keyword','finanzamt',           'contains','Steuerbescheid',     30,'Finanzamt via Volltext');


-- =============================================================================
-- TAG-HIERARCHIE
-- Jeder Tag hat genau einen Eltern-Tag (Baumstruktur, keine Mehrfachzuordnung).
-- Bei der Zuweisung werden alle Vorfahren automatisch mit hinzugefügt:
--   Arztbrief → Gesundheit
--   Krankenversicherung → Versicherung
--   Mietvertrag → Vertrag
--   Fahrzeugbrief → Fahrzeugdokument → KFZ
-- Eigene Tags hinzufügen:
--   INSERT IGNORE INTO tag_hierarchy (tag, parent_tag) VALUES ('Mein Tag','Finanzen');
-- Hierarchie erweitern:
--   INSERT IGNORE INTO tag_hierarchy (tag, parent_tag) VALUES ('Unterkat','Mein Tag');
-- =============================================================================

CREATE TABLE IF NOT EXISTS `tag_hierarchy` (
  `tag`        VARCHAR(100) NOT NULL COMMENT 'Blatt- oder Zwischen-Tag',
  `parent_tag` VARCHAR(100) NULL     COMMENT 'NULL = Wurzel-Kategorie',
  `sort_order` INT NOT NULL DEFAULT 0 COMMENT 'Sortierung innerhalb des Eltern-Knotens',
  PRIMARY KEY (`tag`),
  KEY `idx_parent` (`parent_tag`)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci
  COMMENT='Tag-Hierarchie: tag → parent_tag. Expansion erfolgt automatisch in logic_gate.';

INSERT IGNORE INTO `tag_hierarchy` (`tag`, `parent_tag`, `sort_order`)
VALUES
  -- Wurzel-Kategorien (parent = NULL)
  ('Finanzen',          NULL,          0),
  ('Gesundheit',        NULL,          1),
  ('Versicherung',      NULL,          2),
  ('Vertrag',           NULL,          3),
  ('Wohnen',            NULL,          4),
  ('Arbeit',            NULL,          5),
  ('Behörde',           NULL,          6),
  ('Sozialleistungen',  NULL,          7),
  ('Recht',             NULL,          8),
  ('KFZ',               NULL,          9),
  ('Telekommunikation', NULL,         10),
  ('Familie',           NULL,         11),
  ('Datenschutz',       NULL,         12),
  -- Finanzen
  ('Rechnung',                'Finanzen',    0),
  ('Kontoauszug',             'Finanzen',    1),
  ('Kredit',                  'Finanzen',    2),
  ('Steuern',                 'Finanzen',    3),
  ('Gehalt',                  'Finanzen',    4),
  ('Lohnabrechnung',          'Gehalt',      0),
  ('Lohnsteuerbescheinigung', 'Steuern',     0),
  ('Steuerbescheid',          'Steuern',     1),
  ('Steuererklärung',         'Steuern',     2),
  ('Mahnung',                 'Finanzen',    5),
  ('Vollstreckung',           'Recht',       0),
  ('Insolvenz',               'Recht',       1),
  ('Mahnbescheid',            'Recht',       2),
  ('Quittung',                'Finanzen',    6),
  ('Angebot',                 'Finanzen',    7),
  -- Gesundheit
  ('Arztbrief',          'Gesundheit',   0),
  ('Befund',             'Gesundheit',   1),
  ('Rezept',             'Gesundheit',   2),
  ('Überweisung',        'Gesundheit',   3),
  ('Therapiebericht',    'Gesundheit',   4),
  ('Krankenhaus',        'Gesundheit',   5),
  ('Krankenhausbericht', 'Krankenhaus',  0),
  ('Entlassungsbrief',   'Krankenhaus',  1),
  -- Versicherung
  ('Krankenversicherung',             'Versicherung',        0),
  ('Gesetzliche Krankenversicherung', 'Krankenversicherung', 0),
  ('Private Krankenversicherung',     'Krankenversicherung', 1),
  ('Pflegeversicherung',              'Versicherung',        1),
  ('KFZ-Versicherung',                'Versicherung',        2),
  ('Haftpflicht',                     'Versicherung',        3),
  ('Lebensversicherung',              'Versicherung',        4),
  ('Hausratversicherung',             'Versicherung',        5),
  ('Rentenversicherung',              'Versicherung',        6),
  ('Versicherungsschein',             'Versicherung',        7),
  ('Schadensmeldung',                 'Versicherung',        8),
  ('Schadensregulierung',             'Versicherung',        9),
  -- Vertrag
  ('Mietvertrag',      'Vertrag', 0),
  ('Arbeitsvertrag',   'Vertrag', 1),
  ('Mobilfunkvertrag', 'Vertrag', 2),
  ('Internetvertrag',  'Vertrag', 3),
  -- Wohnen
  ('Nebenkostenabrechnung', 'Wohnen', 0),
  ('Mieterhöhung',          'Wohnen', 1),
  ('Kautionsquittung',      'Wohnen', 2),
  ('Hausgeldabrechnung',    'Wohnen', 3),
  -- Arbeit
  ('Zeugnis',   'Arbeit', 0),
  ('Abmahnung', 'Arbeit', 1),
  -- Behörde
  ('Bescheid',        'Behörde', 0),
  ('Bußgeldbescheid', 'Behörde', 1),
  ('Bescheinigung',   'Behörde', 2),
  ('Rentenbescheid',  'Behörde', 3),
  -- Sozialleistungen
  ('Kindergeld',  'Sozialleistungen', 0),
  ('Elterngeld',  'Sozialleistungen', 1),
  ('Wohngeld',    'Sozialleistungen', 2),
  ('Sozialhilfe', 'Sozialleistungen', 3),
  ('Unterhalt',   'Sozialleistungen', 4),
  -- Recht
  ('Gerichtsschreiben', 'Recht', 3),
  ('Kündigung',         'Recht', 4),
  -- KFZ
  ('Fahrzeugdokument', 'KFZ',              0),
  ('Fahrzeugbrief',    'Fahrzeugdokument',  0),
  ('Fahrzeugschein',   'Fahrzeugdokument',  1),
  ('KFZ-Steuer',       'KFZ',              1),
  ('HU-TÜV',           'KFZ',              2),
  -- Telekommunikation
  ('Mobilfunk', 'Telekommunikation', 0),
  ('Internet',  'Telekommunikation', 1),
  -- Familie
  ('Betreuung', 'Familie', 0),
  -- Datenschutz
  ('DSGVO', 'Datenschutz', 0);


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
