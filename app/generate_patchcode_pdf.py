#!/usr/bin/env python3
"""
generate_patchcode_pdf.py – Erstellt PDF-Trennseiten mit Patch-T Codes.

Patch-T Codes sind horizontale Strichmuster, die von Dokumentenscannern als
Batch-Trennseiten erkannt werden. Dieses Skript erzeugt ein druckfertiges PDF
mit einer oder mehreren Patch-T Trennseiten.

Das Erstellungsdatum wird prominent auf der Seite angezeigt, um die Generierung
der Patchcodes nachvollziehbar zu machen.

Verwendung:
    python generate_patchcode_pdf.py [--output DATEI] [--count ANZAHL] [--date DATUM]

Beispiel:
    python generate_patchcode_pdf.py --output trennseiten.pdf --count 5
    python generate_patchcode_pdf.py --output trenn_2024-01-15.pdf --date 2024-01-15

Hinweis: Der generierte PDF-Code entspricht dem international standardisierten
Patch-T-Muster (abgeleitet aus Code-39 Zeichen 'T'):
  Bar-Breiten: schmal=1, breit=3 (Einheiten)
  Muster: B-S-B-S-B-S-B-S-B-S-B-S-B-S
  (B=Bar/schwarz, S=Space/weiß, Breiten lt. Code-39-T-Kodierung)
"""

import argparse
import sys
from datetime import datetime, date
from pathlib import Path

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib.styles import getSampleStyleSheet
except ImportError:
    print(
        "Fehler: reportlab ist nicht installiert.\n"
        "Bitte installieren mit: pip install reportlab",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Patch-T Code Definition
#
# Der Patch-T Code ist das Code-39-Zeichen 'T' als Patch-Code-Variante.
# Code-39 Encoding für 'T':
#   narrow bar (1) = n
#   wide bar   (3) = W
#   narrow space (1) = n
#   wide space   (3) = W
#
# Binärmuster für 'T' in Code 39:
#   1 = schwarzer Balken (Bar)
#   0 = weißer Zwischenraum (Space)
#
# Code-39 'T' Muster:
#   Bar | Space | Bar | Space | Bar | Space | Bar | Space | Bar | Space | Bar
#   1     1       3     1       1     1       3     1       1     1       1
#   (Breite in narrow-Einheiten)
# ---------------------------------------------------------------------------

PATCH_T_PATTERN = [
    # (breite_in_units, ist_bar)  — Breiten in "narrow module" Einheiten
    (1, True),   # Bar 1 (narrow)
    (1, False),  # Space 1 (narrow)
    (3, True),   # Bar 3 (wide)
    (1, False),  # Space 1 (narrow)
    (1, True),   # Bar 1 (narrow)
    (1, False),  # Space 1 (narrow)
    (3, True),   # Bar 3 (wide)
    (1, False),  # Space 1 (narrow)
    (1, True),   # Bar 1 (narrow)
    (1, False),  # Space 1 (narrow)
    (1, True),   # Bar 1 (narrow)
]

# Ruhezone (Quiet Zone) vor und nach dem Barcode: 10 narrow modules
QUIET_ZONE_MODULES = 10


def draw_patch_t_code(
    c: canvas.Canvas,
    x: float,
    y: float,
    bar_height: float = 25 * mm,
    module_width: float = 1.5 * mm,
    repeat_rows: int = 6,
    row_gap: float = 3 * mm,
) -> float:
    """Zeichnet einen Patch-T Code auf die Zeichenfläche.

    Der Patch-T Code besteht aus mehreren identischen horizontalen Zeilen
    des Strichmusters, die übereinander gedruckt werden. Diese Wiederholung
    macht ihn für Scanner erkennbar.

    Args:
        c: ReportLab Canvas-Objekt.
        x: X-Startposition (linke Kante) in Punkten.
        y: Y-Startposition (obere Kante) der untersten Zeile in Punkten.
        bar_height: Höhe eines einzelnen Streifens.
        module_width: Breite einer "narrow module"-Einheit.
        repeat_rows: Anzahl der übereinander gedruckten Musterzeilen.
        row_gap: Abstand zwischen den Zeilen.

    Returns:
        Gesamtbreite des gedruckten Barcodes in Punkten.
    """
    # Gesamtbreite berechnen
    total_modules = QUIET_ZONE_MODULES * 2 + sum(w for w, _ in PATCH_T_PATTERN)
    total_width = total_modules * module_width

    for row_idx in range(repeat_rows):
        row_y = y + row_idx * (bar_height + row_gap)
        draw_x = x + QUIET_ZONE_MODULES * module_width  # Ruhezone links

        for width_units, is_bar in PATCH_T_PATTERN:
            bar_width = width_units * module_width
            if is_bar:
                c.setFillColor(colors.black)
                c.rect(draw_x, row_y, bar_width, bar_height, fill=1, stroke=0)
            draw_x += bar_width

    return total_width


def create_patch_t_page(
    c: canvas.Canvas,
    page_width: float,
    page_height: float,
    generation_date: date,
    page_number: int = 1,
    total_pages: int = 1,
) -> None:
    """Erstellt eine einzelne Patch-T Trennseite.

    Args:
        c: ReportLab Canvas.
        page_width: Seitenbreite in Punkten.
        page_height: Seitenhöhe in Punkten.
        generation_date: Erstellungsdatum (wird auf der Seite angezeigt).
        page_number: Aktuelle Seitennummer (1-basiert).
        total_pages: Gesamtzahl der Seiten.
    """
    margin = 20 * mm

    # Hintergrund weiß
    c.setFillColor(colors.white)
    c.rect(0, 0, page_width, page_height, fill=1, stroke=0)

    # -------------------------------------------------------------------------
    # Patch-T Codes: 3 Gruppen verteilt über die Seite
    # -------------------------------------------------------------------------
    bar_height = 8 * mm
    module_width = 1.8 * mm
    repeat_rows = 5
    row_gap = 2 * mm

    # Breite des Barcodes berechnen
    total_modules = QUIET_ZONE_MODULES * 2 + sum(w for w, _ in PATCH_T_PATTERN)
    barcode_width = total_modules * module_width

    # Horizontal zentrieren
    x_center = (page_width - barcode_width) / 2

    # Höhe einer Gruppe: repeat_rows * (bar_height + row_gap)
    group_height = repeat_rows * (bar_height + row_gap)

    # Drei Gruppen: oben, Mitte, unten
    positions_y = [
        page_height - margin - group_height,           # oben
        (page_height - group_height) / 2,              # Mitte
        margin,                                         # unten
    ]

    for y_pos in positions_y:
        draw_patch_t_code(
            c,
            x=x_center,
            y=y_pos,
            bar_height=bar_height,
            module_width=module_width,
            repeat_rows=repeat_rows,
            row_gap=row_gap,
        )

    # -------------------------------------------------------------------------
    # Beschriftung
    # -------------------------------------------------------------------------
    c.setFillColor(colors.black)

    # Titel
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(
        page_width / 2,
        page_height - margin - group_height - 12 * mm,
        "PATCH-T TRENNSEITE / SEPARATOR PAGE",
    )

    # Datum (prominent)
    c.setFont("Helvetica-Bold", 11)
    date_str = generation_date.strftime("%d.%m.%Y")
    c.drawCentredString(
        page_width / 2,
        page_height - margin - group_height - 22 * mm,
        f"Erstellt am: {date_str}",
    )

    # Erläuterung
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.grey)
    c.drawCentredString(
        page_width / 2,
        page_height - margin - group_height - 31 * mm,
        "Diese Seite wird vom Scanner als Batch-Trennseite erkannt und nicht archiviert.",
    )

    # Seitennummer
    c.setFont("Helvetica", 8)
    c.drawCentredString(
        page_width / 2,
        margin / 2,
        f"Seite {page_number} von {total_pages}  |  Patch-T Code  |  {date_str}",
    )

    # Rahmen
    c.setStrokeColor(colors.lightgrey)
    c.setLineWidth(0.5)
    c.rect(margin / 2, margin / 2, page_width - margin, page_height - margin, fill=0, stroke=1)


def generate_patch_pdf(
    output_path: Path,
    count: int = 1,
    generation_date: date | None = None,
) -> None:
    """Erzeugt ein PDF mit Patch-T Trennseiten.

    Args:
        output_path: Ausgabepfad für die PDF-Datei.
        count: Anzahl der zu generierenden Trennseiten.
        generation_date: Erstellungsdatum (Standard: heute).
    """
    if generation_date is None:
        generation_date = date.today()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    page_width, page_height = A4

    c = canvas.Canvas(str(output_path), pagesize=A4)

    # PDF-Metadaten
    c.setTitle("Patch-T Trennseiten")
    c.setAuthor("KI-Dokumenten-Archiv")
    c.setSubject(
        f"Patch-T Code Separator Pages – Erstellt: {generation_date.strftime('%Y-%m-%d')}"
    )
    c.setCreator("generate_patchcode_pdf.py")

    for page_num in range(1, count + 1):
        create_patch_t_page(
            c,
            page_width=page_width,
            page_height=page_height,
            generation_date=generation_date,
            page_number=page_num,
            total_pages=count,
        )
        c.showPage()

    c.save()
    print(f"✓ PDF erstellt: {output_path}")
    print(f"  Seiten: {count}")
    print(f"  Datum:  {generation_date.strftime('%d.%m.%Y')}")
    print(f"  Größe:  {output_path.stat().st_size:,} Bytes")


# ---------------------------------------------------------------------------
# Kommandozeilenaufruf
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Erstellt PDF-Trennseiten mit Patch-T Codes für Dokumentenscanner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python generate_patchcode_pdf.py
      → Erstellt 'patch_separator.pdf' mit 1 Seite (heutiges Datum)

  python generate_patchcode_pdf.py --count 10 --output stapel_trenner.pdf
      → Erstellt 10 Trennseiten

  python generate_patchcode_pdf.py --date 2024-03-15 --output archiv_2024-03.pdf
      → Erstellt Trennseite mit festem Datum

Druckhinweise:
  - Auf weißem Papier drucken (kein Recyclingpapier)
  - Schwarz-Weiß-Druck, Hochqualitätsmodus
  - Keine Skalierung/Anpassung der Seitengröße
  - Nach dem Drucken nicht knittern oder verschmutzen
        """,
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("patch_separator.pdf"),
        help="Ausgabedatei (Standard: patch_separator.pdf)",
    )
    parser.add_argument(
        "--count",
        "-c",
        type=int,
        default=1,
        help="Anzahl der Trennseiten (Standard: 1)",
    )
    parser.add_argument(
        "--date",
        "-d",
        type=str,
        default=None,
        help="Erstellungsdatum im Format YYYY-MM-DD (Standard: heute)",
    )

    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count muss mindestens 1 sein.")

    gen_date: date | None = None
    if args.date:
        try:
            gen_date = date.fromisoformat(args.date)
        except ValueError:
            parser.error(f"Ungültiges Datum '{args.date}'. Format: YYYY-MM-DD")

    generate_patch_pdf(
        output_path=args.output,
        count=args.count,
        generation_date=gen_date,
    )


if __name__ == "__main__":
    main()
