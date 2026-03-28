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

        cursor.close()

        with self._lock:
            self._rules = rules
            self._prompts = prompts
            self._loaded_at = time.time()

        logger.debug(
            "DB-Cache aktualisiert: %d Regeln, %d Prompts",
            len(rules),
            len(prompts),
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
# Eingebaute Standard-Regeln (identisch mit den INSERT IGNORE-Blöcken in
# database.sql – hier als Python-Konstante für ensure_defaults())
# Format: (rule_type, match_field, match_value, match_mode, assign_value,
#           priority, description)
# ---------------------------------------------------------------------------
_BUILTIN_RULES: list[tuple[str, str, str, str, str, int, str]] = [
    # ── A) Dokumenttyp → Tag ────────────────────────────────────────────────
    # Finanzen
    ("tag","document_type","Rechnung",              "exact","Rechnung",              50,"Eingehende Rechnungen"),
    ("tag","document_type","Kontoauszug",            "exact","Kontoauszug",            50,"Bankkontoauszüge"),
    ("tag","document_type","Lohnabrechnung",         "exact","Gehalt",                 50,"Gehalts-/Lohnabrechnungen"),
    ("tag","document_type","Gehaltsabrechnung",      "exact","Gehalt",                 50,"Gehalts-/Lohnabrechnungen"),
    ("tag","document_type","Lohnsteuerbescheinigung","exact","Steuern",                50,"Lohnsteuerbescheinigungen"),
    ("tag","document_type","Kreditvertrag",          "exact","Kredit",                 50,"Kredite und Darlehen"),
    ("tag","document_type","Darlehensvertrag",       "exact","Kredit",                 50,"Darlehensverträge"),
    ("tag","document_type","Mahnung",                "exact","Mahnung",                50,"Zahlungsmahnungen"),
    ("tag","document_type","Mahnbescheid",           "exact","Mahnung",                50,"Gerichtlicher Mahnbescheid"),
    ("tag","document_type","Quittung",               "exact","Quittung",               50,"Zahlungsquittungen"),
    ("tag","document_type","Angebot",                "exact","Angebot",                50,"Kostenvoranschläge und Angebote"),
    # Steuern
    ("tag","document_type","Steuerbescheid",             "exact","Steuern", 50,"Steuerbescheide"),
    ("tag","document_type","Einkommensteuerbescheid",    "exact","Steuern", 50,"Einkommensteuerbescheide"),
    ("tag","document_type","Einkommensteuererklärung",   "exact","Steuern", 50,"Steuererklärungen"),
    ("tag","document_type","Kirchensteuerbescheid",      "exact","Steuern", 50,"Kirchensteuerbescheide"),
    ("tag","document_type","Umsatzsteuerbescheid",       "exact","Steuern", 50,"Umsatzsteuerbescheide"),
    # Gesundheit
    ("tag","document_type","Arztbrief",          "exact","Gesundheit", 50,"Arztbriefe"),
    ("tag","document_type","Befundbericht",      "exact","Gesundheit", 50,"Medizinische Befundberichte"),
    ("tag","document_type","Krankenhausbericht", "exact","Gesundheit", 50,"Krankenhausberichte"),
    ("tag","document_type","Entlassungsbrief",   "exact","Gesundheit", 50,"Entlassungsbriefe aus Krankenhaus"),
    ("tag","document_type","Rezept",             "exact","Gesundheit", 50,"Arztrezepte"),
    ("tag","document_type","Überweisungsschein", "exact","Gesundheit", 50,"Facharzt-Überweisungen"),
    ("tag","document_type","Krankenhausrechnung","exact","Gesundheit", 50,"Krankenhausrechnungen"),
    ("tag","document_type","Therapiebericht",    "exact","Gesundheit", 50,"Therapieberichte"),
    # Versicherungen
    ("tag","document_type","Versicherungsschein",  "exact","Versicherung", 50,"Versicherungsscheine"),
    ("tag","document_type","Versicherungspolice",  "exact","Versicherung", 50,"Versicherungspolicen"),
    ("tag","document_type","Schadensregulierung",  "exact","Versicherung", 50,"Schadensregulierungen"),
    ("tag","document_type","Schadensmeldung",      "exact","Versicherung", 50,"Schadensmeldungen"),
    ("tag","document_type","Nachtragspolice",      "exact","Versicherung", 50,"Nachtragspolicen"),
    # Wohnen
    ("tag","document_type","Mietvertrag",           "exact","Wohnen", 50,"Mietverträge"),
    ("tag","document_type","Nebenkostenabrechnung", "exact","Wohnen", 50,"Nebenkostenabrechnungen"),
    ("tag","document_type","Mieterhöhung",          "exact","Wohnen", 50,"Mieterhöhungsschreiben"),
    ("tag","document_type","Kautionsquittung",      "exact","Wohnen", 50,"Kautionsquittungen"),
    ("tag","document_type","Wohnungskündigung",     "exact","Wohnen", 50,"Wohnungskündigungen"),
    ("tag","document_type","Hausgeldabrechnung",    "exact","Wohnen", 50,"WEG-Hausgeldabrechnungen"),
    # Arbeit / Beruf
    ("tag","document_type","Arbeitsvertrag",     "exact","Arbeit",    50,"Arbeitsverträge"),
    ("tag","document_type","Zeugnis",            "exact","Arbeit",    50,"Arbeitszeugnisse"),
    ("tag","document_type","Kündigungsschreiben","exact","Kündigung", 50,"Arbeitgeberkündigungen"),
    ("tag","document_type","Abmahnung",          "exact","Arbeit",    50,"Arbeitsrechtliche Abmahnungen"),
    # Behörden / Recht
    ("tag","document_type","Bescheid",               "exact","Behörde",      50,"Behördenbescheide"),
    ("tag","document_type","Behördenpost",           "exact","Behörde",      50,"Post von Behörden"),
    ("tag","document_type","Bußgeldbescheid",        "exact","Bußgeld",      50,"Bußgeldbescheide"),
    ("tag","document_type","Gerichtsschreiben",      "exact","Recht",        50,"Gerichtliche Korrespondenz"),
    ("tag","document_type","Vollstreckungsbescheid", "exact","Vollstreckung",50,"Vollstreckungsbescheide"),
    # KFZ
    ("tag","document_type","Fahrzeugbrief",  "exact","KFZ", 50,"Fahrzeugbriefe"),
    ("tag","document_type","Fahrzeugschein", "exact","KFZ", 50,"Fahrzeugscheine"),
    ("tag","document_type","HU-Bericht",     "exact","KFZ", 50,"Hauptuntersuchung (TÜV)"),
    ("tag","document_type","Kfz-Steuer",     "exact","KFZ", 50,"Kraftfahrzeugsteuerbescheide"),
    # Telekommunikation
    ("tag","document_type","Mobilfunkvertrag","exact","Telekommunikation", 50,"Mobilfunkverträge"),
    ("tag","document_type","Internetvertrag", "exact","Telekommunikation", 50,"Internetverträge"),
    # Sonstiges
    ("tag","document_type","Vertrag",       "exact","Vertrag",       50,"Allgemeine Verträge"),
    ("tag","document_type","Kündigung",     "exact","Kündigung",     50,"Allgemeine Kündigungen"),
    ("tag","document_type","Bescheinigung", "exact","Bescheinigung", 50,"Amtliche Bescheinigungen"),
    ("tag","document_type","Werbung",       "exact","Werbung",       50,"Werbung und Marketing"),

    # ── B) Organisation → Tag ────────────────────────────────────────────────
    # Gesetzliche Krankenversicherungen
    ("tag","organization","Techniker Krankenkasse",    "contains","Krankenversicherung", 40,"TK"),
    ("tag","organization","Barmer",                    "contains","Krankenversicherung", 40,"Barmer"),
    ("tag","organization","AOK",                       "contains","Krankenversicherung", 40,"AOK"),
    ("tag","organization","DAK",                       "contains","Krankenversicherung", 40,"DAK"),
    ("tag","organization","IKK",                       "contains","Krankenversicherung", 40,"IKK"),
    ("tag","organization","Knappschaft",               "contains","Krankenversicherung", 40,"Knappschaft"),
    ("tag","organization","BKK",                       "contains","Krankenversicherung", 40,"BKK"),
    ("tag","organization","Hanseatische Krankenkasse", "contains","Krankenversicherung", 40,"HEK"),
    ("tag","organization","hkk",                       "contains","Krankenversicherung", 40,"hkk"),
    # Rentenversicherung
    ("tag","organization","Deutsche Rentenversicherung","contains","Rente", 40,"DRV"),
    ("tag","organization","Rentenversicherung",         "contains","Rente", 40,"Rentenversicherung"),
    # Behörden
    ("tag","organization","Bundesagentur für Arbeit","contains","Arbeitsagentur",  40,"BA für Arbeit"),
    ("tag","organization","Jobcenter",               "contains","Jobcenter",       40,"Jobcenter"),
    ("tag","organization","Finanzamt",               "contains","Steuern",         40,"Finanzamt"),
    ("tag","organization","Bundeszentralamt",        "contains","Steuern",         40,"Bundeszentralamt"),
    ("tag","organization","Amtsgericht",             "contains","Recht",           40,"Amtsgericht"),
    ("tag","organization","Landgericht",             "contains","Recht",           40,"Landgericht"),
    ("tag","organization","Oberlandesgericht",       "contains","Recht",           40,"OLG"),
    ("tag","organization","Rechtsanwalt",            "contains","Recht",           40,"Anwaltskanzlei"),
    ("tag","organization","Inkasso",                 "contains","Mahnung",         40,"Inkasso-Büro"),
    ("tag","organization","Ordnungsamt",             "contains","Behörde",         40,"Ordnungsamt"),
    ("tag","organization","Bürgeramt",               "contains","Behörde",         40,"Bürgeramt"),
    ("tag","organization","Einwohnermeldeamt",       "contains","Behörde",         40,"Einwohnermeldeamt"),
    ("tag","organization","Jugendamt",               "contains","Behörde",         40,"Jugendamt"),
    ("tag","organization","Sozialamt",               "contains","Sozialleistungen",40,"Sozialamt"),
    ("tag","organization","Versorgungsamt",          "contains","Behörde",         40,"Versorgungsamt"),
    ("tag","organization","Zoll",                    "contains","Behörde",         40,"Zoll"),
    # Energie / Versorger
    ("tag","organization","Stadtwerke", "contains","Energie", 40,"Stadtwerke"),
    ("tag","organization","E.ON",       "contains","Energie", 40,"E.ON"),
    ("tag","organization","RWE",        "contains","Energie", 40,"RWE"),
    ("tag","organization","EnBW",       "contains","Energie", 40,"EnBW"),
    ("tag","organization","Vattenfall", "contains","Energie", 40,"Vattenfall"),
    ("tag","organization","Innogy",     "contains","Energie", 40,"Innogy"),
    # Telekommunikation
    ("tag","organization","Telekom",   "contains","Telekommunikation", 40,"Telekom"),
    ("tag","organization","Vodafone",  "contains","Telekommunikation", 40,"Vodafone"),
    ("tag","organization","O2",        "contains","Telekommunikation", 40,"O2"),
    ("tag","organization","1&1",       "contains","Telekommunikation", 40,"1&1"),
    ("tag","organization","Freenet",   "contains","Telekommunikation", 40,"Freenet"),
    ("tag","organization","Unitymedia","contains","Telekommunikation", 40,"Unitymedia"),
    ("tag","organization","Congstar",  "contains","Telekommunikation", 40,"Congstar"),
    # Banken
    ("tag","organization","Sparkasse",     "contains","Bank", 40,"Sparkasse"),
    ("tag","organization","Volksbank",     "contains","Bank", 40,"Volksbank"),
    ("tag","organization","Raiffeisenbank","contains","Bank", 40,"Raiffeisenbank"),
    ("tag","organization","Commerzbank",   "contains","Bank", 40,"Commerzbank"),
    ("tag","organization","Deutsche Bank", "contains","Bank", 40,"Deutsche Bank"),
    ("tag","organization","DKB",           "contains","Bank", 40,"DKB"),
    ("tag","organization","ING",           "contains","Bank", 40,"ING"),
    ("tag","organization","Postbank",      "contains","Bank", 40,"Postbank"),
    ("tag","organization","Comdirect",     "contains","Bank", 40,"Comdirect"),
    ("tag","organization","N26",           "contains","Bank", 40,"N26"),
    ("tag","organization","Targobank",     "contains","Bank", 40,"Targobank"),
    # Versicherungen
    ("tag","organization","Allianz",     "contains","Versicherung", 40,"Allianz"),
    ("tag","organization","HUK",         "contains","Versicherung", 40,"HUK"),
    ("tag","organization","AXA",         "contains","Versicherung", 40,"AXA"),
    ("tag","organization","Generali",    "contains","Versicherung", 40,"Generali"),
    ("tag","organization","Zurich",      "contains","Versicherung", 40,"Zurich"),
    ("tag","organization","R+V",         "contains","Versicherung", 40,"R+V"),
    ("tag","organization","DEVK",        "contains","Versicherung", 40,"DEVK"),
    ("tag","organization","Ergo",        "contains","Versicherung", 40,"Ergo"),
    ("tag","organization","Debeka",      "contains","Versicherung", 40,"Debeka"),
    ("tag","organization","Signal Iduna","contains","Versicherung", 40,"Signal Iduna"),
    ("tag","organization","VHV",         "contains","Versicherung", 40,"VHV"),
    ("tag","organization","Gothaer",     "contains","Versicherung", 40,"Gothaer"),
    # Rundfunkbeitrag
    ("tag","organization","Beitragsservice","contains","Rundfunkbeitrag", 40,"GEZ/Beitragsservice"),
    ("tag","organization","GEZ",            "contains","Rundfunkbeitrag", 40,"GEZ"),

    # ── C) Schlüsselwort im OCR-Text → Tag ──────────────────────────────────
    ("tag","keyword","mahnung",             "contains","Mahnung",            30,"Mahnung via Volltext"),
    ("tag","keyword","zahlungserinnerung",  "contains","Mahnung",            30,"Zahlungserinnerung"),
    ("tag","keyword","inkasso",             "contains","Mahnung",            30,"Inkasso via Volltext"),
    ("tag","keyword","vollstreckung",       "contains","Vollstreckung",      30,"Vollstreckung via Volltext"),
    ("tag","keyword","zwangsvollstreckung", "contains","Vollstreckung",      30,"Zwangsvollstreckung"),
    ("tag","keyword","insolvenz",           "contains","Insolvenz",          30,"Insolvenz via Volltext"),
    ("tag","keyword","kfz-steuer",          "contains","KFZ",                30,"Kfz-Steuer → KFZ"),
    ("tag","keyword","kraftfahrzeugsteuer", "contains","KFZ",                30,"Kraftfahrzeugsteuer → KFZ"),
    ("tag","keyword","fahrzeugschein",      "contains","KFZ",                30,"Fahrzeugschein → KFZ"),
    ("tag","keyword","hauptuntersuchung",   "contains","KFZ",                30,"TÜV/HU → KFZ"),
    ("tag","keyword","kindergeld",          "contains","Familie",            30,"Kindergeld → Familie"),
    ("tag","keyword","elterngeld",          "contains","Familie",            30,"Elterngeld → Familie"),
    ("tag","keyword","unterhalt",           "contains","Familie",            30,"Unterhalt → Familie"),
    ("tag","keyword","betreuungsgeld",      "contains","Familie",            30,"Betreuungsgeld → Familie"),
    ("tag","keyword","rentenbescheid",      "contains","Rente",              30,"Rentenbescheid → Rente"),
    ("tag","keyword","rentenanpassung",     "contains","Rente",              30,"Rentenanpassung → Rente"),
    ("tag","keyword","wohngeld",            "contains","Sozialleistungen",   30,"Wohngeld → Sozialleistungen"),
    ("tag","keyword","sozialhilfe",         "contains","Sozialleistungen",   30,"Sozialhilfe"),
    ("tag","keyword","datenschutz",         "contains","Datenschutz",        30,"Datenschutz-Schreiben"),
    ("tag","keyword","dsgvo",               "contains","Datenschutz",        30,"DSGVO-Schreiben"),
    ("tag","keyword","abmahnung",           "contains","Abmahnung",          30,"Abmahnung erkannt"),
    ("tag","keyword","beitragsrechnung",    "contains","Versicherung",       30,"Versicherungsbeitragsrechnung"),
    ("tag","keyword","krankenversicherung", "contains","Krankenversicherung",30,"Krankenversicherung via Volltext"),
    ("tag","keyword","pflegeversicherung",  "contains","Pflegeversicherung", 30,"Pflegeversicherung"),
    ("tag","keyword","steuernummer",        "contains","Steuern",            30,"Steuernummer → Steuer-Dokument"),
    ("tag","keyword","steuer-id",           "contains","Steuern",            30,"Steuer-ID → Steuer-Dokument"),
    ("tag","keyword","finanzamt",           "contains","Steuern",            30,"Finanzamt via Volltext"),

    # ── D) Korrespondent: Absender-Normalisierung ────────────────────────────
    ("correspondent","sender","Techniker Krankenkasse",     "contains","Techniker Krankenkasse",     20,"TK normieren"),
    ("correspondent","sender","Barmer",                     "contains","Barmer",                     20,"Barmer normieren"),
    ("correspondent","sender","AOK",                        "contains","AOK",                        20,"AOK normieren"),
    ("correspondent","sender","DAK",                        "contains","DAK-Gesundheit",              20,"DAK normieren"),
    ("correspondent","sender","IKK",                        "contains","IKK",                        20,"IKK normieren"),
    ("correspondent","sender","Deutsche Rentenversicherung","contains","Deutsche Rentenversicherung", 20,"DRV normieren"),
    ("correspondent","sender","Bundesagentur für Arbeit",   "contains","Bundesagentur für Arbeit",   20,"BA normieren"),
    ("correspondent","sender","Jobcenter",                  "contains","Jobcenter",                  20,"Jobcenter normieren"),
    ("correspondent","sender","Finanzamt",                  "contains","Finanzamt",                  20,"Finanzamt normieren"),
    ("correspondent","sender","Amtsgericht",                "contains","Amtsgericht",                20,"Amtsgericht normieren"),
    ("correspondent","sender","Landgericht",                "contains","Landgericht",                20,"Landgericht normieren"),
    ("correspondent","sender","Allianz",                    "contains","Allianz",                    20,"Allianz normieren"),
    ("correspondent","sender","HUK",                        "contains","HUK-COBURG",                 20,"HUK normieren"),
    ("correspondent","sender","AXA",                        "contains","AXA",                        20,"AXA normieren"),
    ("correspondent","sender","DEVK",                       "contains","DEVK",                       20,"DEVK normieren"),
    ("correspondent","sender","Ergo",                       "contains","Ergo",                       20,"Ergo normieren"),
    ("correspondent","sender","Debeka",                     "contains","Debeka",                     20,"Debeka normieren"),
    ("correspondent","sender","Signal Iduna",               "contains","Signal Iduna",                20,"Signal Iduna normieren"),
    ("correspondent","sender","Stadtwerke",                 "contains","Stadtwerke",                 20,"Stadtwerke normieren"),
    ("correspondent","sender","Telekom",                    "contains","Deutsche Telekom",            20,"Telekom normieren"),
    ("correspondent","sender","Vodafone",                   "contains","Vodafone",                   20,"Vodafone normieren"),
    ("correspondent","sender","1&1",                        "contains","1&1",                        20,"1&1 normieren"),
    ("correspondent","sender","Sparkasse",                  "contains","Sparkasse",                  20,"Sparkasse normieren"),
    ("correspondent","sender","Volksbank",                  "contains","Volksbank",                  20,"Volksbank normieren"),
    ("correspondent","sender","Commerzbank",                "contains","Commerzbank",                20,"Commerzbank normieren"),
    ("correspondent","sender","Deutsche Bank",              "contains","Deutsche Bank",              20,"Deutsche Bank normieren"),
    ("correspondent","sender","DKB",                        "contains","DKB Deutsche Kreditbank",    20,"DKB normieren"),
    ("correspondent","sender","ING",                        "contains","ING",                        20,"ING normieren"),
    ("correspondent","sender","Postbank",                   "contains","Postbank",                   20,"Postbank normieren"),
    ("correspondent","sender","Beitragsservice",            "contains","ARD ZDF Beitragsservice",    20,"GEZ normieren"),
]

# ai_analyzer – Metadaten-Extraktion
_DEFAULT_AI_ANALYZER_SYSTEM = (
    "Du bist ein erfahrener Dokumentenarchivarius für deutschsprachige "
    "Privat- und Geschäftsdokumente. Extrahiere Metadaten präzise und "
    "antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt."
)

_DEFAULT_AI_ANALYZER_USER_TEMPLATE = """
Analysiere den folgenden deutschen Dokumententext und extrahiere die Metadaten.
Antworte AUSSCHLIESSLICH mit einem gültigen JSON-Objekt mit diesen Feldern:
- sender: Absender (Name/Firma oder null)
- document_date: Datum auf dem Dokument (Format YYYY-MM-DD oder null)
- document_type: Typ (Rechnung, Kontoauszug, Versicherung, Behördenpost, Arztbrief, Vertrag, Werbung, Sonstiges)
- reference_number: Aktenzeichen/Rechnungsnummer oder null
- confidence: Deine Gesamtkonfidenz (0.0–1.0)
- recipient_names: Liste der im Dokument genannten Empfängernamen (leer wenn keine gefunden)
- organizations: Liste der genannten Firmen/Behörden (leer wenn keine)

Dokumententext:
{text}

JSON:
""".strip()

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
