#!/usr/bin/env python3
"""
pre_classifier.py – KI-gestützte Voranalyse des Scan-Ordners.

Analysiert den Inhalt des Scan-Ordners in ZWEI PHASEN, bevor die eigentliche
Pipeline startet:

  Phase 1 – Einzeldokument-Analyse:
    Jedes Dokument wird individuell per OCR + LLM analysiert.
    Ergebnis: Typ, Tags, Personen, Organisationen, Zusammenfassung.

  Phase 2 – Kontext-Synthese:
    ALLE Einzelanalysen werden zusammen an das LLM übergeben.
    Das LLM erkennt übergreifende Muster, Zusammenhänge und "Fälle"
    (z.B. "5 TK-Schreiben + 3 Arztbriefe → laufender Krankheitsfall").
    Ergebnis: optimale Tag-Taxonomie mit Begründungen und Zusammenhängen.

Ausgabe: NUR LISTEN – nichts wird automatisch in Paperless angelegt.
Der Nutzer entscheidet selbst, welche Tags/Kontakte er übernimmt.

Verwendung:
    python pre_classifier.py [--scan-dir /data/scans] [--output /data/pre_analysis]
    docker compose --profile preclass run --rm pre_classifier
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rich – formatierte Konsolenausgabe (optionale Abhängigkeit)
# ---------------------------------------------------------------------------
try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore


# ---------------------------------------------------------------------------
# LLM-Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PHASE1 = """\
Du bist ein erfahrener Dokumentenarchivarius für deutschsprachige Privat- und \
Geschäftsdokumente. Du hast tiefes Wissen über:
- Deutsche Behörden, Formulare und amtliche Dokumente
- Medizinische Dokumente (Arztbriefe, Befunde, Krankenkassen-Schreiben)
- Finanzielle Dokumente (Steuer, Versicherung, Banken)
- Wohn- und Mietangelegenheiten
- Rechtliche Schreiben und Verträge

Deine Stärke: Du erkennst den ECHTEN Kontext eines Dokuments, nicht nur \
Schlüsselwörter. Antworte IMMER ausschließlich mit einem validen JSON-Objekt, \
ohne Markdown-Blöcke oder erklärenden Text."""

_USER_PHASE1 = """\
Analysiere den folgenden Dokumententext und erstelle eine strukturierte \
Klassifizierung.

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
}}"""

_SYSTEM_PHASE2 = """\
Du bist ein erfahrener Dokumentenarchivarius. Du analysierst eine GESAMTE \
Dokumentensammlung auf übergreifende Muster, Zusammenhänge und wiederkehrende \
Themen. Dein Ziel: eine optimale Tag-Taxonomie für ein Dokumenten-Archiv \
vorschlagen, die Verbindungen zwischen Dokumenten sichtbar macht.
Antworte IMMER ausschließlich mit einem validen JSON-Objekt."""

_USER_PHASE2 = """\
Hier sind die Analysen von {n} gescannten Dokumenten. Analysiere sie als \
GESAMTHEIT und erkenne übergreifende Muster.

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
      "pattern": "Kurze Beschreibung des erkannten Musters oder Zusammenhangs",
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
}}"""


# ---------------------------------------------------------------------------
# Tag-Farbzuordnung (nach Kategorie-Schlüsselwörtern)
# ---------------------------------------------------------------------------

_COLOR_RULES: list[tuple[frozenset[str], str]] = [
    (frozenset({"finanzen", "rechnung", "zahlung", "mahnung", "steuer", "gehalt",
                "lohn", "kontoauszug", "bank", "kredit", "darlehen"}), "#28a745"),
    (frozenset({"gesundheit", "arzt", "arztbrief", "krankenhaus", "befund",
                "rezept", "medizin", "krankenkasse", "krankenversicherung",
                "therapie", "operation"}), "#17a2b8"),
    (frozenset({"versicherung", "police", "schaden", "haftpflicht",
                "lebensversicherung", "kfz-versicherung"}), "#6f42c1"),
    (frozenset({"behörde", "amt", "bescheid", "bußgeld", "gericht",
                "mahnbescheid", "ordnungsamt", "finanzamt", "bundesagentur"}), "#fd7e14"),
    (frozenset({"wohnen", "miete", "mietvertrag", "nebenkosten",
                "vermieter", "mieter", "wohnung"}), "#e83e8c"),
    (frozenset({"vertrag", "arbeitsvertrag", "kündigung", "zeugnis",
                "angebot", "recht", "anwalt"}), "#dc3545"),
    (frozenset({"kfz", "auto", "fahrzeug", "führerschein",
                "zulassung", "kraftfahrzeug"}), "#495057"),
    (frozenset({"rente", "pension", "altersvorsorge",
                "rentenversicherung"}), "#6c757d"),
]
_DEFAULT_COLOR = "#adb5bd"


def _tag_color(tag: str) -> str:
    t = tag.lower()
    for keywords, color in _COLOR_RULES:
        if any(kw in t for kw in keywords):
            return color
    return _DEFAULT_COLOR


# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------

@dataclass
class DocumentPreAnalysis:
    """Ergebnis der Phase-1-Analyse eines einzelnen Dokuments."""
    filename: str
    document_type: str
    topic_tags: list[str]
    persons: list[dict[str, str]]
    organizations: list[str]
    sender: str | None
    recipient: str | None
    summary: str
    action_required: bool
    action_description: str
    confidence: float
    ocr_chars: int
    error: str | None = None

    def to_summary_block(self) -> str:
        """Kompakte Textdarstellung für den Phase-2-Prompt."""
        lines = [
            f"[{self.filename}]",
            f"  Typ: {self.document_type}",
            f"  Tags: {', '.join(self.topic_tags) if self.topic_tags else '–'}",
            f"  Absender: {self.sender or '–'}",
            f"  Empfänger: {self.recipient or '–'}",
        ]
        if self.persons:
            pnames = "; ".join(
                f"{p['name']} ({p['role']})" for p in self.persons[:4]
            )
            lines.append(f"  Personen: {pnames}")
        if self.organizations:
            lines.append(f"  Organisationen: {', '.join(self.organizations[:4])}")
        lines.append(f"  Zusammenfassung: {self.summary}")
        if self.action_required:
            lines.append(f"  ⚠ Handlungsbedarf: {self.action_description}")
        return "\n".join(lines)


@dataclass
class ContextSynthesis:
    """Ergebnis der Phase-2-Kontext-Synthese über alle Dokumente."""
    overall_assessment: str
    tag_taxonomy: list[dict[str, Any]]
    connections: list[dict[str, Any]]
    recommended_correspondents: list[dict[str, Any]]
    error: str | None = None


@dataclass
class PreAnalysisReport:
    """Gesamtbericht der Voranalyse."""
    scan_dir: str
    created_at: str
    total_files: int
    analyzed_count: int
    failed_count: int
    # Phase 1 – Einzelanalysen
    analyses: list[DocumentPreAnalysis] = field(default_factory=list)
    document_type_counts: dict[str, int] = field(default_factory=dict)
    tag_counts: dict[str, int] = field(default_factory=dict)
    person_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    org_counts: dict[str, int] = field(default_factory=dict)
    # Phase 2 – Kontext-Synthese
    synthesis: ContextSynthesis | None = None


# ---------------------------------------------------------------------------
# LLM-Client (OpenAI-kompatibel → funktioniert auch mit Ollama)
# ---------------------------------------------------------------------------

class ChatLLMClient:
    """OpenAI-kompatibler Chat-Client.

    Funktioniert mit:
    - Ollama  (base_url = http://ollama:11434)
    - OpenAI  (base_url = https://api.openai.com)
    - Jede andere OpenAI-kompatible API
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout: int = 120,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self._base = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })

    def chat(self, system: str, user: str) -> str:
        """Sendet eine Chat-Anfrage und gibt den Antworttext zurück."""
        resp = self._session.post(
            f"{self._base}/v1/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "temperature": 0.1,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return (
            resp.json()
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )

    def is_available(self) -> bool:
        """Prüft, ob der LLM-Dienst erreichbar ist."""
        try:
            return self._session.get(
                f"{self._base}/v1/models", timeout=5
            ).status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# JSON-Parsing
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> dict:
    """Extrahiert robustes JSON aus einer LLM-Antwort."""
    # 1. Direkt parsen
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 2. JSON aus Markdown-Block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 3. Erstes JSON-Objekt
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {}


# ---------------------------------------------------------------------------
# OCR-Extraktion
# ---------------------------------------------------------------------------

def _extract_text(pdf_path: Path, max_chars: int = 6000) -> str:
    """Extrahiert Text via PyMuPDF."""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        text = "\n".join(page.get_text("text") for page in doc).strip()
        doc.close()
        return text[:max_chars]
    except Exception as exc:
        logger.warning("OCR fehlgeschlagen (%s): %s", pdf_path.name, exc)
        return ""


# ---------------------------------------------------------------------------
# Nicht-destruktives PDF-Splitting
# ---------------------------------------------------------------------------

def _split_for_analysis(pdf_path: Path, tmp_dir: Path, dpi: int = 150) -> list[Path]:
    """Splittet eine PDF an Patch-T-Codes in ein temporäres Verzeichnis.
    Die Originaldatei wird NICHT verändert.
    """
    patch_pages: list[int] = []
    try:
        import pdf2image
        from pyzbar import pyzbar

        images = pdf2image.convert_from_path(str(pdf_path), dpi=dpi)
        for idx, img in enumerate(images):
            for bc in pyzbar.decode(img):
                data = bc.data.decode("utf-8", errors="ignore").upper().strip()
                if data.startswith("PATCH") or bc.type.upper() in {"PATCHCODE", "PATCH"}:
                    patch_pages.append(idx)
                    break
    except Exception:
        pass  # Splitting ohne Patch-Erkennung: ganzes Dokument analysieren

    try:
        import fitz

        doc = fitz.open(str(pdf_path))
        total = doc.page_count
        if not patch_pages:
            doc.close()
            return [pdf_path]

        separators = sorted(set(patch_pages))
        segments: list[tuple[int, int]] = []
        start = 0
        for sep in separators:
            if sep > start:
                segments.append((start, sep - 1))
            start = sep + 1
        if start < total:
            segments.append((start, total - 1))

        created: list[Path] = []
        for i, (first, last) in enumerate(segments):
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=first, to_page=last)
            out = tmp_dir / f"{pdf_path.stem}_seg{i + 1:03d}.pdf"
            new_doc.save(str(out))
            new_doc.close()
            created.append(out)
        doc.close()
        return created or [pdf_path]
    except Exception as exc:
        logger.warning("PDF-Splitting fehlgeschlagen (%s): %s", pdf_path.name, exc)
        return [pdf_path]


# ---------------------------------------------------------------------------
# Phase 1: Einzeldokument-Analyse
# ---------------------------------------------------------------------------

def _analyse_single(pdf_path: Path, llm: ChatLLMClient) -> DocumentPreAnalysis:
    """Analysiert ein einzelnes Dokument mit dem LLM."""
    text = _extract_text(pdf_path)
    if not text.strip():
        return DocumentPreAnalysis(
            filename=pdf_path.name,
            document_type="Unbekannt",
            topic_tags=[],
            persons=[],
            organizations=[],
            sender=None,
            recipient=None,
            summary="Kein Text extrahierbar (evtl. gescanntes Bild ohne OCR-Layer).",
            action_required=False,
            action_description="",
            confidence=0.0,
            ocr_chars=0,
            error="Kein OCR-Text",
        )

    try:
        raw = llm.chat(_SYSTEM_PHASE1, _USER_PHASE1.format(text=text))
        d = _parse_json(raw)
        return DocumentPreAnalysis(
            filename=pdf_path.name,
            document_type=str(d.get("document_type", "Sonstiges")),
            topic_tags=[str(t) for t in d.get("topic_tags", []) if t],
            persons=[
                {"name": str(p.get("name", "")), "role": str(p.get("role", ""))}
                for p in d.get("persons", [])
                if isinstance(p, dict) and p.get("name")
            ],
            organizations=[str(o) for o in d.get("organizations", []) if o],
            sender=d.get("sender") or None,
            recipient=d.get("recipient") or None,
            summary=str(d.get("summary", "")),
            action_required=bool(d.get("action_required", False)),
            action_description=str(d.get("action_description", "")),
            confidence=float(d.get("confidence", 0.5)),
            ocr_chars=len(text),
        )
    except Exception as exc:
        logger.error("LLM-Analyse fehlgeschlagen (%s): %s", pdf_path.name, exc)
        return DocumentPreAnalysis(
            filename=pdf_path.name,
            document_type="Unbekannt",
            topic_tags=[],
            persons=[],
            organizations=[],
            sender=None,
            recipient=None,
            summary="",
            action_required=False,
            action_description="",
            confidence=0.0,
            ocr_chars=len(text),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Phase 2: Kontext-Synthese
# ---------------------------------------------------------------------------

def _build_summary_block(analyses: list[DocumentPreAnalysis]) -> str:
    """Erstellt den kompakten Zusammenfassungsblock für Phase 2."""
    blocks = [a.to_summary_block() for a in analyses if not a.error]
    return "\n\n".join(blocks)


def _synthesize(
    analyses: list[DocumentPreAnalysis],
    llm: ChatLLMClient,
    batch_size: int = 25,
) -> ContextSynthesis:
    """Phase 2: LLM analysiert ALLE Dokumente gemeinsam (in Batches bei Bedarf).

    Bei großen Mengen werden Dokumente in Batches synthetisiert. Ergebnisse
    werden in einem finalen Meta-Synthesis-Schritt zusammengeführt.
    """
    valid = [a for a in analyses if not a.error]
    if not valid:
        return ContextSynthesis(
            overall_assessment="Keine analysierten Dokumente vorhanden.",
            tag_taxonomy=[],
            connections=[],
            recommended_correspondents=[],
        )

    # Batches bilden
    batches = [valid[i: i + batch_size] for i in range(0, len(valid), batch_size)]

    batch_results: list[dict] = []
    for batch_idx, batch in enumerate(batches):
        logger.info(
            "Kontext-Synthese Batch %d/%d (%d Dokumente) …",
            batch_idx + 1,
            len(batches),
            len(batch),
        )
        summaries = _build_summary_block(batch)
        try:
            raw = llm.chat(
                _SYSTEM_PHASE2,
                _USER_PHASE2.format(n=len(batch), summaries=summaries),
            )
            batch_results.append(_parse_json(raw))
        except Exception as exc:
            logger.error("Phase-2-Batch %d fehlgeschlagen: %s", batch_idx + 1, exc)
            batch_results.append({})

    # Wenn mehrere Batches: Meta-Synthese
    if len(batch_results) > 1:
        _log_info("Führe Meta-Synthese aus …")
        meta_summaries = "\n\n".join(
            json.dumps(r, ensure_ascii=False, indent=2)
            for r in batch_results
            if r
        )
        meta_prompt = (
            f"Das folgende JSON enthält {len(batch_results)} Zwischen-Analysen "
            f"von je ca. {batch_size} Dokumenten. Führe sie zu EINER finalen "
            f"Tag-Taxonomie zusammen. Entferne Duplikate, fasse ähnliche Tags zusammen.\n\n"
            f"{meta_summaries}"
        )
        try:
            raw = llm.chat(_SYSTEM_PHASE2, meta_prompt)
            final = _parse_json(raw)
        except Exception as exc:
            logger.error("Meta-Synthese fehlgeschlagen: %s", exc)
            final = batch_results[0] if batch_results else {}
    else:
        final = batch_results[0] if batch_results else {}

    if not final:
        return ContextSynthesis(
            overall_assessment="Synthese konnte nicht durchgeführt werden.",
            tag_taxonomy=[],
            connections=[],
            recommended_correspondents=[],
            error="Kein JSON aus LLM erhalten",
        )

    return ContextSynthesis(
        overall_assessment=str(final.get("overall_assessment", "")),
        tag_taxonomy=[
            t for t in final.get("tag_taxonomy", [])
            if isinstance(t, dict) and t.get("tag")
        ],
        connections=[
            c for c in final.get("connections", [])
            if isinstance(c, dict) and c.get("pattern")
        ],
        recommended_correspondents=[
            r for r in final.get("recommended_correspondents", [])
            if isinstance(r, dict) and r.get("name")
        ],
    )


# ---------------------------------------------------------------------------
# Aggregation (Phase 1 → Frequency-Maps)
# ---------------------------------------------------------------------------

def _aggregate(analyses: list[DocumentPreAnalysis]) -> tuple[
    dict[str, int],   # document_type_counts
    dict[str, int],   # tag_counts
    dict[str, Any],   # person_map  {name_lower → {name, roles, count}}
    dict[str, int],   # org_counts
]:
    dtype_ctr: Counter[str] = Counter()
    tag_ctr: Counter[str] = Counter()
    org_ctr: Counter[str] = Counter()
    persons: dict[str, dict[str, Any]] = {}

    for a in analyses:
        if a.error:
            continue
        dtype_ctr[a.document_type] += 1
        for t in a.topic_tags:
            tag_ctr[t.strip()] += 1
        # Dokumenttyp auch als Tag zählen
        tag_ctr[a.document_type.strip()] += 1
        for p in a.persons:
            name = p.get("name", "").strip()
            role = p.get("role", "").strip()
            if len(name) < 3:
                continue
            key = name.lower()
            if key in persons:
                persons[key]["count"] += 1
                if role and role not in persons[key]["roles"]:
                    persons[key]["roles"].append(role)
            else:
                persons[key] = {"name": name, "roles": [role] if role else [], "count": 1}
        for org in a.organizations:
            org = org.strip()
            if len(org) >= 3:
                org_ctr[org] += 1
        if a.sender and len(a.sender.strip()) >= 3:
            org_ctr[a.sender.strip()] += 1

    return (
        dict(dtype_ctr.most_common()),
        dict(tag_ctr.most_common()),
        persons,
        dict(org_ctr.most_common()),
    )


# ---------------------------------------------------------------------------
# Hauptklasse
# ---------------------------------------------------------------------------

class PreClassifier:
    """Orchestriert die zweiphasige Voranalyse."""

    def __init__(self, config: AppConfig, llm: ChatLLMClient) -> None:
        self.config = config
        self.llm = llm

    def run(self, scan_dir: Path, output_dir: Path) -> PreAnalysisReport:
        """Führt beide Analysephasen durch und gibt den Bericht zurück."""
        pdf_files = sorted(
            scan_dir.glob("*.pdf")
            | scan_dir.glob("*.PDF")
            | scan_dir.glob("*.tif")
            | scan_dir.glob("*.tiff")
            | scan_dir.glob("*.TIF")
            | scan_dir.glob("*.TIFF"),
            key=lambda p: p.name,
        )

        if not pdf_files:
            logger.warning("Keine PDF/TIFF-Dateien in %s gefunden.", scan_dir)
            return PreAnalysisReport(
                scan_dir=str(scan_dir),
                created_at=datetime.now().isoformat(timespec="seconds"),
                total_files=0,
                analyzed_count=0,
                failed_count=0,
            )

        _print(f"Scan-Ordner: {scan_dir}  ({len(pdf_files)} Dateien gefunden)")

        analyses: list[DocumentPreAnalysis] = []

        with tempfile.TemporaryDirectory(prefix="preclass_") as tmp_str:
            tmp = Path(tmp_str)
            for file_path in pdf_files:
                # TIFF → PDF
                if file_path.suffix.lower() in {".tif", ".tiff"}:
                    file_path = _tiff_to_pdf(file_path, tmp)
                    if file_path is None:
                        continue

                segments = _split_for_analysis(
                    file_path, tmp,
                    dpi=self.config.watcher.expected_dpi,
                )
                for seg in segments:
                    _print(f"  → Analysiere: {seg.name}")
                    ana = _analyse_single(seg, self.llm)
                    analyses.append(ana)

        failed = sum(1 for a in analyses if a.error)
        analyzed = len(analyses) - failed

        _print(f"\nPhase 1 abgeschlossen: {analyzed} Dokumente analysiert, {failed} fehlgeschlagen")
        _print("Starte Phase 2: Kontext-Synthese …\n")

        synthesis = _synthesize(
            analyses,
            self.llm,
            batch_size=self.config.pre_classifier.synthesis_batch_size,
        )

        dtype_counts, tag_counts, person_map, org_counts = _aggregate(analyses)

        report = PreAnalysisReport(
            scan_dir=str(scan_dir),
            created_at=datetime.now().isoformat(timespec="seconds"),
            total_files=len(pdf_files),
            analyzed_count=analyzed,
            failed_count=failed,
            analyses=analyses,
            document_type_counts=dtype_counts,
            tag_counts=tag_counts,
            person_map=person_map,
            org_counts=org_counts,
            synthesis=synthesis,
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"pre_analysis_{ts}.json"
        _save_json(report, report_path)

        return report, report_path


# ---------------------------------------------------------------------------
# Ausgabe – NUR LISTEN, keine automatischen Aktionen
# ---------------------------------------------------------------------------

def print_report(report: PreAnalysisReport, report_path: Path) -> None:
    """Gibt den Voranalyse-Bericht als formatierte Listen aus."""
    if _RICH:
        _rich_report(report, report_path)
    else:
        _plain_report(report, report_path)


def _rich_report(report: PreAnalysisReport, report_path: Path) -> None:
    syn = report.synthesis

    console.print()
    console.print(
        Panel.fit(
            f"[bold cyan]KI-Dokumenten-Archiv: Voranalyse-Ergebnis[/bold cyan]\n"
            f"[dim]Nichts wird automatisch angelegt – nur Vorschläge![/dim]\n\n"
            f"Scan-Ordner : [yellow]{report.scan_dir}[/yellow]\n"
            f"Erstellt    : {report.created_at}\n"
            f"Dateien     : {report.total_files} gesamt | "
            f"[green]{report.analyzed_count}[/green] analysiert | "
            f"[red]{report.failed_count}[/red] fehlgeschlagen",
            box=box.DOUBLE,
            padding=(0, 2),
        )
    )

    # Gesamteinschätzung (Phase 2)
    if syn and syn.overall_assessment:
        console.print()
        console.print(Rule("[bold]🔍 Gesamteinschätzung der Dokumentensammlung[/bold]"))
        console.print(f"  {syn.overall_assessment}\n")

    # Erkannte Dokumenttypen (Phase 1 – Häufigkeiten)
    if report.document_type_counts:
        console.print(Rule("[bold]📋 Erkannte Dokumenttypen[/bold]"))
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        t.add_column("Dokumenttyp", style="cyan", no_wrap=True)
        t.add_column("Anzahl", justify="right")
        t.add_column("Balken")
        total = sum(report.document_type_counts.values())
        for dtype, count in report.document_type_counts.items():
            bar = "█" * min(count * 2, 30)
            pct = f"{count / total * 100:.0f}%"
            t.add_row(dtype, f"{count}×", f"{bar}  {pct}")
        console.print(t)

    # Kontextuell empfohlene Tags (Phase 2)
    if syn and syn.tag_taxonomy:
        console.print(
            Rule(f"[bold]🏷️  Empfohlene Tags – kontextuell analysiert "
                 f"({len(syn.tag_taxonomy)} Vorschläge)[/bold]")
        )
        console.print(
            "  [dim]Diese Tags berücksichtigen Zusammenhänge über alle Dokumente.[/dim]\n"
        )
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        t.add_column("Tag", style="cyan", no_wrap=True)
        t.add_column("Kategorie", style="yellow")
        t.add_column("Priorität")
        t.add_column("Begründung / Kontext")
        for tag_info in sorted(
            syn.tag_taxonomy,
            key=lambda x: {"hoch": 0, "mittel": 1, "niedrig": 2}.get(
                str(x.get("priority", "niedrig")).lower(), 3
            ),
        ):
            prio = str(tag_info.get("priority", "–"))
            prio_styled = {
                "hoch": "[bold red]hoch[/bold red]",
                "mittel": "[yellow]mittel[/yellow]",
                "niedrig": "[dim]niedrig[/dim]",
            }.get(prio.lower(), prio)
            files = tag_info.get("affected_files", [])
            file_hint = f" ({len(files)} Dok.)" if files else ""
            t.add_row(
                tag_info.get("tag", "–"),
                tag_info.get("category", "–"),
                prio_styled,
                tag_info.get("rationale", "–")[:80] + file_hint,
            )
        console.print(t)

    # Erkannte Zusammenhänge (Phase 2)
    if syn and syn.connections:
        console.print(
            Rule(f"[bold]🔗 Erkannte Zusammenhänge ({len(syn.connections)})[/bold]")
        )
        for i, conn in enumerate(syn.connections, 1):
            files = conn.get("files", [])
            suggested = conn.get("suggested_tag", "")
            console.print(
                f"  [bold]{i}.[/bold] {conn.get('pattern', '–')}"
            )
            if suggested:
                console.print(f"      [dim]→ Empfohlener Tag:[/dim] [cyan]{suggested}[/cyan]")
            if files:
                console.print(
                    f"      [dim]Betroffene Dateien:[/dim] "
                    f"{', '.join(str(f) for f in files[:5])}"
                    + (f" … (+{len(files) - 5})" if len(files) > 5 else "")
                )
        console.print()

    # Gefundene Personen & Kontakte
    all_persons = sorted(
        report.person_map.values(), key=lambda p: p["count"], reverse=True
    )
    if all_persons:
        console.print(
            Rule(f"[bold]👤 Gefundene Personen & Kontakte ({len(all_persons)})[/bold]")
        )
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        t.add_column("Name", style="cyan", no_wrap=True)
        t.add_column("Rollen")
        t.add_column("Dokumente", justify="right")
        for p in all_persons:
            roles = ", ".join(p["roles"][:3]) if p["roles"] else "–"
            t.add_row(p["name"], roles, f"{p['count']}×")
        console.print(t)

    # Empfohlene Korrespondenten (Phase 2)
    if syn and syn.recommended_correspondents:
        console.print(
            Rule(
                f"[bold]🏢 Empfohlene Korrespondenten – Phase-2-Analyse "
                f"({len(syn.recommended_correspondents)})[/bold]"
            )
        )
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        t.add_column("Name", style="cyan", no_wrap=True)
        t.add_column("Typ", style="yellow")
        t.add_column("Rolle in der Sammlung")
        t.add_column("Dok.", justify="right")
        for r in syn.recommended_correspondents:
            t.add_row(
                r.get("name", "–"),
                r.get("type", "–"),
                r.get("rationale", "–")[:70],
                str(r.get("document_count", "–")),
            )
        console.print(t)

    # Dokumente mit Handlungsbedarf
    action_docs = [a for a in report.analyses if a.action_required and not a.error]
    if action_docs:
        console.print(
            Rule(f"[bold red]⚠️  Dokumente mit Handlungsbedarf ({len(action_docs)})[/bold red]")
        )
        for a in action_docs:
            console.print(f"  • [yellow]{a.filename}[/yellow]: {a.action_description}")
        console.print()

    # Fehlgeschlagene Dokumente
    failed_docs = [a for a in report.analyses if a.error]
    if failed_docs:
        console.print(Rule(f"[bold red]❌ Fehler ({len(failed_docs)})[/bold red]"))
        for a in failed_docs:
            console.print(f"  • [dim]{a.filename}[/dim]: {a.error}")
        console.print()

    console.print(
        Panel.fit(
            f"[bold green]✅ Analyse abgeschlossen[/bold green]\n\n"
            f"Vollständiger JSON-Bericht:\n[yellow]{report_path}[/yellow]\n\n"
            f"[dim]→ Bitte Tags und Kontakte MANUELL in Paperless anlegen.[/dim]",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )
    console.print()


def _plain_report(report: PreAnalysisReport, report_path: Path) -> None:
    """Einfache Textausgabe ohne Rich."""
    sep = "═" * 65
    syn = report.synthesis

    print(f"\n{sep}")
    print("  KI-Dokumenten-Archiv: Voranalyse-Ergebnis")
    print("  (Nichts wird automatisch angelegt – nur Vorschläge!)")
    print(sep)
    print(f"  Scan-Ordner : {report.scan_dir}")
    print(f"  Erstellt    : {report.created_at}")
    print(f"  Dateien     : {report.total_files} | analysiert: {report.analyzed_count} | Fehler: {report.failed_count}")
    print(sep)

    if syn and syn.overall_assessment:
        print("\nGESAMTEINSCHÄTZUNG:")
        print(f"  {syn.overall_assessment}")

    if report.document_type_counts:
        print("\nERKANNTE DOKUMENTTYPEN:")
        for dtype, count in report.document_type_counts.items():
            print(f"  {count:4d}×  {dtype}")

    if syn and syn.tag_taxonomy:
        print(f"\nEMPFOHLENE TAGS ({len(syn.tag_taxonomy)} Vorschläge, kontextuell analysiert):")
        for tag_info in sorted(
            syn.tag_taxonomy,
            key=lambda x: {"hoch": 0, "mittel": 1, "niedrig": 2}.get(
                str(x.get("priority", "")).lower(), 3
            ),
        ):
            files = tag_info.get("affected_files", [])
            print(
                f"  [{tag_info.get('tag', '?')}]"
                f"  Kategorie: {tag_info.get('category', '?')}"
                f"  Priorität: {tag_info.get('priority', '?')}"
                f"  ({len(files)} Dok.)"
            )
            print(f"      Begründung: {tag_info.get('rationale', '')}")

    if syn and syn.connections:
        print(f"\nERKANNTE ZUSAMMENHÄNGE ({len(syn.connections)}):")
        for i, c in enumerate(syn.connections, 1):
            print(f"  {i}. {c.get('pattern', '?')}")
            if c.get("suggested_tag"):
                print(f"     → Tag-Empfehlung: {c['suggested_tag']}")
            files = c.get("files", [])
            if files:
                print(f"     Dateien: {', '.join(str(f) for f in files[:5])}")

    persons_sorted = sorted(
        report.person_map.values(), key=lambda p: p["count"], reverse=True
    )
    if persons_sorted:
        print(f"\nGEFUNDENE PERSONEN & KONTAKTE ({len(persons_sorted)}):")
        for p in persons_sorted:
            roles = ", ".join(p["roles"]) if p["roles"] else "–"
            print(f"  {p['name']:35s}  [{roles}]  {p['count']}×")

    if syn and syn.recommended_correspondents:
        print(f"\nEMPFOHLENE KORRESPONDENTEN ({len(syn.recommended_correspondents)}):")
        for r in syn.recommended_correspondents:
            print(
                f"  {r.get('name', '?'):35s}  "
                f"{r.get('type', '?'):12s}  "
                f"{r.get('document_count', '?')}×"
            )
            print(f"      {r.get('rationale', '')}")

    action_docs = [a for a in report.analyses if a.action_required and not a.error]
    if action_docs:
        print(f"\nHANDLUNGSBEDARF ({len(action_docs)} Dokumente):")
        for a in action_docs:
            print(f"  ! {a.filename}")
            print(f"    {a.action_description}")

    failed_docs = [a for a in report.analyses if a.error]
    if failed_docs:
        print(f"\nFEHLER ({len(failed_docs)} Dokumente):")
        for a in failed_docs:
            print(f"  ✗ {a.filename}: {a.error}")

    print(f"\n{sep}")
    print(f"  JSON-Bericht: {report_path}")
    print("  → Tags und Kontakte bitte MANUELL in Paperless anlegen.")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# JSON-Bericht speichern
# ---------------------------------------------------------------------------

def _save_json(report: PreAnalysisReport, path: Path) -> None:
    syn = report.synthesis
    data: dict[str, Any] = {
        "meta": {
            "scan_dir": report.scan_dir,
            "created_at": report.created_at,
            "total_files": report.total_files,
            "analyzed_count": report.analyzed_count,
            "failed_count": report.failed_count,
        },
        "phase1_document_types": report.document_type_counts,
        "phase1_tag_frequencies": report.tag_counts,
        "phase1_persons": sorted(
            report.person_map.values(), key=lambda p: p["count"], reverse=True
        ),
        "phase1_organizations": report.org_counts,
        "phase2_overall_assessment": syn.overall_assessment if syn else "",
        "phase2_tag_taxonomy": syn.tag_taxonomy if syn else [],
        "phase2_connections": syn.connections if syn else [],
        "phase2_recommended_correspondents": (
            syn.recommended_correspondents if syn else []
        ),
        "phase2_synthesis_error": syn.error if syn else None,
        "document_analyses": [
            {
                "filename": a.filename,
                "document_type": a.document_type,
                "topic_tags": a.topic_tags,
                "persons": a.persons,
                "organizations": a.organizations,
                "sender": a.sender,
                "recipient": a.recipient,
                "summary": a.summary,
                "action_required": a.action_required,
                "action_description": a.action_description,
                "confidence": a.confidence,
                "error": a.error,
            }
            for a in report.analyses
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _tiff_to_pdf(tif_path: Path, tmp_dir: Path) -> Path | None:
    try:
        import fitz
        pdf_path = tmp_dir / (tif_path.stem + ".pdf")
        doc = fitz.open()
        img_doc = fitz.open(str(tif_path))
        for page in img_doc:
            page_doc = fitz.open("pdf", page.get_pixmap().pdfocr_tobytes())
            doc.insert_pdf(page_doc)
        doc.save(str(pdf_path))
        doc.close()
        img_doc.close()
        return pdf_path
    except Exception as exc:
        logger.warning("TIFF-Konvertierung fehlgeschlagen (%s): %s", tif_path.name, exc)
        return None


def _log_info(msg: str) -> None:
    logger.info(msg)


def _print(msg: str) -> None:
    if _RICH:
        console.print(f"  [dim]{msg}[/dim]")
    else:
        print(f"  {msg}")


# ---------------------------------------------------------------------------
# CLI-Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Zweiphasige KI-Voranalyse des Scan-Ordners.\n"
            "Phase 1: Einzeldokument-Analyse.\n"
            "Phase 2: Kontext-Synthese über alle Dokumente.\n"
            "Ausgabe: Vorschlagslisten für Tags und Kontakte (kein Auto-Import)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python pre_classifier.py
  python pre_classifier.py --scan-dir /data/scans --output /data/pre_analysis
  python pre_classifier.py --ollama-model llama3.1
  python pre_classifier.py --backend openai   # benötigt OPENAI_API_KEY

Via Docker Compose:
  docker compose --profile preclass up ollama
  docker compose --profile preclass run --rm pre_classifier
        """,
    )
    parser.add_argument("--scan-dir", type=Path, default=None)
    parser.add_argument("--output", "-o", type=Path, default=None)
    parser.add_argument(
        "--backend",
        choices=["ollama", "openai"],
        default=None,
        help="LLM-Backend (überschreibt config.yaml)",
    )
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument("--ollama-model", default=None)
    parser.add_argument("--openai-model", default=None)
    args = parser.parse_args()

    import os as _os

    config = load_config()
    configure_logging(config)
    pc = config.pre_classifier

    scan_dir = args.scan_dir or Path(config.paths.scan_input_dir)
    output_dir = args.output or Path(pc.report_output_dir)

    if not scan_dir.exists():
        logger.error("Scan-Verzeichnis nicht gefunden: %s", scan_dir)
        sys.exit(1)

    # LLM-Backend bestimmen
    backend = args.backend or pc.llm_backend
    openai_key = _os.environ.get("OPENAI_API_KEY", "")

    if backend == "openai" and openai_key:
        model = args.openai_model or pc.openai_model
        llm = ChatLLMClient(
            base_url="https://api.openai.com",
            model=model,
            api_key=openai_key,
        )
        _print(f"LLM-Backend: OpenAI ({model})")
    else:
        url = args.ollama_url or pc.ollama_base_url
        model = args.ollama_model or pc.ollama_model
        llm = ChatLLMClient(base_url=url, model=model, api_key="ollama")
        if not llm.is_available():
            msg = (
                f"Ollama unter {url} nicht erreichbar.\n"
                f"Stelle sicher, dass der Ollama-Container läuft:\n"
                f"  docker compose --profile preclass up ollama\n"
                f"Und das Modell '{model}' geladen ist:\n"
                f"  docker compose --profile preclass exec ollama ollama pull {model}"
            )
            if not pc.allow_llm_fallback:
                logger.error(msg)
                sys.exit(1)
            logger.warning(msg)
        _print(f"LLM-Backend: Ollama @ {url} (Modell: {model})")

    classifier = PreClassifier(config=config, llm=llm)
    report, report_path = classifier.run(scan_dir=scan_dir, output_dir=output_dir)
    print_report(report, report_path)


if __name__ == "__main__":
    main()
