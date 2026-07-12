"""Generate synthetic, permission-safe PDFs for local ingestion benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = ROOT / "fixtures"
GOLDENS_DIR = FIXTURES_DIR / "goldens"
FONT_NAME = "BenchmarkDevanagari"
OCR_SCAN_TEXT = (
    "SCANNED ARCHIVE RECORD This page intentionally contains pixels only. "
    "Its expected text is recovered by the OCR candidate, not by the native PDF parser."
)
OCR_ONLY_PAGES = {"scan-en-001": {1}, "mixed-en-001": {2}}


def write_golden(name: str, pages: list[str | None] | None) -> None:
    if pages is None:
        return
    payload = {
        "fixture_id": name,
        "pages": [
            {
                "page_number": index,
                "text": text,
                "scorable_by_native_parser": (
                    text is not None and index not in OCR_ONLY_PAGES.get(name, set())
                ),
                "scorable_by_ocr": index in OCR_ONLY_PAGES.get(name, set()),
            }
            for index, text in enumerate(pages, start=1)
        ],
    }
    (GOLDENS_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def new_canvas(
    path: Path, *, encryption: StandardEncryption | None = None
) -> canvas.Canvas:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    document.setTitle(path.stem)
    document.setAuthor("RAG API benchmark fixture generator")
    document.setCreator("RAG API")
    if encryption is not None:
        document.setEncrypt(encryption)
    return document


def draw_wrapped(document: canvas.Canvas, text: str, x: int, y: int, width: int) -> int:
    words = text.split()
    line: list[str] = []
    for word in words:
        candidate = " ".join([*line, word])
        if document.stringWidth(candidate) > width and line:
            document.drawString(x, y, " ".join(line))
            y -= 16
            line = [word]
        else:
            line.append(word)
    if line:
        document.drawString(x, y, " ".join(line))
        y -= 16
    return y


def create_scan_image(path: Path, font_path: Path) -> None:
    image = Image.new("RGB", (1500, 2100), "#f7f2e8")
    drawer = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path), 54)
    body = ImageFont.truetype(str(font_path), 36)
    drawer.text((100, 120), "SCANNED ARCHIVE RECORD", fill="black", font=font)
    drawer.multiline_text(
        (100, 260),
        "This page intentionally contains pixels only.\n"
        "Its expected text is recovered by the OCR candidate,\n"
        "not by the native PDF parser.",
        fill="black",
        font=body,
        spacing=28,
    )
    image.save(path, format="PNG")


def create_native_simple(path: Path) -> list[str]:
    text = "Native fixture: a searchable PDF with a stable first-page citation."
    document = new_canvas(path)
    document.setFont("Helvetica", 12)
    draw_wrapped(document, text, 72, 760, 450)
    document.showPage()
    document.save()
    return [text]


def create_multicolumn(path: Path) -> list[str]:
    left = "Left column begins. It contains the first ordered statement."
    right = "Right column begins. It contains the second ordered statement."
    document = new_canvas(path)
    document.setFont("Helvetica", 11)
    draw_wrapped(document, left, 72, 760, 210)
    draw_wrapped(document, right, 320, 760, 210)
    document.showPage()
    document.save()
    return [f"{left} {right}"]


def create_table(path: Path) -> list[str]:
    rows = [("Item", "Quantity", "Price"), ("Books", "3", "$30"), ("Maps", "2", "$18")]
    document = new_canvas(path)
    document.setFont("Helvetica", 12)
    y = 760
    for row in rows:
        x = 72
        for cell in row:
            document.setStrokeColor(HexColor("#333333"))
            document.rect(x, y - 20, 140, 24)
            document.drawString(x + 5, y - 5, cell)
            x += 140
        y -= 24
    document.showPage()
    document.save()
    return [" ".join(cell for row in rows for cell in row)]


def create_scan(path: Path, image_path: Path) -> list[str]:
    document = new_canvas(path)
    document.drawImage(ImageReader(str(image_path)), 45, 70, width=500, height=700)
    document.showPage()
    document.save()
    return [OCR_SCAN_TEXT]


def create_mixed(path: Path, image_path: Path) -> list[str]:
    native = "Mixed fixture native page: this must remain native provenance."
    document = new_canvas(path)
    document.setFont("Helvetica", 12)
    document.drawString(72, 760, native)
    document.showPage()
    document.drawImage(ImageReader(str(image_path)), 45, 70, width=500, height=700)
    document.showPage()
    document.save()
    return [native, OCR_SCAN_TEXT]


def create_rotated(path: Path) -> list[str]:
    text = "Rotated fixture: page association must remain page one."
    document = new_canvas(path)
    document.setPageRotation(90)
    document.setFont("Helvetica", 12)
    document.drawString(72, 760, text)
    document.showPage()
    document.save()
    return [text]


def create_multilingual(path: Path, font_path: Path) -> list[str]:
    english = "English heading. "
    hindi = "हिंदी अनुच्छेद: दस्तावेज़ खोज के लिए परीक्षण।"
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))
    document = new_canvas(path)
    document.setFont("Helvetica", 14)
    document.drawString(72, 760, english)
    document.setFont(FONT_NAME, 14)
    document.drawString(72 + document.stringWidth(english, "Helvetica", 14), 760, hindi)
    document.showPage()
    document.save()
    return [english + hindi]


def create_encrypted(path: Path) -> None:
    encryption = StandardEncryption(
        userPassword="benchmark-only", ownerPassword="benchmark-owner"
    )
    document = new_canvas(path, encryption=encryption)
    document.setFont("Helvetica", 12)
    document.drawString(72, 760, "This encrypted fixture must be rejected.")
    document.showPage()
    document.save()


def create_limit_breach(path: Path) -> list[str]:
    document = new_canvas(path)
    pages: list[str] = []
    for page_number in range(20):
        text = f"Limit fixture page {page_number + 1}: bounded resource testing."
        pages.append(text)
        document.setFont("Helvetica", 12)
        document.drawString(72, 760, text)
        document.showPage()
    document.save()
    return pages


def create_suspicious_action(path: Path) -> None:
    document = new_canvas(path)
    document.setFont("Helvetica", 12)
    document.drawString(72, 760, "Fixture for active-content inspection policy.")
    document.showPage()
    document.save()
    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": path.stem, "/Creator": "RAG API"})
    writer._root_object[NameObject("/OpenAction")] = DictionaryObject(
        {
            NameObject("/S"): NameObject("/JavaScript"),
            NameObject("/JS"): TextStringObject("app.alert('fixture-only');"),
        }
    )
    with path.open("wb") as output:
        writer.write(output)


def file_metadata(path: Path) -> dict[str, int | str]:
    content = path.read_bytes()
    return {
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "bytes": len(content),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font-path", type=Path, required=True)
    args = parser.parse_args()
    if not args.font_path.is_file():
        raise SystemExit(f"Font file not found: {args.font_path}")

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    scan_image = FIXTURES_DIR / "scan-source.png"
    create_scan_image(scan_image, args.font_path)

    generated: dict[str, list[str | None] | None] = {
        "native-simple-en-001": create_native_simple(
            FIXTURES_DIR / "native-simple-en-001.pdf"
        ),
        "native-multicolumn-en-001": create_multicolumn(
            FIXTURES_DIR / "native-multicolumn-en-001.pdf"
        ),
        "table-form-en-001": create_table(FIXTURES_DIR / "table-form-en-001.pdf"),
        "scan-en-001": create_scan(FIXTURES_DIR / "scan-en-001.pdf", scan_image),
        "mixed-en-001": create_mixed(FIXTURES_DIR / "mixed-en-001.pdf", scan_image),
        "rotated-en-001": create_rotated(FIXTURES_DIR / "rotated-en-001.pdf"),
        "native-multilingual-001": create_multilingual(
            FIXTURES_DIR / "native-multilingual-001.pdf", args.font_path
        ),
        "limit-breach-001": create_limit_breach(FIXTURES_DIR / "limit-breach-001.pdf"),
    }
    create_encrypted(FIXTURES_DIR / "encrypted-001.pdf")
    create_suspicious_action(FIXTURES_DIR / "suspicious-active-content-001.pdf")
    native_source = FIXTURES_DIR / "native-simple-en-001.pdf"
    corrupt = FIXTURES_DIR / "corrupt-001.pdf"
    corrupt.write_bytes(native_source.read_bytes()[:80])
    duplicate = FIXTURES_DIR / "duplicate-native-simple-en-001.pdf"
    duplicate.write_bytes(native_source.read_bytes())

    inventory: dict[str, object] = {"generator": "synthetic-v1", "fixtures": {}}
    for name, golden_pages in generated.items():
        write_golden(name, golden_pages)
    for path in sorted(FIXTURES_DIR.glob("*.pdf")):
        inventory["fixtures"][path.stem] = file_metadata(path)
    (FIXTURES_DIR / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
