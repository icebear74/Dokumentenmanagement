"""
watcher.py – Überwacht den Scan-Eingangsordner, erkennt Patch-T Codes
             und splittet PDFs an den Trennstellen.

Konfiguration vollständig über config.yaml (kein Hardcoding).
Verwendet watchdog für Echtzeit-Ereignisse + Fallback auf Polling.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import fitz  # PyMuPDF
from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from config_loader import AppConfig, configure_logging, load_config

logger = logging.getLogger(__name__)

# Patch-T-Code Erkennung: Zolsoft/pyzbar kann QR/Barcodes lesen,
# aber Patch Codes sind spezielle 1D-Muster.
# Wir nutzen pdf2image + pyzbar für die Erkennung.
try:
    import pdf2image
    from pyzbar import pyzbar

    _BARCODE_AVAILABLE = True
except ImportError:
    _BARCODE_AVAILABLE = False
    logger.warning("pyzbar/pdf2image nicht verfügbar – Patch-T-Erkennung deaktiviert.")


# ---------------------------------------------------------------------------
# Patch-T-Code-Erkennung
# ---------------------------------------------------------------------------

PATCH_CODE_TYPES = {
    "PATCH T",
    "PATCH I",
    "PATCH II",
    "PATCH III",
    "PATCH IV",
    "PATCH VI",
}


def detect_patch_codes(pdf_path: Path, dpi: int = 300) -> list[int]:
    """Gibt eine Liste von Seitennummern (0-basiert) zurück, die Patch Codes enthalten.

    Args:
        pdf_path: Pfad zur PDF-Datei.
        dpi: Auflösung für die Konvertierung (niedrig für Geschwindigkeit).

    Returns:
        Liste der Seitenindizes mit Patch Codes.
    """
    if not _BARCODE_AVAILABLE:
        logger.warning("Barcode-Bibliotheken fehlen, überspringe Patch-Erkennung für %s", pdf_path.name)
        return []

    patch_pages: list[int] = []
    try:
        images = pdf2image.convert_from_path(str(pdf_path), dpi=dpi)
        for page_idx, image in enumerate(images):
            barcodes = pyzbar.decode(image)
            for bc in barcodes:
                bc_data = bc.data.decode("utf-8", errors="ignore").upper().strip()
                bc_type = bc.type.upper()
                if bc_data in PATCH_CODE_TYPES or bc_type in {"PATCHCODE", "PATCH"}:
                    logger.info(
                        "Patch-Code '%s' (Typ=%s) auf Seite %d in %s erkannt.",
                        bc_data,
                        bc_type,
                        page_idx + 1,
                        pdf_path.name,
                    )
                    patch_pages.append(page_idx)
    except Exception as exc:
        logger.error("Fehler bei Patch-Erkennung für %s: %s", pdf_path.name, exc)
    return patch_pages


def split_pdf_at_patches(
    pdf_path: Path,
    patch_pages: list[int],
    output_dir: Path,
) -> list[Path]:
    """Splittet eine PDF an den Patch-Code-Seiten.

    Patch-Code-Seiten selbst werden nicht in die Ausgabe-PDFs übernommen.

    Args:
        pdf_path: Eingabe-PDF.
        patch_pages: Seitenindizes der Patch Codes (werden als Trenner behandelt).
        output_dir: Ausgabeverzeichnis für die geteilten PDFs.

    Returns:
        Liste der erstellten Teil-PDFs.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count

    # Segmente bestimmen: Seitenbereiche zwischen Patch Codes
    separators = sorted(set(patch_pages))
    segments: list[tuple[int, int]] = []
    start = 0
    for sep in separators:
        if sep > start:
            segments.append((start, sep - 1))
        start = sep + 1  # Patch-Seite überspringen
    if start < total_pages:
        segments.append((start, total_pages - 1))

    created: list[Path] = []
    stem = pdf_path.stem

    for seg_idx, (first, last) in enumerate(segments):
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=first, to_page=last)
        out_path = output_dir / f"{stem}_part{seg_idx + 1:03d}.pdf"
        new_doc.save(str(out_path))
        new_doc.close()
        logger.info(
            "Segment %d/%d gespeichert: %s (Seiten %d–%d)",
            seg_idx + 1,
            len(segments),
            out_path.name,
            first + 1,
            last + 1,
        )
        created.append(out_path)

    doc.close()
    return created


# ---------------------------------------------------------------------------
# Watchdog-Handler
# ---------------------------------------------------------------------------

class ScanFolderHandler(FileSystemEventHandler):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.scan_input = Path(config.paths.scan_input_dir)
        self.split_output = Path(config.paths.split_output_dir)
        self.tmp_dir = Path(config.paths.tmp_dir)

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in {".pdf", ".tif", ".tiff"}:
            return

        self._wait_for_stable_file(path)
        self._process_file(path)

    def _wait_for_stable_file(self, path: Path) -> None:
        """Wartet, bis die Datei nicht mehr wächst (vollständig geschrieben)."""
        wait = self.config.watcher.file_stable_wait_seconds
        min_size = self.config.watcher.min_file_size_bytes
        prev_size = -1
        while True:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return
            if size >= min_size and size == prev_size:
                break
            prev_size = size
            time.sleep(wait)

    def _process_file(self, path: Path) -> None:
        logger.info("Neue Datei erkannt: %s", path.name)

        # TIF → PDF konvertieren
        if path.suffix.lower() in {".tif", ".tiff"}:
            path = self._tif_to_pdf(path)
            if path is None:
                return

        patch_pages = detect_patch_codes(
            path,
            dpi=min(300, self.config.watcher.expected_dpi),
        )

        if patch_pages:
            logger.info(
                "%d Patch-Code(s) gefunden in %s – starte Split.",
                len(patch_pages),
                path.name,
            )
            split_pdf_at_patches(path, patch_pages, self.split_output)
        else:
            # Kein Patch Code – Datei direkt in split_output verschieben
            dest = self.split_output / path.name
            shutil.move(str(path), str(dest))
            logger.info("Keine Patch Codes – Datei direkt verschoben: %s", dest.name)

    def _tif_to_pdf(self, tif_path: Path) -> Path | None:
        """Konvertiert ein TIFF in eine PDF-Datei."""
        try:
            pdf_path = self.tmp_dir / (tif_path.stem + ".pdf")
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            doc = fitz.open()
            img_doc = fitz.open(str(tif_path))
            for page in img_doc:
                pdfbytes = page.get_pixmap().pdfocr_tobytes()
                page_doc = fitz.open("pdf", pdfbytes)
                doc.insert_pdf(page_doc)
            doc.save(str(pdf_path))
            doc.close()
            img_doc.close()
            logger.info("TIF → PDF: %s", pdf_path.name)
            return pdf_path
        except Exception as exc:
            logger.error("TIF-Konvertierung fehlgeschlagen für %s: %s", tif_path.name, exc)
            return None


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    configure_logging(config)

    scan_dir = Path(config.paths.scan_input_dir)
    split_dir = Path(config.paths.split_output_dir)
    tmp_dir = Path(config.paths.tmp_dir)

    for d in (scan_dir, split_dir, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)

    handler = ScanFolderHandler(config)

    # Versuche inotify-basierte Überwachung, Fallback auf Polling
    try:
        observer = Observer()
        observer.schedule(handler, str(scan_dir), recursive=False)
        observer.start()
        logger.info(
            "Watcher gestartet (inotify): überwache %s",
            scan_dir,
        )
    except Exception:
        observer = PollingObserver(
            timeout=config.watcher.poll_interval_seconds
        )
        observer.schedule(handler, str(scan_dir), recursive=False)
        observer.start()
        logger.info(
            "Watcher gestartet (polling, interval=%ds): überwache %s",
            config.watcher.poll_interval_seconds,
            scan_dir,
        )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Watcher wird beendet …")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
