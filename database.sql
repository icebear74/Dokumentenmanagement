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
                      'organization','keyword') NOT NULL
                   COMMENT 'Gegen welches Dokumentfeld wird geprüft?',
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
  -- Verhindert doppelte Einträge für identische Regeln (ermöglicht INSERT IGNORE)
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
-- =============================================================================

-- ---------------------------------------------------------------------------
-- A) TAG-REGELN: Dokumenttyp → Tag  (priority 50, match: exact)
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  -- Finanzen
  ('tag','document_type','Rechnung',              'exact','Rechnung',              50,'Eingehende Rechnungen'),
  ('tag','document_type','Kontoauszug',            'exact','Kontoauszug',            50,'Bankkontoauszüge'),
  ('tag','document_type','Lohnabrechnung',         'exact','Gehalt',                 50,'Gehalts-/Lohnabrechnungen'),
  ('tag','document_type','Gehaltsabrechnung',      'exact','Gehalt',                 50,'Gehalts-/Lohnabrechnungen'),
  ('tag','document_type','Lohnsteuerbescheinigung','exact','Steuern',                50,'Lohnsteuerbescheinigungen'),
  ('tag','document_type','Kreditvertrag',          'exact','Kredit',                 50,'Kredite und Darlehen'),
  ('tag','document_type','Darlehensvertrag',       'exact','Kredit',                 50,'Darlehensverträge'),
  ('tag','document_type','Mahnung',                'exact','Mahnung',                50,'Zahlungsmahnung'),
  ('tag','document_type','Mahnbescheid',           'exact','Mahnung',                50,'Gerichtlicher Mahnbescheid'),
  ('tag','document_type','Quittung',               'exact','Quittung',               50,'Zahlungsquittungen'),
  ('tag','document_type','Angebot',                'exact','Angebot',                50,'Kostenvoranschläge und Angebote'),
  -- Steuern
  ('tag','document_type','Steuerbescheid',             'exact','Steuern', 50,'Steuerbescheide allgemein'),
  ('tag','document_type','Einkommensteuerbescheid',    'exact','Steuern', 50,'Einkommensteuerbescheide'),
  ('tag','document_type','Einkommensteuererklärung',   'exact','Steuern', 50,'Steuererklärungen'),
  ('tag','document_type','Kirchensteuerbescheid',      'exact','Steuern', 50,'Kirchensteuerbescheide'),
  ('tag','document_type','Umsatzsteuerbescheid',       'exact','Steuern', 50,'Umsatzsteuerbescheide'),
  -- Gesundheit
  ('tag','document_type','Arztbrief',          'exact','Gesundheit', 50,'Arztbriefe'),
  ('tag','document_type','Befundbericht',      'exact','Gesundheit', 50,'Medizinische Befundberichte'),
  ('tag','document_type','Krankenhausbericht', 'exact','Gesundheit', 50,'Krankenhausberichte'),
  ('tag','document_type','Entlassungsbrief',   'exact','Gesundheit', 50,'Entlassungsbriefe aus Krankenhaus'),
  ('tag','document_type','Rezept',             'exact','Gesundheit', 50,'Arztrezepte'),
  ('tag','document_type','Überweisungsschein', 'exact','Gesundheit', 50,'Facharzt-Überweisungen'),
  ('tag','document_type','Krankenhausrechnung','exact','Gesundheit', 50,'Krankenhausrechnungen'),
  ('tag','document_type','Therapiebericht',    'exact','Gesundheit', 50,'Therapieberichte'),
  -- Versicherungen
  ('tag','document_type','Versicherungsschein',  'exact','Versicherung', 50,'Versicherungsscheine'),
  ('tag','document_type','Versicherungspolice',  'exact','Versicherung', 50,'Versicherungspolicen'),
  ('tag','document_type','Schadensregulierung',  'exact','Versicherung', 50,'Schadensregulierungen'),
  ('tag','document_type','Schadensmeldung',      'exact','Versicherung', 50,'Schadensmeldungen'),
  ('tag','document_type','Nachtragspolice',      'exact','Versicherung', 50,'Nachtragspolicen'),
  -- Wohnen / Immobilien
  ('tag','document_type','Mietvertrag',            'exact','Wohnen', 50,'Mietverträge'),
  ('tag','document_type','Nebenkostenabrechnung',  'exact','Wohnen', 50,'Nebenkostenabrechnungen'),
  ('tag','document_type','Mieterhöhung',           'exact','Wohnen', 50,'Mieterhöhungsschreiben'),
  ('tag','document_type','Kautionsquittung',       'exact','Wohnen', 50,'Kautionsquittungen'),
  ('tag','document_type','Wohnungskündigung',      'exact','Wohnen', 50,'Wohnungskündigungen'),
  ('tag','document_type','Hausgeldabrechnung',     'exact','Wohnen', 50,'WEG-Hausgeldabrechnungen'),
  -- Arbeit / Beruf
  ('tag','document_type','Arbeitsvertrag',     'exact','Arbeit', 50,'Arbeitsverträge'),
  ('tag','document_type','Zeugnis',            'exact','Arbeit', 50,'Arbeitszeugnisse'),
  ('tag','document_type','Kündigungsschreiben','exact','Kündigung', 50,'Arbeitgeberkündigungen'),
  ('tag','document_type','Abmahnung',          'exact','Arbeit', 50,'Arbeitsrechtliche Abmahnungen'),
  -- Behörden / Recht
  ('tag','document_type','Bescheid',               'exact','Behörde',      50,'Behördenbescheide allgemein'),
  ('tag','document_type','Behördenpost',           'exact','Behörde',      50,'Post von Behörden'),
  ('tag','document_type','Bußgeldbescheid',        'exact','Bußgeld',      50,'Bußgeldbescheide'),
  ('tag','document_type','Gerichtsschreiben',      'exact','Recht',        50,'Gerichtliche Korrespondenz'),
  ('tag','document_type','Vollstreckungsbescheid', 'exact','Vollstreckung',50,'Vollstreckungsbescheide'),
  ('tag','document_type','Mahnbescheid',           'exact','Mahnung',      50,'Gerichtliche Mahnbescheide'),
  -- KFZ / Fahrzeuge
  ('tag','document_type','Fahrzeugbrief',   'exact','KFZ', 50,'Fahrzeugbriefe / Zulassungsbescheinigungen'),
  ('tag','document_type','Fahrzeugschein',  'exact','KFZ', 50,'Fahrzeugscheine'),
  ('tag','document_type','HU-Bericht',      'exact','KFZ', 50,'Hauptuntersuchung (TÜV)'),
  ('tag','document_type','Kfz-Steuer',      'exact','KFZ', 50,'Kraftfahrzeugsteuerbescheide'),
  -- Telekommunikation / Internet
  ('tag','document_type','Mobilfunkvertrag', 'exact','Telekommunikation', 50,'Mobilfunkverträge'),
  ('tag','document_type','Internetvertrag',  'exact','Telekommunikation', 50,'Internetverträge'),
  -- Sonstiges
  ('tag','document_type','Vertrag',       'exact','Vertrag',      50,'Allgemeine Verträge'),
  ('tag','document_type','Kündigung',     'exact','Kündigung',    50,'Allgemeine Kündigungen'),
  ('tag','document_type','Bescheinigung', 'exact','Bescheinigung',50,'Amtliche Bescheinigungen'),
  ('tag','document_type','Werbung',       'exact','Werbung',      50,'Werbung und Marketing');


-- ---------------------------------------------------------------------------
-- B) TAG-REGELN: Organisation → Tag  (priority 40, match: contains)
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  -- Gesetzliche Krankenversicherungen
  ('tag','organization','Techniker Krankenkasse', 'contains','Krankenversicherung', 40,'TK → Krankenversicherung'),
  ('tag','organization','Barmer',                 'contains','Krankenversicherung', 40,'Barmer → Krankenversicherung'),
  ('tag','organization','AOK',                    'contains','Krankenversicherung', 40,'AOK → Krankenversicherung'),
  ('tag','organization','DAK',                    'contains','Krankenversicherung', 40,'DAK → Krankenversicherung'),
  ('tag','organization','IKK',                    'contains','Krankenversicherung', 40,'IKK → Krankenversicherung'),
  ('tag','organization','Knappschaft',            'contains','Krankenversicherung', 40,'Knappschaft → Krankenversicherung'),
  ('tag','organization','BKK',                    'contains','Krankenversicherung', 40,'BKK → Krankenversicherung'),
  ('tag','organization','Hanseatische Krankenkasse','contains','Krankenversicherung',40,'HEK → Krankenversicherung'),
  ('tag','organization','hkk',                    'contains','Krankenversicherung', 40,'hkk → Krankenversicherung'),
  -- Deutsche Rentenversicherung
  ('tag','organization','Deutsche Rentenversicherung','contains','Rente', 40,'DRV → Rente'),
  ('tag','organization','Rentenversicherung',          'contains','Rente', 40,'Rentenversicherung → Rente'),
  -- Bundesagentur für Arbeit / Jobcenter
  ('tag','organization','Bundesagentur für Arbeit','contains','Arbeitsagentur', 40,'BA für Arbeit'),
  ('tag','organization','Jobcenter',               'contains','Jobcenter',      40,'Jobcenter → Sozialleistungen'),
  -- Finanzamt / Steuerbehörden
  ('tag','organization','Finanzamt',               'contains','Steuern', 40,'Finanzamt → Steuern'),
  ('tag','organization','Bundeszentralamt',        'contains','Steuern', 40,'Bundeszentralamt für Steuern'),
  -- Gerichte / Recht
  ('tag','organization','Amtsgericht',             'contains','Recht', 40,'Amtsgericht → Recht'),
  ('tag','organization','Landgericht',             'contains','Recht', 40,'Landgericht → Recht'),
  ('tag','organization','Oberlandesgericht',       'contains','Recht', 40,'OLG → Recht'),
  ('tag','organization','Bundesgericht',           'contains','Recht', 40,'Bundesgericht → Recht'),
  ('tag','organization','Rechtsanwalt',            'contains','Recht', 40,'Anwaltskanzlei → Recht'),
  ('tag','organization','Inkasso',                 'contains','Mahnung', 40,'Inkasso-Büro → Mahnung'),
  -- Sonstige Behörden
  ('tag','organization','Ordnungsamt',             'contains','Behörde', 40,'Ordnungsamt → Behörde'),
  ('tag','organization','Bürgeramt',               'contains','Behörde', 40,'Bürgeramt → Behörde'),
  ('tag','organization','Einwohnermeldeamt',       'contains','Behörde', 40,'Einwohnermeldeamt → Behörde'),
  ('tag','organization','Jugendamt',               'contains','Behörde', 40,'Jugendamt → Behörde'),
  ('tag','organization','Sozialamt',               'contains','Behörde', 40,'Sozialamt → Sozialleistungen'),
  ('tag','organization','Versorgungsamt',          'contains','Behörde', 40,'Versorgungsamt → Behörde'),
  ('tag','organization','Zoll',                    'contains','Behörde', 40,'Zollbehörde → Behörde'),
  -- Energie / Versorger
  ('tag','organization','Stadtwerke', 'contains','Energie', 40,'Stadtwerke → Energie'),
  ('tag','organization','E.ON',       'contains','Energie', 40,'E.ON → Energie'),
  ('tag','organization','RWE',        'contains','Energie', 40,'RWE → Energie'),
  ('tag','organization','EnBW',       'contains','Energie', 40,'EnBW → Energie'),
  ('tag','organization','Vattenfall', 'contains','Energie', 40,'Vattenfall → Energie'),
  ('tag','organization','Innogy',     'contains','Energie', 40,'Innogy → Energie'),
  ('tag','organization','Eon',        'contains','Energie', 40,'Eon → Energie'),
  -- Telekommunikation
  ('tag','organization','Telekom',  'contains','Telekommunikation', 40,'Telekom → Telekommunikation'),
  ('tag','organization','Vodafone', 'contains','Telekommunikation', 40,'Vodafone → Telekommunikation'),
  ('tag','organization','O2',       'contains','Telekommunikation', 40,'O2 → Telekommunikation'),
  ('tag','organization','1&1',      'contains','Telekommunikation', 40,'1&1 → Telekommunikation'),
  ('tag','organization','Freenet',  'contains','Telekommunikation', 40,'Freenet → Telekommunikation'),
  ('tag','organization','Unitymedia','contains','Telekommunikation',40,'Unitymedia → Telekommunikation'),
  ('tag','organization','Congstar', 'contains','Telekommunikation', 40,'Congstar → Telekommunikation'),
  -- Banken
  ('tag','organization','Sparkasse',     'contains','Bank', 40,'Sparkasse → Bank'),
  ('tag','organization','Volksbank',     'contains','Bank', 40,'Volksbank → Bank'),
  ('tag','organization','Raiffeisenbank','contains','Bank', 40,'Raiffeisenbank → Bank'),
  ('tag','organization','Commerzbank',   'contains','Bank', 40,'Commerzbank → Bank'),
  ('tag','organization','Deutsche Bank', 'contains','Bank', 40,'Deutsche Bank → Bank'),
  ('tag','organization','DKB',           'contains','Bank', 40,'DKB → Bank'),
  ('tag','organization','ING',           'contains','Bank', 40,'ING → Bank'),
  ('tag','organization','Postbank',      'contains','Bank', 40,'Postbank → Bank'),
  ('tag','organization','Comdirect',     'contains','Bank', 40,'Comdirect → Bank'),
  ('tag','organization','N26',           'contains','Bank', 40,'N26 → Bank'),
  ('tag','organization','Targobank',     'contains','Bank', 40,'Targobank → Bank'),
  -- Versicherungen
  ('tag','organization','Allianz',   'contains','Versicherung', 40,'Allianz → Versicherung'),
  ('tag','organization','HUK',       'contains','Versicherung', 40,'HUK → Versicherung'),
  ('tag','organization','AXA',       'contains','Versicherung', 40,'AXA → Versicherung'),
  ('tag','organization','Generali',  'contains','Versicherung', 40,'Generali → Versicherung'),
  ('tag','organization','Zurich',    'contains','Versicherung', 40,'Zurich → Versicherung'),
  ('tag','organization','R+V',       'contains','Versicherung', 40,'R+V → Versicherung'),
  ('tag','organization','DEVK',      'contains','Versicherung', 40,'DEVK → Versicherung'),
  ('tag','organization','Ergo',      'contains','Versicherung', 40,'Ergo → Versicherung'),
  ('tag','organization','Debeka',    'contains','Versicherung', 40,'Debeka → Versicherung'),
  ('tag','organization','Signal Iduna','contains','Versicherung',40,'Signal Iduna → Versicherung'),
  ('tag','organization','VHV',       'contains','Versicherung', 40,'VHV → Versicherung'),
  ('tag','organization','Gothaer',   'contains','Versicherung', 40,'Gothaer → Versicherung'),
  -- Rundfunkbeitrag
  ('tag','organization','Beitragsservice','contains','Rundfunkbeitrag', 40,'ARD ZDF GEZ → Rundfunkbeitrag'),
  ('tag','organization','GEZ',            'contains','Rundfunkbeitrag', 40,'GEZ → Rundfunkbeitrag');


-- ---------------------------------------------------------------------------
-- C) TAG-REGELN: Schlüsselwort im OCR-Text → Tag  (priority 30, contains)
-- Greifen wenn Dokumenttyp/Organisation nichts passendes liefert.
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  ('tag','keyword','mahnung',            'contains','Mahnung',           30,'Mahnungserkennung via Volltext'),
  ('tag','keyword','zahlungserinnerung', 'contains','Mahnung',           30,'Zahlungserinnerung via Volltext'),
  ('tag','keyword','inkasso',            'contains','Mahnung',           30,'Inkasso-Schreiben via Volltext'),
  ('tag','keyword','vollstreckung',      'contains','Vollstreckung',     30,'Vollstreckungsmaßnahme via Volltext'),
  ('tag','keyword','zwangsvollstreckung','contains','Vollstreckung',     30,'Zwangsvollstreckung via Volltext'),
  ('tag','keyword','insolvenz',          'contains','Insolvenz',         30,'Insolvenz-Schreiben via Volltext'),
  ('tag','keyword','kfz-steuer',         'contains','KFZ',               30,'Kfz-Steuer → KFZ via Volltext'),
  ('tag','keyword','kraftfahrzeugsteuer','contains','KFZ',               30,'Kraftfahrzeugsteuer → KFZ'),
  ('tag','keyword','fahrzeugschein',     'contains','KFZ',               30,'Fahrzeugschein → KFZ'),
  ('tag','keyword','hauptuntersuchung',  'contains','KFZ',               30,'TÜV/HU → KFZ'),
  ('tag','keyword','kindergeld',         'contains','Familie',           30,'Kindergeld → Familie'),
  ('tag','keyword','elterngeld',         'contains','Familie',           30,'Elterngeld → Familie'),
  ('tag','keyword','unterhalt',          'contains','Familie',           30,'Unterhalt → Familie'),
  ('tag','keyword','betreuungsgeld',     'contains','Familie',           30,'Betreuungsgeld → Familie'),
  ('tag','keyword','rentenbescheid',     'contains','Rente',             30,'Rentenbescheid → Rente'),
  ('tag','keyword','rentenanpassung',    'contains','Rente',             30,'Rentenanpassung → Rente'),
  ('tag','keyword','wohngeld',           'contains','Sozialleistungen',  30,'Wohngeld → Sozialleistungen'),
  ('tag','keyword','sozialhilfe',        'contains','Sozialleistungen',  30,'Sozialhilfe → Sozialleistungen'),
  ('tag','keyword','datenschutz',        'contains','Datenschutz',       30,'Datenschutz-bezogen'),
  ('tag','keyword','dsgvo',              'contains','Datenschutz',       30,'DSGVO-Schreiben'),
  ('tag','keyword','abmahnung',          'contains','Abmahnung',         30,'Abmahnung erkannt (Arbeit oder Recht)'),
  ('tag','keyword','beitragsrechnung',   'contains','Versicherung',      30,'Versicherungsbeitragsrechnung'),
  ('tag','keyword','krankenversicherung','contains','Krankenversicherung',30,'Krankenversicherung via Volltext'),
  ('tag','keyword','pflegeversicherung', 'contains','Pflegeversicherung',30,'Pflegeversicherung via Volltext'),
  ('tag','keyword','steuernummer',       'contains','Steuern',           30,'Steuernummer → Steuer-Dokument'),
  ('tag','keyword','steuer-id',          'contains','Steuern',           30,'Steuer-ID → Steuer-Dokument'),
  ('tag','keyword','finanzamt',          'contains','Steuern',           30,'Finanzamt via Volltext');


-- ---------------------------------------------------------------------------
-- D) KORRESPONDENTEN-REGELN: Absender-Normalisierung  (priority 20, contains)
-- Sorgt für einheitliche Korrespondentennamen in Paperless unabhängig
-- von der genauen Absenderangabe im Dokument.
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO `assignment_rules`
  (`rule_type`, `match_field`, `match_value`, `match_mode`, `assign_value`, `priority`, `description`)
VALUES
  ('correspondent','sender','Techniker Krankenkasse',    'contains','Techniker Krankenkasse',    20,'TK-Varianten normieren'),
  ('correspondent','sender','Barmer',                    'contains','Barmer',                    20,'Barmer normieren'),
  ('correspondent','sender','AOK',                       'contains','AOK',                       20,'AOK normieren'),
  ('correspondent','sender','DAK',                       'contains','DAK-Gesundheit',             20,'DAK normieren'),
  ('correspondent','sender','IKK',                       'contains','IKK',                       20,'IKK normieren'),
  ('correspondent','sender','Deutsche Rentenversicherung','contains','Deutsche Rentenversicherung',20,'DRV normieren'),
  ('correspondent','sender','Bundesagentur für Arbeit',  'contains','Bundesagentur für Arbeit',  20,'BA für Arbeit normieren'),
  ('correspondent','sender','Jobcenter',                 'contains','Jobcenter',                 20,'Jobcenter normieren'),
  ('correspondent','sender','Finanzamt',                 'contains','Finanzamt',                 20,'Finanzamt normieren'),
  ('correspondent','sender','Amtsgericht',               'contains','Amtsgericht',               20,'Amtsgericht normieren'),
  ('correspondent','sender','Landgericht',               'contains','Landgericht',               20,'Landgericht normieren'),
  ('correspondent','sender','Allianz',                   'contains','Allianz',                   20,'Allianz normieren'),
  ('correspondent','sender','HUK',                       'contains','HUK-COBURG',                20,'HUK-Varianten normieren'),
  ('correspondent','sender','AXA',                       'contains','AXA',                       20,'AXA normieren'),
  ('correspondent','sender','DEVK',                      'contains','DEVK',                      20,'DEVK normieren'),
  ('correspondent','sender','Ergo',                      'contains','Ergo',                      20,'Ergo normieren'),
  ('correspondent','sender','Debeka',                    'contains','Debeka',                    20,'Debeka normieren'),
  ('correspondent','sender','Signal Iduna',              'contains','Signal Iduna',               20,'Signal Iduna normieren'),
  ('correspondent','sender','Stadtwerke',                'contains','Stadtwerke',                20,'Stadtwerke (lokal) normieren'),
  ('correspondent','sender','Telekom',                   'contains','Deutsche Telekom',           20,'Telekom normieren'),
  ('correspondent','sender','Vodafone',                  'contains','Vodafone',                  20,'Vodafone normieren'),
  ('correspondent','sender','1&1',                       'contains','1&1',                       20,'1&1 normieren'),
  ('correspondent','sender','Sparkasse',                 'contains','Sparkasse',                 20,'Sparkasse normieren'),
  ('correspondent','sender','Volksbank',                 'contains','Volksbank',                 20,'Volksbank normieren'),
  ('correspondent','sender','Commerzbank',               'contains','Commerzbank',               20,'Commerzbank normieren'),
  ('correspondent','sender','Deutsche Bank',             'contains','Deutsche Bank',             20,'Deutsche Bank normieren'),
  ('correspondent','sender','DKB',                       'contains','DKB Deutsche Kreditbank',   20,'DKB normieren'),
  ('correspondent','sender','ING',                       'contains','ING',                       20,'ING normieren'),
  ('correspondent','sender','Postbank',                  'contains','Postbank',                  20,'Postbank normieren'),
  ('correspondent','sender','Beitragsservice',           'contains','ARD ZDF Beitragsservice',   20,'GEZ/Beitragsservice normieren');



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
