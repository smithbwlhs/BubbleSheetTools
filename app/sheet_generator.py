"""
Generate printable bubble sheets as a single PDF.

The PDF starts with an ANSWER KEY sheet (the teacher fills it in and scans it
as the key), followed by one sheet per student. Every page carries:
  * four solid corner squares used by the scanner to straighten the page,
  * a QR code identifying the exam, the student and the page number,
  * the class, exam and student name printed in plain text.

All positions come from sheet_layout.build_layout(), which the scanner also
uses, so what is drawn here is exactly what the scanner expects.
"""

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from .sheet_layout import (
    CONTENT_LEFT, CONTENT_RIGHT, FIDUCIAL_CENTERS, FIDUCIAL_SIZE, PAGE_H, PAGE_W, QR_BOX,
    ExamSpec, PageLayout, SheetIdentity, build_layout, encode_qr,
)

BUBBLE_OUTLINE_GRAY = 0.15  # 0 = black, 1 = white
BUBBLE_LETTER_GRAY = 0.72   # letters inside bubbles are light so they don't read as marks


def _flip(y: float) -> float:
    """Convert a top-origin y coordinate to ReportLab's bottom-origin."""
    return PAGE_H - y


def _qr_image(text: str) -> ImageReader:
    """Render QR code text to an image ReportLab can draw."""
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _fit_text(c: canvas.Canvas, text: str, font: str, size: float, max_w: float) -> float:
    """Shrink the font size until text fits in max_w points."""
    while size > 6 and c.stringWidth(text, font, size) > max_w:
        size -= 0.5
    return size


def _draw_frame(c: canvas.Canvas, spec: ExamSpec, who: SheetIdentity,
                page: PageLayout, total_pages: int) -> None:
    """Corner squares, header text and QR code (common to every page)."""
    # Corner squares.
    c.setFillGray(0)
    half = FIDUCIAL_SIZE / 2
    for x, y in FIDUCIAL_CENTERS:
        c.rect(x - half, _flip(y) - half, FIDUCIAL_SIZE, FIDUCIAL_SIZE, stroke=0, fill=1)

    # QR code.
    qx, qy, qs = QR_BOX
    c.drawImage(_qr_image(encode_qr(spec, who, page.page)), qx, _flip(qy + qs), qs, qs)

    # Header text, left of the QR code.
    text_w = qx - CONTENT_LEFT - 12
    title = f"{spec.class_name}  —  {spec.exam_name}"
    size = _fit_text(c, title, "Helvetica-Bold", 15, text_w)
    c.setFont("Helvetica-Bold", size)
    c.drawString(CONTENT_LEFT, _flip(62), title)

    if who.kind == "k":
        c.setFont("Helvetica-Bold", 20)
        c.drawString(CONTENT_LEFT, _flip(92), "ANSWER KEY")
        c.setFont("Helvetica", 9)
        c.drawString(CONTENT_LEFT, _flip(110),
                     "Fill in every correct answer. Bubble more than one if several are correct.")
        c.drawString(CONTENT_LEFT, _flip(122),
                     "Scan and upload this sheet first. Keep it away from students.")
    else:
        name = f"Name: {who.student_name}"
        c.setFont("Helvetica", _fit_text(c, name, "Helvetica", 13, text_w))
        c.drawString(CONTENT_LEFT, _flip(90), name)
        c.setFont("Helvetica", 9)
        c.drawString(CONTENT_LEFT, _flip(110),
                     "Use a dark pencil. Fill each bubble completely. Erase changes cleanly.")
        c.drawString(CONTENT_LEFT, _flip(122),
                     "Do not write on the corner squares or the QR code.")

    if total_pages > 1:
        c.setFont("Helvetica", 8)
        c.drawRightString(CONTENT_RIGHT, _flip(134), f"Page {page.page} of {total_pages}")


def _draw_body(c: canvas.Canvas, page: PageLayout) -> None:
    """Bubbles, question numbers, column letters and written response boxes."""
    c.setFillGray(0)
    for h in page.headers:
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(h.x, _flip(h.y) - 3, h.letter)

    for lbl in page.labels:
        c.setFont("Helvetica-Bold", 9)
        c.setFillGray(0)
        c.drawRightString(lbl.x, _flip(lbl.y) - 3.2, f"{lbl.question}.")

    c.setLineWidth(0.8)
    for b in page.bubbles:
        c.setStrokeGray(BUBBLE_OUTLINE_GRAY)
        c.circle(b.x, _flip(b.y), b.r, stroke=1, fill=0)
        letter_size = b.r * 1.15
        c.setFont("Helvetica", letter_size)
        c.setFillGray(BUBBLE_LETTER_GRAY)
        c.drawCentredString(b.x, _flip(b.y) - letter_size * 0.35, "ABCDEFGHIJ"[b.choice])

    c.setFillGray(0)
    c.setStrokeGray(0)
    for w in page.written:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(w.x, _flip(w.y) + 4, f"Written response {w.number}")
        c.setLineWidth(1)
        c.rect(w.x, _flip(w.y + w.h), w.w, w.h, stroke=1, fill=0)


def generate_sheets_pdf(spec: ExamSpec, names: list[str]) -> bytes:
    """Build the full PDF: answer key sheet, then every student's sheet."""
    pages = build_layout(spec)  # also validates the spec
    if not names:
        raise ValueError("At least one student name is required.")

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"{spec.class_name} - {spec.exam_name} bubble sheets")
    c.setAuthor("BubbleSheetTools")

    # Answer key: only the bubble page is needed.
    key = SheetIdentity(kind="k", student_name="", student_index=0)
    _draw_frame(c, spec, key, pages[0], 1)
    _draw_body(c, PageLayout(page=1, bubbles=pages[0].bubbles, labels=pages[0].labels,
                             headers=pages[0].headers))
    c.showPage()

    for index, name in enumerate(names, start=1):
        who = SheetIdentity(kind="s", student_name=name, student_index=index)
        for page in pages:
            _draw_frame(c, spec, who, page, len(pages))
            _draw_body(c, page)
            c.showPage()

    c.save()
    return buf.getvalue()
