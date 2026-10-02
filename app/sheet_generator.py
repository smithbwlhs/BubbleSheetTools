"""
Generate printable bubble sheets as a single PDF.

The PDF starts with an ANSWER KEY sheet (the teacher fills it in and scans it
as the key; one per version on multi-version exams), followed by one sheet per
student. Every page carries:
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

from .answer_key import AnswerKey
from .sheet_layout import (
    CONTENT_LEFT, CONTENT_RIGHT, FIDUCIAL_CENTERS, FIDUCIAL_SIZE, PAGE_H, PAGE_W, QR_BOX,
    ExamSpec, PageLayout, SheetIdentity, build_layout, encode_qr, format_question_list,
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
                page: PageLayout, total_pages: int, prefilled: bool = False) -> None:
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
        label = f"ANSWER KEY \u2014 Version {who.version}" if who.version else "ANSWER KEY"
        c.drawString(CONTENT_LEFT, _flip(92), label)
        c.setFont("Helvetica", 9)
        if prefilled:
            c.drawString(CONTENT_LEFT, _flip(110),
                         "Filled in from the answer key you entered. Check it before grading.")
            c.drawString(CONTENT_LEFT, _flip(122),
                         "Upload this page (no need to print it) as the key. Keep it from students.")
        else:
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


def _draw_version_row(c: canvas.Canvas, page: PageLayout, filled: int = 0) -> None:
    """The "Version 1 2 3 4" row. On key sheets its own version is pre-filled."""
    if not page.version_bubbles:
        return
    lbl = page.version_label
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 11)
    c.drawRightString(lbl.x, _flip(lbl.y) - 4, "Version")
    c.setLineWidth(1)
    for b in page.version_bubbles:
        c.setStrokeGray(BUBBLE_OUTLINE_GRAY)
        if b.choice + 1 == filled:
            c.setFillGray(0)
            c.circle(b.x, _flip(b.y), b.r, stroke=1, fill=1)
            continue
        c.circle(b.x, _flip(b.y), b.r, stroke=1, fill=0)
        c.setFillGray(BUBBLE_LETTER_GRAY)
        c.setFont("Helvetica", b.r * 1.2)
        c.drawCentredString(b.x, _flip(b.y) - b.r * 0.42, str(b.choice + 1))
    last = page.version_bubbles[-1]
    c.setFillGray(0.35)
    c.setFont("Helvetica", 8.5)
    note = ("This sheet is the key for the filled-in version." if filled
            else "Fill in the version number printed on your test.")
    c.drawString(last.x + last.r + 14, _flip(lbl.y) - 3, note)


def _draw_body(c: canvas.Canvas, page: PageLayout, version_filled: int = 0,
               filled: dict[int, frozenset[int]] | None = None) -> None:
    """Bubbles, question numbers, column letters and written response boxes.
    `filled` ({question: choices}) pre-fills bubbles, for answer key sheets."""
    filled = filled or {}
    _draw_version_row(c, page, version_filled)
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
        if b.choice in filled.get(b.question, ()):
            c.setFillGray(0)
            c.circle(b.x, _flip(b.y), b.r, stroke=1, fill=1)
            continue
        c.circle(b.x, _flip(b.y), b.r, stroke=1, fill=0)
        letter_size = b.r * 1.15
        c.setFont("Helvetica", letter_size)
        c.setFillGray(BUBBLE_LETTER_GRAY)
        c.drawCentredString(b.x, _flip(b.y) - letter_size * 0.35, "ABCDEFGHIJ"[b.choice])

    # Open (non-MC) questions: an outlined box where the bubbles would be.
    for box in page.open_boxes:
        c.setStrokeGray(BUBBLE_OUTLINE_GRAY)
        c.setLineWidth(0.8)
        c.roundRect(box.x, _flip(box.y) - box.h / 2, box.w, box.h, box.h / 2, stroke=1, fill=0)
        size = _fit_text(c, "open response", "Helvetica-Oblique", min(box.h * 0.62, 9),
                         box.w - box.h)
        c.setFont("Helvetica-Oblique", size)
        c.setFillGray(0.45)
        c.drawCentredString(box.x + box.w / 2, _flip(box.y) - size * 0.35, "open response")

    c.setFillGray(0)
    c.setStrokeGray(0)
    for w in page.written:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(w.x, _flip(w.y) + 4, f"Written response {w.number}")
        c.setLineWidth(1)
        c.rect(w.x, _flip(w.y + w.h), w.w, w.h, stroke=1, fill=0)


def check_keys_match(spec: ExamSpec, keys: dict[int, AnswerKey]) -> None:
    """Make sure answer keys entered on the form fit the exam settings."""
    if not keys:
        return
    if max(keys) > spec.num_versions:
        raise ValueError(f"There is a key for version {max(keys)} but the exam has "
                         f"{spec.num_versions} version(s).")
    for v, key in keys.items():
        label = f"The version {v} answer key" if spec.num_versions > 1 else "The answer key"
        if key.num_questions != spec.num_questions:
            raise ValueError(f"{label} has {key.num_questions} questions but the exam is set "
                             f"to {spec.num_questions}.")
        if set(key.open_questions) != set(spec.open_questions):
            raise ValueError(f"{label} has FR on question(s) "
                             f"{format_question_list(key.open_questions) or 'none'}, but the "
                             f"exam's open questions are "
                             f"{format_question_list(spec.open_questions) or 'none'}.")
        too_high = [q for q, ch in key.answers.items() if max(ch) >= spec.num_choices]
        if too_high:
            q = too_high[0]
            raise ValueError(f"{label} uses {key.letters(q)} on question {q}, but the sheet "
                             f"only has {spec.num_choices} answer choices.")


def generate_sheets_pdf(spec: ExamSpec, names: list[str],
                        keys: dict[int, AnswerKey] | None = None) -> bytes:
    """Build the full PDF: answer key sheet(s), then every student's sheet.

    With `keys` ({version: AnswerKey}) the key sheets come pre-filled.
    """
    pages = build_layout(spec)  # also validates the spec
    if not names:
        raise ValueError("At least one student name is required.")
    keys = keys or {}
    check_keys_match(spec, keys)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"{spec.class_name} - {spec.exam_name} bubble sheets")
    c.setAuthor("BubbleSheetTools")

    # Answer key sheet(s): only the bubble page is needed. Multi-version
    # exams get one per version, with that version already filled in.
    key_page = PageLayout(page=1, bubbles=pages[0].bubbles, labels=pages[0].labels,
                          headers=pages[0].headers, version_bubbles=pages[0].version_bubbles,
                          version_label=pages[0].version_label, open_boxes=pages[0].open_boxes)
    versions = range(1, spec.num_versions + 1) if spec.num_versions > 1 else [0]
    for version in versions:
        key = SheetIdentity(kind="k", student_name="", student_index=0, version=version)
        answer_key = keys.get(version or 1)
        _draw_frame(c, spec, key, key_page, 1, prefilled=answer_key is not None)
        _draw_body(c, key_page, version_filled=version,
                   filled=answer_key.answers if answer_key else None)
        c.showPage()

    for index, name in enumerate(names, start=1):
        who = SheetIdentity(kind="s", student_name=name, student_index=index)
        for page in pages:
            _draw_frame(c, spec, who, page, len(pages))
            _draw_body(c, page)
            c.showPage()

    c.save()
    return buf.getvalue()
