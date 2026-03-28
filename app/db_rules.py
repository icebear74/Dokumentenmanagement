"""
db_rules.py – Datenbankbasierte Klassifizierungsregeln und LLM-Prompts.

Alle Zuweisungsregeln (Empfänger, Tags, Korrespondenten) und sämtliche
LLM-System-Prompts werden aus der Datenbank geladen und dort verwaltet.
Änderungen wirken sofort – kein Container-Neustart erforderlich.

Kerntabellen:
  llm_prompts      – System-Prompts und User-Templates pro Dienst
  assignment_rules – Flexible Zuweisungsregeln (Regex, Exact, Contains …)

Verwendung:
    from db_rules import RulesCache, apply_rules, ensure_defaults

    cache = RulesCache(lambda: mysql.connector.connect(...), ttl_seconds=60)
    rules = cache.get_rules(rule_type="recipient")
    assignments = apply_rules(doc_fields, rules)
    system_prompt = cache.get_prompt("ai_analyzer", "default", "system",
                                     default=FALLBACK_PROMPT)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import mysql.connector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class AssignmentRule:
    """Eine einzelne Zuweisungsregel aus der Tabelle `assignment_rules`."""
    id: int
    rule_type: str        # 'recipient' | 'tag' | 'correspondent'
    match_field: str      # 'sender' | 'recipient_name' | 'document_type' |
    #                       'organization' | 'keyword' | 'llm_tag'
    #                       'llm_tag' prüft gegen LLM-extrahierte topic_tags
    match_value: str      # Zu suchender Wert (case-insensitive)
    match_mode: str       # 'exact' | 'contains' | 'startswith' | 'regex'
    assign_value: str     # Zuzuweisender Wert
    priority: int         # Höher = früher ausgewertet
    description: str | None = None


@dataclass
class LLMPrompt:
    """Ein LLM-Prompt-Eintrag aus der Tabelle `llm_prompts`."""
    id: int
    service: str          # 'ai_analyzer' | 'pre_classifier' | …
    name: str             # 'default' | 'phase1' | 'phase2' | …
    prompt_type: str      # 'system' | 'user_template'
    content: str
    version: int


# ---------------------------------------------------------------------------
# TTL-Cache für Regeln und Prompts
# ---------------------------------------------------------------------------

class RulesCache:
    """Thread-sicherer Cache für DB-Regeln und Prompts mit TTL-Refresh.

    Alle Dienste (logic_gate, ai_analyzer, pre_classifier) nutzen dieselbe
    Klasse, jeweils mit eigener Instanz und DB-Verbindungsfabrik.
    """

    def __init__(
        self,
        conn_factory: Callable[[], mysql.connector.MySQLConnection],
        ttl_seconds: int = 60,
    ) -> None:
        self._factory = conn_factory
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._rules: list[AssignmentRule] = []
        self._prompts: dict[tuple[str, str, str], str] = {}
        # tag → parent_tag (None = root).  Loaded from tag_hierarchy table.
        self._hierarchy: dict[str, str | None] = {}
        self._loaded_at: float = 0.0

    # ------------------------------------------------------------------
    # Öffentliche API
    # ------------------------------------------------------------------

    def get_rules(self, rule_type: str | None = None) -> list[AssignmentRule]:
        """Gibt aktive Zuweisungsregeln zurück (nach priority DESC sortiert)."""
        self._refresh_if_stale()
        with self._lock:
            if rule_type:
                return [r for r in self._rules if r.rule_type == rule_type]
            return list(self._rules)

    def get_hierarchy(self) -> dict[str, str | None]:
        """Gibt die Tag-Hierarchie zurück: {tag → parent_tag | None}."""
        self._refresh_if_stale()
        with self._lock:
            return dict(self._hierarchy)

    def get_prompt(
        self,
        service: str,
        name: str,
        prompt_type: str,
        default: str = "",
    ) -> str:
        """Gibt den Prompt-Text für den angegebenen Dienst zurück.

        Falls kein Eintrag in der DB existiert, wird `default` zurückgegeben.
        """
        self._refresh_if_stale()
        with self._lock:
            key = (service, name, prompt_type)
            return self._prompts.get(key, default)

    def invalidate(self) -> None:
        """Erzwingt sofortiges Neuladen beim nächsten Zugriff."""
        with self._lock:
            self._loaded_at = 0.0

    # ------------------------------------------------------------------
    # Interne Lade-Logik
    # ------------------------------------------------------------------

    def _refresh_if_stale(self) -> None:
        if time.time() - self._loaded_at < self._ttl:
            return
        try:
            conn = self._factory()
            try:
                self._load_from_db(conn)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "Regeln/Prompts konnten nicht aus DB geladen werden: %s. "
                "Verwende gecachte Daten.",
                exc,
            )
            # Bei Fehler trotzdem TTL hochsetzen, um Flood zu vermeiden
            with self._lock:
                self._loaded_at = time.time()

    def _load_from_db(self, conn: mysql.connector.MySQLConnection) -> None:
        rules: list[AssignmentRule] = []
        prompts: dict[tuple[str, str, str], str] = {}
        hierarchy: dict[str, str | None] = {}

        cursor = conn.cursor(dictionary=True)

        # Regeln laden
        cursor.execute(
            """
            SELECT id, rule_type, match_field, match_value, match_mode,
                   assign_value, priority, description
            FROM assignment_rules
            WHERE is_active = 1
            ORDER BY priority DESC, id ASC
            """
        )
        for row in cursor.fetchall():
            rules.append(
                AssignmentRule(
                    id=int(row["id"]),
                    rule_type=str(row["rule_type"]),
                    match_field=str(row["match_field"]),
                    match_value=str(row["match_value"]),
                    match_mode=str(row["match_mode"]),
                    assign_value=str(row["assign_value"]),
                    priority=int(row["priority"]),
                    description=row.get("description"),
                )
            )

        # Prompts laden
        cursor.execute(
            """
            SELECT service, name, prompt_type, content
            FROM llm_prompts
            WHERE is_active = 1
            """
        )
        for row in cursor.fetchall():
            key = (str(row["service"]), str(row["name"]), str(row["prompt_type"]))
            prompts[key] = str(row["content"])

        # Tag-Hierarchie laden (optional – Tabelle muss existieren)
        try:
            cursor.execute(
                "SELECT tag, parent_tag FROM tag_hierarchy ORDER BY sort_order ASC"
            )
            for row in cursor.fetchall():
                hierarchy[str(row["tag"])] = (
                    str(row["parent_tag"]) if row["parent_tag"] else None
                )
        except Exception as exc:
            logger.warning(
                "tag_hierarchy konnte nicht geladen werden: %s. "
                "Hierarchie-Expansion deaktiviert.",
                exc,
            )

        cursor.close()

        with self._lock:
            self._rules = rules
            self._prompts = prompts
            self._hierarchy = hierarchy
            self._loaded_at = time.time()

        logger.debug(
            "DB-Cache aktualisiert: %d Regeln, %d Prompts, %d Hierarchie-Einträge",
            len(rules),
            len(prompts),
            len(hierarchy),
        )


# ---------------------------------------------------------------------------
# Zuweisungs-Engine
# ---------------------------------------------------------------------------

def apply_rules(
    doc_fields: dict[str, Any],
    rules: list[AssignmentRule],
) -> dict[str, list[str]]:
    """Wendet Zuweisungsregeln auf ein Dokument an.

    Die LLM-extrahierten `topic_tags` werden IMMER direkt als Tags
    übernommen (höchste semantische Relevanz). DB-Regeln ergänzen diese
    um strukturelle Tags (Dokumenttyp, Schlüsselwörter) und bestimmen
    Empfänger und Korrespondenten.

    Args:
        doc_fields: Dict mit Feldern des Dokuments:
            - sender (str)
            - recipient_names (list[str])
            - document_type (str)
            - topic_tags (list[str])  – direkt vom LLM; werden als Tags übernommen
            - organizations (list[str])
            - ocr_text (str, optional – für keyword-Regeln)
        rules: Sortierte Liste aktiver AssignmentRule (priority DESC).

    Returns:
        Dict mit Listen der zugewiesenen Werte:
            - recipient:      Nur der erste Treffer (höchste Priorität)
            - tags:           LLM topic_tags + alle Rule-Treffer, dedupliziert
            - correspondents: Alle Treffer, dedupliziert
    """
    sender = str(doc_fields.get("sender") or "").strip()
    doc_type = str(doc_fields.get("document_type") or "").strip()
    recipient_names: list[str] = [
        str(n) for n in doc_fields.get("recipient_names", []) if n
    ]
    topic_tags: list[str] = [
        str(t).strip() for t in doc_fields.get("topic_tags", []) if t
    ]
    organizations: list[str] = [
        str(o) for o in doc_fields.get("organizations", []) if o
    ]
    ocr_text = str(doc_fields.get("ocr_text") or "")

    # LLM-Tags sind die primäre, semantisch reichhaltige Quelle –
    # direkt übernehmen, kein Regelabgleich nötig.
    result: dict[str, list[str]] = {
        "recipient": [],
        "tags": list(topic_tags),          # LLM-Tags sofort einsetzen
        "correspondents": [],
    }

    for rule in rules:
        # Zielfelder für diese Regel bestimmen
        if rule.match_field == "sender":
            targets = [sender] if sender else []
        elif rule.match_field == "recipient_name":
            targets = recipient_names
        elif rule.match_field == "document_type":
            targets = [doc_type] if doc_type else []
        elif rule.match_field == "organization":
            targets = organizations
        elif rule.match_field == "keyword":
            targets = [ocr_text] if ocr_text else []
        elif rule.match_field == "llm_tag":
            # Prüfe gegen die LLM-extrahierten topic_tags
            targets = topic_tags
        else:
            continue

        if not targets:
            continue

        matched = any(_matches(t, rule.match_value, rule.match_mode) for t in targets)
        if not matched:
            continue

        value = rule.assign_value
        if rule.rule_type == "recipient":
            # Nur den ersten Empfänger-Treffer übernehmen (höchste Priorität)
            if not result["recipient"]:
                result["recipient"].append(value)
        elif rule.rule_type == "tag":
            if value not in result["tags"]:
                result["tags"].append(value)
        elif rule.rule_type == "correspondent":
            if value not in result["correspondents"]:
                result["correspondents"].append(value)

    return result


def _matches(target: str, pattern: str, mode: str) -> bool:
    """Vergleicht `target` mit `pattern` entsprechend dem Modus."""
    t = target.lower()
    p = pattern.lower()
    if mode == "exact":
        return t == p
    if mode == "contains":
        return p in t
    if mode == "startswith":
        return t.startswith(p)
    if mode == "regex":
        try:
            return bool(re.search(pattern, target, re.IGNORECASE))
        except re.error:
            logger.warning("Ungültiges Regex-Muster in assignment_rules: %r", pattern)
            return False
    return False


# ---------------------------------------------------------------------------
# Eingebaute Standard-Regeln – nur SEMANTISCH, KEINE firmenbezogenen Regeln.
# Tags kommen primär vom LLM (topic_tags). Diese Regeln ergänzen strukturell.
# Format: (rule_type, match_field, match_value, match_mode, assign_value,
#           priority, description)
# ---------------------------------------------------------------------------
_BUILTIN_RULES: list[tuple[str, str, str, str, str, int, str]] = [
    # ── A) Dokumenttyp → Blatt-Tag  (LLM gibt Dokumenttyp, Regel mappt auf Tag)
    # Finanzen
    ("tag","document_type","Rechnung",               "exact","Rechnung",               50,"Eingehende Rechnungen"),
    ("tag","document_type","Kontoauszug",             "exact","Kontoauszug",             50,"Bankkontoauszüge"),
    ("tag","document_type","Lohnabrechnung",          "exact","Lohnabrechnung",          50,"Lohnabrechnungen"),
    ("tag","document_type","Gehaltsabrechnung",       "exact","Lohnabrechnung",          50,"Gehaltsabrechnungen"),
    ("tag","document_type","Lohnsteuerbescheinigung", "exact","Lohnsteuerbescheinigung", 50,"Lohnsteuerbescheinigungen"),
    ("tag","document_type","Kreditvertrag",           "exact","Kredit",                  50,"Kreditverträge"),
    ("tag","document_type","Darlehensvertrag",        "exact","Kredit",                  50,"Darlehensverträge"),
    ("tag","document_type","Mahnung",                 "exact","Mahnung",                 50,"Mahnungen"),
    ("tag","document_type","Mahnbescheid",            "exact","Mahnbescheid",            50,"Gerichtliche Mahnbescheide"),
    ("tag","document_type","Quittung",                "exact","Quittung",                50,"Zahlungsquittungen"),
    ("tag","document_type","Angebot",                 "exact","Angebot",                 50,"Kostenvoranschläge"),
    # Steuern
    ("tag","document_type","Steuerbescheid",              "exact","Steuerbescheid",          50,"Steuerbescheide"),
    ("tag","document_type","Einkommensteuerbescheid",     "exact","Steuerbescheid",          50,"Einkommensteuerbescheide"),
    ("tag","document_type","Einkommensteuererklärung",    "exact","Steuererklärung",         50,"Steuererklärungen"),
    ("tag","document_type","Kirchensteuerbescheid",       "exact","Steuerbescheid",          50,"Kirchensteuerbescheide"),
    ("tag","document_type","Umsatzsteuerbescheid",        "exact","Steuerbescheid",          50,"Umsatzsteuerbescheide"),
    # Gesundheit
    ("tag","document_type","Arztbrief",           "exact","Arztbrief",           50,"Arztbriefe"),
    ("tag","document_type","Befundbericht",        "exact","Befund",              50,"Befundberichte"),
    ("tag","document_type","Krankenhausbericht",   "exact","Krankenhausbericht",  50,"Krankenhausberichte"),
    ("tag","document_type","Entlassungsbrief",     "exact","Entlassungsbrief",    50,"Entlassungsbriefe"),
    ("tag","document_type","Rezept",               "exact","Rezept",              50,"Arztrezepte"),
    ("tag","document_type","Überweisungsschein",   "exact","Überweisung",         50,"Arztüberweisungen"),
    ("tag","document_type","Krankenhausrechnung",  "exact","Rechnung",            50,"Krankenhausrechnungen"),
    ("tag","document_type","Therapiebericht",      "exact","Therapiebericht",     50,"Therapieberichte"),
    # Versicherungen
    ("tag","document_type","Versicherungsschein",  "exact","Versicherungsschein", 50,"Versicherungsscheine"),
    ("tag","document_type","Versicherungspolice",  "exact","Versicherungsschein", 50,"Versicherungspolicen"),
    ("tag","document_type","Schadensregulierung",  "exact","Schadensregulierung", 50,"Schadensregulierungen"),
    ("tag","document_type","Schadensmeldung",      "exact","Schadensmeldung",     50,"Schadensmeldungen"),
    ("tag","document_type","Nachtragspolice",      "exact","Versicherungsschein", 50,"Nachtragspolicen"),
    # Wohnen
    ("tag","document_type","Mietvertrag",           "exact","Mietvertrag",           50,"Mietverträge"),
    ("tag","document_type","Nebenkostenabrechnung", "exact","Nebenkostenabrechnung", 50,"Nebenkostenabrechnungen"),
    ("tag","document_type","Mieterhöhung",          "exact","Mieterhöhung",          50,"Mieterhöhungsschreiben"),
    ("tag","document_type","Kautionsquittung",      "exact","Kautionsquittung",      50,"Kautionsquittungen"),
    ("tag","document_type","Wohnungskündigung",     "exact","Kündigung",             50,"Wohnungskündigungen"),
    ("tag","document_type","Hausgeldabrechnung",    "exact","Hausgeldabrechnung",    50,"WEG-Hausgeldabrechnungen"),
    # Arbeit
    ("tag","document_type","Arbeitsvertrag",     "exact","Arbeitsvertrag", 50,"Arbeitsverträge"),
    ("tag","document_type","Zeugnis",            "exact","Zeugnis",        50,"Arbeitszeugnisse"),
    ("tag","document_type","Kündigungsschreiben","exact","Kündigung",      50,"Kündigungen"),
    ("tag","document_type","Abmahnung",          "exact","Abmahnung",      50,"Abmahnungen"),
    # Behörde / Recht
    ("tag","document_type","Bescheid",               "exact","Bescheid",            50,"Behördenbescheide"),
    ("tag","document_type","Behördenpost",           "exact","Bescheid",            50,"Behördenpost"),
    ("tag","document_type","Bußgeldbescheid",        "exact","Bußgeldbescheid",     50,"Bußgeldbescheide"),
    ("tag","document_type","Gerichtsschreiben",      "exact","Gerichtsschreiben",   50,"Gerichtliche Korrespondenz"),
    ("tag","document_type","Vollstreckungsbescheid", "exact","Vollstreckung",       50,"Vollstreckungsbescheide"),
    # KFZ
    ("tag","document_type","Fahrzeugbrief",  "exact","Fahrzeugdokument", 50,"Fahrzeugbriefe"),
    ("tag","document_type","Fahrzeugschein", "exact","Fahrzeugdokument", 50,"Fahrzeugscheine"),
    ("tag","document_type","HU-Bericht",     "exact","HU-TÜV",          50,"Hauptuntersuchung"),
    ("tag","document_type","Kfz-Steuer",     "exact","KFZ-Steuer",      50,"Kraftfahrzeugsteuer"),
    # Telekommunikation
    ("tag","document_type","Mobilfunkvertrag","exact","Mobilfunk",  50,"Mobilfunkverträge"),
    ("tag","document_type","Internetvertrag", "exact","Internet",   50,"Internetverträge"),
    # Sonstiges
    ("tag","document_type","Vertrag",       "exact","Vertrag",       50,"Allgemeine Verträge"),
    ("tag","document_type","Kündigung",     "exact","Kündigung",     50,"Allgemeine Kündigungen"),
    ("tag","document_type","Bescheinigung", "exact","Bescheinigung", 50,"Amtliche Bescheinigungen"),
    ("tag","document_type","Werbung",       "exact","Werbung",       50,"Werbung"),

    # ── B) Schlüsselwort im OCR-Text → Blatt-Tag  (semantisch, firmenunabhängig)
    ("tag","keyword","mahnung",             "contains","Mahnung",            30,"Mahnung via Volltext"),
    ("tag","keyword","zahlungserinnerung",  "contains","Mahnung",            30,"Zahlungserinnerung"),
    ("tag","keyword","inkasso",             "contains","Mahnung",            30,"Inkasso via Volltext"),
    ("tag","keyword","vollstreckung",       "contains","Vollstreckung",      30,"Vollstreckung via Volltext"),
    ("tag","keyword","zwangsvollstreckung", "contains","Vollstreckung",      30,"Zwangsvollstreckung"),
    ("tag","keyword","insolvenz",           "contains","Insolvenz",          30,"Insolvenz via Volltext"),
    ("tag","keyword","kfz-steuer",          "contains","KFZ-Steuer",         30,"Kfz-Steuer via Volltext"),
    ("tag","keyword","kraftfahrzeugsteuer", "contains","KFZ-Steuer",         30,"Kraftfahrzeugsteuer"),
    ("tag","keyword","hauptuntersuchung",   "contains","HU-TÜV",             30,"TÜV/HU via Volltext"),
    ("tag","keyword","kindergeld",          "contains","Kindergeld",         30,"Kindergeld"),
    ("tag","keyword","elterngeld",          "contains","Elterngeld",         30,"Elterngeld"),
    ("tag","keyword","unterhalt",           "contains","Unterhalt",          30,"Unterhalt"),
    ("tag","keyword","betreuungsgeld",      "contains","Elterngeld",         30,"Betreuungsgeld"),
    ("tag","keyword","rentenbescheid",      "contains","Rentenbescheid",     30,"Rentenbescheid"),
    ("tag","keyword","rentenanpassung",     "contains","Rentenbescheid",     30,"Rentenanpassung"),
    ("tag","keyword","wohngeld",            "contains","Wohngeld",           30,"Wohngeld"),
    ("tag","keyword","sozialhilfe",         "contains","Sozialhilfe",        30,"Sozialhilfe"),
    ("tag","keyword","datenschutz",         "contains","Datenschutz",        30,"Datenschutz-Schreiben"),
    ("tag","keyword","dsgvo",               "contains","Datenschutz",        30,"DSGVO"),
    ("tag","keyword","abmahnung",           "contains","Abmahnung",          30,"Abmahnung erkannt"),
    ("tag","keyword","krankenversicherung", "contains","Krankenversicherung",30,"Krankenversicherung via Volltext"),
    ("tag","keyword","pflegeversicherung",  "contains","Pflegeversicherung", 30,"Pflegeversicherung"),
    ("tag","keyword","steuernummer",        "contains","Steuerbescheid",     30,"Steuernummer im Text"),
    ("tag","keyword","finanzamt",           "contains","Steuerbescheid",     30,"Finanzamt via Volltext"),
]


# ---------------------------------------------------------------------------
# Tag-Hierarchie  (tag → parent_tag | None = Root)
# Identisch mit den INSERT IGNORE-Blöcken in database.sql.
# Format: (tag, parent_tag | None, sort_order)
#
# Struktur (Auszug):
#   Finanzen
#     Rechnung        Kontoauszug
#     Kredit
#     Steuern
#       Steuerbescheid / Steuererklärung / Lohnsteuerbescheinigung
#     Gehalt / Lohnabrechnung
#     Mahnung
#       Vollstreckung / Insolvenz
#     Quittung / Angebot
#   Gesundheit
#     Arztbrief / Befund / Rezept / Überweisung / Therapiebericht
#     Krankenhaus
#       Krankenhausbericht / Entlassungsbrief
#   Versicherung
#     Krankenversicherung
#       Gesetzliche Krankenversicherung / Private Krankenversicherung
#     Pflegeversicherung / KFZ-Versicherung / Haftpflicht
#     Lebensversicherung / Hausratversicherung / Rentenversicherung
#     Versicherungsschein / Schadensmeldung / Schadensregulierung
#   Vertrag
#     Mietvertrag / Arbeitsvertrag / Mobilfunkvertrag
#     Internetvertrag / Kredit / Versicherungsschein
#   Wohnen
#     Mietvertrag / Nebenkostenabrechnung / Mieterhöhung
#     Kautionsquittung / Hausgeldabrechnung
#   Arbeit
#     Arbeitsvertrag / Zeugnis / Abmahnung / Lohnabrechnung
#   Behörde
#     Bescheid / Steuerbescheid / Bußgeldbescheid / Bescheinigung
#     Rentenbescheid
#   Sozialleistungen
#     Kindergeld / Elterngeld / Wohngeld / Sozialhilfe / Unterhalt
#   Recht
#     Gerichtsschreiben / Mahnbescheid / Vollstreckung / Kündigung / Abmahnung
#   KFZ
#     Fahrzeugdokument
#       Fahrzeugbrief / Fahrzeugschein
#     KFZ-Steuer / HU-TÜV / KFZ-Versicherung
#   Telekommunikation
#     Mobilfunk / Internet
#   Familie
#     Kindergeld / Elterngeld / Unterhalt / Betreuung
#   Datenschutz
#     DSGVO
# ---------------------------------------------------------------------------
_BUILTIN_HIERARCHY: list[tuple[str, str | None, int]] = [
    # ── Wurzel-Kategorien (parent = None) ──────────────────────────────────
    ("Finanzen",          None,          0),
    ("Gesundheit",        None,          1),
    ("Versicherung",      None,          2),
    ("Vertrag",           None,          3),
    ("Wohnen",            None,          4),
    ("Arbeit",            None,          5),
    ("Behörde",           None,          6),
    ("Sozialleistungen",  None,          7),
    ("Recht",             None,          8),
    ("KFZ",               None,          9),
    ("Telekommunikation", None,         10),
    ("Familie",           None,         11),
    ("Datenschutz",       None,         12),

    # ── Finanzen ────────────────────────────────────────────────────────────
    ("Rechnung",                 "Finanzen",   0),
    ("Kontoauszug",              "Finanzen",   1),
    ("Kredit",                   "Finanzen",   2),
    ("Steuern",                  "Finanzen",   3),
    ("Gehalt",                   "Finanzen",   4),
    ("Lohnabrechnung",           "Gehalt",     0),
    ("Lohnsteuerbescheinigung",  "Steuern",    0),
    ("Steuerbescheid",           "Steuern",    1),
    ("Steuererklärung",          "Steuern",    2),
    ("Mahnung",                  "Finanzen",   5),
    ("Vollstreckung",            "Recht",      0),
    ("Insolvenz",                "Recht",      1),
    ("Mahnbescheid",             "Recht",      2),
    ("Quittung",                 "Finanzen",   6),
    ("Angebot",                  "Finanzen",   7),

    # ── Gesundheit ──────────────────────────────────────────────────────────
    ("Arztbrief",                "Gesundheit", 0),
    ("Befund",                   "Gesundheit", 1),
    ("Rezept",                   "Gesundheit", 2),
    ("Überweisung",              "Gesundheit", 3),
    ("Therapiebericht",          "Gesundheit", 4),
    ("Krankenhaus",              "Gesundheit", 5),
    ("Krankenhausbericht",       "Krankenhaus", 0),
    ("Entlassungsbrief",         "Krankenhaus", 1),

    # ── Versicherung ────────────────────────────────────────────────────────
    ("Krankenversicherung",           "Versicherung", 0),
    ("Gesetzliche Krankenversicherung","Krankenversicherung", 0),
    ("Private Krankenversicherung",    "Krankenversicherung", 1),
    ("Pflegeversicherung",            "Versicherung", 1),
    ("KFZ-Versicherung",              "Versicherung", 2),
    ("Haftpflicht",                   "Versicherung", 3),
    ("Lebensversicherung",            "Versicherung", 4),
    ("Hausratversicherung",           "Versicherung", 5),
    ("Rentenversicherung",            "Versicherung", 6),
    ("Versicherungsschein",           "Versicherung", 7),
    ("Schadensmeldung",               "Versicherung", 8),
    ("Schadensregulierung",           "Versicherung", 9),

    # ── Vertrag ─────────────────────────────────────────────────────────────
    ("Mietvertrag",     "Vertrag", 0),
    ("Arbeitsvertrag",  "Vertrag", 1),
    ("Mobilfunkvertrag","Vertrag", 2),
    ("Internetvertrag", "Vertrag", 3),

    # ── Wohnen ──────────────────────────────────────────────────────────────
    ("Nebenkostenabrechnung", "Wohnen", 0),
    ("Mieterhöhung",          "Wohnen", 1),
    ("Kautionsquittung",      "Wohnen", 2),
    ("Hausgeldabrechnung",    "Wohnen", 3),

    # ── Arbeit ──────────────────────────────────────────────────────────────
    ("Zeugnis",     "Arbeit", 0),
    ("Abmahnung",   "Arbeit", 1),

    # ── Behörde ─────────────────────────────────────────────────────────────
    ("Bescheid",         "Behörde", 0),
    ("Bußgeldbescheid",  "Behörde", 1),
    ("Bescheinigung",    "Behörde", 2),
    ("Rentenbescheid",   "Behörde", 3),

    # ── Sozialleistungen ────────────────────────────────────────────────────
    ("Kindergeld",  "Sozialleistungen", 0),
    ("Elterngeld",  "Sozialleistungen", 1),
    ("Wohngeld",    "Sozialleistungen", 2),
    ("Sozialhilfe", "Sozialleistungen", 3),
    ("Unterhalt",   "Sozialleistungen", 4),

    # ── Recht ───────────────────────────────────────────────────────────────
    ("Gerichtsschreiben", "Recht", 3),
    ("Kündigung",         "Recht", 4),

    # ── KFZ ─────────────────────────────────────────────────────────────────
    ("Fahrzeugdokument", "KFZ",             0),
    ("Fahrzeugbrief",    "Fahrzeugdokument",0),
    ("Fahrzeugschein",   "Fahrzeugdokument",1),
    ("KFZ-Steuer",       "KFZ",             1),
    ("HU-TÜV",           "KFZ",             2),

    # ── Telekommunikation ───────────────────────────────────────────────────
    ("Mobilfunk", "Telekommunikation", 0),
    ("Internet",  "Telekommunikation", 1),

    # ── Familie ─────────────────────────────────────────────────────────────
    ("Betreuung", "Familie", 0),

    # ── Datenschutz ─────────────────────────────────────────────────────────
    ("DSGVO",      "Datenschutz", 0),

    # ── Cross-Referenzen (Duplikat-Einträge werden von INSERT IGNORE ignoriert)
    # Mietvertrag gehört auch zu Wohnen → extra Regel über assignment_rules,
    # denn in der Hierarchie hat jeder Tag genau einen Eltern-Knoten.
]


# ---------------------------------------------------------------------------
# Tag-Hierarchie-Expansion
# ---------------------------------------------------------------------------

def expand_tags(
    tags: list[str],
    hierarchy: dict[str, str | None],
) -> list[str]:
    """Ergänzt für jeden Tag alle Vorfahren bis zur Wurzel.

    Beispiel:
        tags      = ["Arztbrief"]
        hierarchy = {"Arztbrief": "Gesundheit", "Gesundheit": None, ...}
        →  ["Arztbrief", "Gesundheit"]

    Duplikate werden entfernt; die Reihenfolge bleibt erhalten
    (Blatt-Tags zuerst, Wurzel-Tags zuletzt).
    """
    result: list[str] = []
    seen: set[str] = set()
    depth_limit = 20  # Schutz vor Endlosschleifen; 20 Ebenen sind mehr als ausreichend

    for tag in tags:
        if tag not in seen:
            result.append(tag)
            seen.add(tag)
        # Vorfahren ergänzen
        current = tag
        depth = 0
        while depth < depth_limit:
            parent = hierarchy.get(current)
            if parent is None:
                break
            if parent not in seen:
                result.append(parent)
                seen.add(parent)
            elif depth == depth_limit - 1:
                logger.warning(
                    "expand_tags: Hierarchie-Tiefe von %d erreicht für Tag %r – "
                    "prüfe auf Zyklen in der tag_hierarchy-Tabelle.",
                    depth_limit, tag,
                )
            current = parent
            depth += 1

    return result


_DEFAULT_AI_ANALYZER_SYSTEM = """\
Du bist ein erfahrener Dokumentenarchivarius für deutschsprachige Privat- und \
Geschäftsdokumente. Du erkennst den INHALTLICHEN KONTEXT eines Dokuments, \
nicht nur Schlüsselwörter. Antworte AUSSCHLIESSLICH mit einem validen \
JSON-Objekt, ohne Markdown oder erklärender Text.\
"""

_DEFAULT_AI_ANALYZER_USER_TEMPLATE = """\
Analysiere den folgenden deutschen Dokumententext und extrahiere alle Metadaten.

WICHTIG für das Datum (document_date):
- Suche nach dem Briefdatum / Ausstellungsdatum / Rechnungsdatum.
- Akzeptierte Formate im Quelltext: "27. März 2026", "27.03.2026", "2026-03-27",
  "März 2026", "03/2026" – konvertiere IMMER nach ISO 8601: YYYY-MM-DD.
- Bei unvollständigen Datumsangaben (nur Monat/Jahr): verwende den 1. des Monats.
- Gibt es kein Datum: null.

WICHTIG für topic_tags:
- Bestimme 2–6 semantische Tags, die den INHALT beschreiben (nicht die Firma).
- Beispiele: "Krankenversicherung", "Steuerbescheid", "Mietvertrag", "Mahnung",
  "Arztbrief", "Kündigung", "Rentenversicherung", "KFZ-Steuer", "Kindergeld" …
- Verwende Tags aus der bestehenden Hierarchie wenn passend:
  Finanzen, Gesundheit, Versicherung, Vertrag, Wohnen, Arbeit, Behörde,
  Sozialleistungen, Recht, KFZ, Telekommunikation, Familie, Datenschutz
  (+ Unter-Tags der jeweiligen Kategorie).
- Keine Firmennamen als Tags!

Dokumententext:
---
{text}
---

Antworte mit GENAU diesem JSON:
{{
  "sender": "Absender (Name/Firma oder null)",
  "recipient_names": ["Name 1", "Name 2"],
  "document_date": "YYYY-MM-DD oder null",
  "document_type": "Exakter Typ: Rechnung | Arztbrief | Mietvertrag | Steuerbescheid | …",
  "topic_tags": ["Tag1", "Tag2"],
  "organizations": ["Firma oder Behörde 1", "Firma 2"],
  "reference_number": "Aktenzeichen/Rechnungsnr. oder null",
  "confidence": 0.9
}}\
"""

# pre_classifier – Phase 1: Einzeldokument
_DEFAULT_PRECLASSIFIER_PHASE1_SYSTEM = (
    "Du bist ein erfahrener Dokumentenarchivarius für deutschsprachige "
    "Privat- und Geschäftsdokumente. Du hast tiefes Wissen über deutsche "
    "Behörden, medizinische, finanzielle und rechtliche Dokumente. "
    "Erkenne den ECHTEN Kontext – nicht nur Schlüsselwörter. "
    "Antworte IMMER ausschließlich mit einem validen JSON-Objekt."
)

_DEFAULT_PRECLASSIFIER_PHASE1_USER_TEMPLATE = """
Analysiere den folgenden Dokumententext und erstelle eine strukturierte Klassifizierung.

Dokumententext (OCR):
---
{text}
---

Antworte mit GENAU diesem JSON (alle Felder angeben):
{{
  "document_type": "Exakter Dokumenttyp auf Deutsch",
  "topic_tags": ["max. 6 semantische Themen-Tags auf Deutsch, kurz"],
  "persons": [
    {{"name": "Vollständiger Name", "role": "Funktion: Absender/Empfänger/Arzt/Anwalt/etc."}}
  ],
  "organizations": ["Organisation 1", "Organisation 2"],
  "sender": "Hauptabsender (Name oder Firma)",
  "recipient": "Empfänger-Name(n) aus dem Dokument",
  "summary": "Ein präziser Satz: Worum geht es?",
  "action_required": false,
  "action_description": "Nur wenn action_required true: Was ist zu tun?",
  "confidence": 0.85
}}
""".strip()

# pre_classifier – Phase 2: Kontext-Synthese
_DEFAULT_PRECLASSIFIER_PHASE2_SYSTEM = (
    "Du bist ein erfahrener Dokumentenarchivarius. Du analysierst eine GESAMTE "
    "Dokumentensammlung auf übergreifende Muster, Zusammenhänge und wiederkehrende "
    "Themen. Dein Ziel: eine optimale Tag-Taxonomie vorschlagen, die Verbindungen "
    "zwischen Dokumenten sichtbar macht. "
    "Antworte IMMER ausschließlich mit einem validen JSON-Objekt."
)

_DEFAULT_PRECLASSIFIER_PHASE2_USER_TEMPLATE = """
Hier sind die Analysen von {n} gescannten Dokumenten. Analysiere sie als GESAMTHEIT.

Dokument-Zusammenfassungen:
---
{summaries}
---

Antworte mit GENAU diesem JSON:
{{
  "overall_assessment": "Gesamteinschätzung der Sammlung in 2-3 Sätzen",
  "tag_taxonomy": [
    {{
      "tag": "Tag-Name auf Deutsch",
      "category": "Oberkategorie (Gesundheit/Finanzen/Behörde/Wohnen/Arbeit/Recht/Sonstiges)",
      "rationale": "Warum dieser Tag? Welche Dokumente/Muster begründen ihn?",
      "affected_files": ["datei1.pdf"],
      "priority": "hoch"
    }}
  ],
  "connections": [
    {{
      "pattern": "Beschreibung des erkannten Musters oder Zusammenhangs",
      "files": ["datei1.pdf", "datei2.pdf"],
      "suggested_tag": "Empfohlener Tag für diesen Zusammenhang"
    }}
  ],
  "recommended_correspondents": [
    {{
      "name": "Name der Person oder Organisation",
      "type": "Person oder Organisation",
      "rationale": "Welche Rolle spielt diese Person/Org in der Sammlung?",
      "document_count": 3
    }}
  ]
}}
""".strip()

# Alle Standard-Prompts als Nachschlagetabelle
_DEFAULT_PROMPTS: dict[tuple[str, str, str], str] = {
    ("ai_analyzer",    "default", "system"):        _DEFAULT_AI_ANALYZER_SYSTEM,
    ("ai_analyzer",    "default", "user_template"): _DEFAULT_AI_ANALYZER_USER_TEMPLATE,
    ("pre_classifier", "phase1",  "system"):        _DEFAULT_PRECLASSIFIER_PHASE1_SYSTEM,
    ("pre_classifier", "phase1",  "user_template"): _DEFAULT_PRECLASSIFIER_PHASE1_USER_TEMPLATE,
    ("pre_classifier", "phase2",  "system"):        _DEFAULT_PRECLASSIFIER_PHASE2_SYSTEM,
    ("pre_classifier", "phase2",  "user_template"): _DEFAULT_PRECLASSIFIER_PHASE2_USER_TEMPLATE,
}


# ---------------------------------------------------------------------------
# Datenbank-Initialisierung: Standard-Einträge sicherstellen
# ---------------------------------------------------------------------------

def ensure_defaults(
    conn: mysql.connector.MySQLConnection,
    persons_me: list[str] | None = None,
    persons_partner: list[str] | None = None,
    tag_mapping: dict[str, str] | None = None,
) -> None:
    """Stellt sicher, dass Standard-Prompts und Basis-Regeln in der DB vorhanden sind.

    Verwendet INSERT IGNORE – bestehende, angepasste Einträge werden NICHT
    überschrieben. Kann bei jedem Service-Start aufgerufen werden.

    Eingebaute Regeln (_BUILTIN_RULES) werden immer per INSERT IGNORE
    eingetragen – sie können einzeln per is_active=0 deaktiviert, aber
    nicht aus Versehen überschrieben werden.

    Args:
        conn:            Aktive DB-Verbindung.
        persons_me:      Namen aus config.yaml persons.me  → recipient-Regel "me".
        persons_partner: Namen aus config.yaml persons.partner → "partner".
        tag_mapping:     tag_mapping aus config.yaml → tag-Regeln (nur als Fallback
                         wenn noch gar keine tag-Regeln vorhanden sind).
    """
    cursor = conn.cursor()

    # ── 1. Standard-Prompts ─────────────────────────────────────────────────
    for (service, name, ptype), content in _DEFAULT_PROMPTS.items():
        cursor.execute(
            """
            INSERT IGNORE INTO llm_prompts (service, name, prompt_type, content, notes)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (service, name, ptype, content,
             "Standard-Eintrag – kann in der DB angepasst werden."),
        )

    # ── 2. Eingebaute Zuweisungsregeln (immer INSERT IGNORE) ────────────────
    if _BUILTIN_RULES:
        cursor.executemany(
            """
            INSERT IGNORE INTO assignment_rules
              (rule_type, match_field, match_value, match_mode,
               assign_value, priority, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            _BUILTIN_RULES,
        )
        logger.debug("Eingebaute Regeln per INSERT IGNORE sichergestellt (%d Einträge).",
                     len(_BUILTIN_RULES))

    # ── 3. Empfänger-Regeln aus config.yaml seeden (nur wenn keine vorhanden) ─
    cursor.execute(
        "SELECT COUNT(*) FROM assignment_rules WHERE rule_type = 'recipient'"
    )
    (recipient_rule_count,) = cursor.fetchone()

    if not recipient_rule_count:
        person_rules: list[tuple] = []
        for name in (persons_me or []):
            person_rules.append((
                "recipient", "recipient_name", name, "exact", "me", 100,
                f"Aus config.yaml persons.me – {name}",
            ))
        for name in (persons_partner or []):
            person_rules.append((
                "recipient", "recipient_name", name, "exact", "partner", 90,
                f"Aus config.yaml persons.partner – {name}",
            ))
        if person_rules:
            cursor.executemany(
                """
                INSERT IGNORE INTO assignment_rules
                  (rule_type, match_field, match_value, match_mode,
                   assign_value, priority, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                person_rules,
            )
            logger.info(
                "%d Empfänger-Regeln aus config.yaml in DB eingetragen.",
                len(person_rules),
            )

    # ── 4. Tag-Mapping aus config.yaml als Ergänzung (nur wenn noch leer) ───
    # Die eingebauten Regeln decken die häufigsten Fälle ab. Das tag_mapping
    # aus config.yaml wird nur als Rückfall eingefügt, wenn der Nutzer
    # zusätzliche Typen definiert hat die noch nicht in den Builtin-Regeln sind.
    if tag_mapping:
        for doc_type, paperless_tag in tag_mapping.items():
            cursor.execute(
                """
                INSERT IGNORE INTO assignment_rules
                  (rule_type, match_field, match_value, match_mode,
                   assign_value, priority, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "tag", "document_type", doc_type, "exact", paperless_tag, 50,
                    f"Aus config.yaml tag_mapping – {doc_type} → {paperless_tag}",
                ),
            )

    conn.commit()
    cursor.close()
    logger.info("DB-Standards sichergestellt (Prompts + %d eingebaute Regeln).",
                len(_BUILTIN_RULES))

    # ── 5. Tag-Hierarchie seeden ─────────────────────────────────────────────
    _seed_tag_hierarchy(conn)


def _seed_tag_hierarchy(conn: mysql.connector.MySQLConnection) -> None:
    """Trägt die eingebaute Tag-Hierarchie per INSERT IGNORE in die DB ein."""
    cursor = conn.cursor()
    try:
        cursor.executemany(
            """
            INSERT IGNORE INTO tag_hierarchy (tag, parent_tag, sort_order)
            VALUES (%s, %s, %s)
            """,
            _BUILTIN_HIERARCHY,
        )
        conn.commit()
        logger.debug(
            "Tag-Hierarchie per INSERT IGNORE sichergestellt (%d Einträge).",
            len(_BUILTIN_HIERARCHY),
        )
    except Exception as exc:
        logger.warning("tag_hierarchy konnte nicht geseedet werden: %s", exc)
        conn.rollback()
    finally:
        cursor.close()
