"""
Student reports: one printable page per student, like the slip a scantron
machine produces.

Each page shows the student's score and every question in order with their
answer, the correct answer (unless the teacher hides answers) and a mark;
missed questions are highlighted, and answers the teacher hasn't checked yet
are marked "being checked". Generated in memory from the grading session.
"""

import io

from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

from .grading import GradingSession, StudentResult, letters
from .sheet_layout import CONTENT_LEFT, CONTENT_W, INCH, PAGE_H, PAGE_W

MISSED_FILL = Color(0.98, 0.87, 0.85)    # light red
CHECKING_FILL = Color(0.99, 0.94, 0.76)  # light yellow
OPEN_FILL = Color(0.94, 0.94, 0.93)      # light gray
RULE = Color(0.80, 0.79, 0.76)
INK_2 = Color(0.35, 0.34, 0.31)
MISSED_INK = Color(0.62, 0.16, 0.12)

CHECK, CROSS = "check", "cross"   # marks are drawn as lines (no symbol font needed)
RIGHT_INK = Color(0.12, 0.36, 0.29)

GRID_TOP = 2.55 * INCH          # below the header and score summary
GRID_BOTTOM = PAGE_H - 0.95 * INCH
MAX_ROW_H = 20.0


def _fmt(score: float | None) -> str:
    if score is None:
        return "—"
    return f"{score:g}"


def _y(top: float) -> float:
    """Top-origin y (like the rest of the app) to ReportLab's bottom origin."""
    return PAGE_H - top


def _fit(c: canvas.Canvas, text: str, font: str, size: float, width: float) -> float:
    while size > 6 and c.stringWidth(text, font, size) > width:
        size -= 0.5
    return size


def _header(c: canvas.Canvas, session: GradingSession, s: StudentResult, show_answers: bool):
    spec = session.spec
    title = f"{s.class_name or spec.class_name}  —  {spec.exam_name}"
    c.setFillGray(0.35)
    c.setFont("Helvetica", _fit(c, title, "Helvetica", 11, CONTENT_W))
    c.drawString(CONTENT_LEFT, _y(0.75 * INCH), title)

    c.setFillGray(0)
    c.setFont("Helvetica-Bold", _fit(c, s.name, "Helvetica-Bold", 22, CONTENT_W * 0.62))
    c.drawString(CONTENT_LEFT, _y(1.12 * INCH), s.name)

    details = []
    if session.num_versions > 1:
        details.append(f"Version {s.version}" if s.version else "Version: being checked")
    if not show_answers:
        details.append("Correct answers are not shown on this report.")
    if details:
        c.setFont("Helvetica", 9.5)
        c.setFillColor(INK_2)
        c.drawString(CONTENT_LEFT, _y(1.36 * INCH), "   ·   ".join(details))


def _score_box(c: canvas.Canvas, session: GradingSession, s: StudentResult):
    """Multiple choice score on the right; hand-scored items underneath."""
    score = session.score(s)
    total_q = len(session.questions)
    right = CONTENT_LEFT + CONTENT_W
    c.setFillGray(0)
    if score is None:
        c.setFont("Helvetica-Bold", 14)
        c.drawRightString(right, _y(1.05 * INCH), "Score pending")
    else:
        c.setFont("Helvetica-Bold", 26)
        c.drawRightString(right, _y(1.12 * INCH), f"{score} / {total_q}")
        c.setFont("Helvetica", 10)
        c.setFillColor(INK_2)
        c.drawRightString(right, _y(1.36 * INCH),
                          f"multiple choice · {100 * score / total_q:.0f}%")

    # Hand-scored parts (open questions, written responses) and the total.
    parts = [(f"Q{q} (open)", s.open_scores.get(q)) for q in session.open_questions]
    parts += [(f"Written {n}", v) for n, v in sorted(s.written_scores.items())]
    if parts:
        entered = [v for _, v in parts if v is not None]
        line = "   ".join(f"{label}: {_fmt(v)}" for label, v in parts)
        if score is not None:
            line += f"      Total: {score + sum(entered):g}"
        c.setFont("Helvetica", 9.5)
        c.setFillGray(0.2)
        size = _fit(c, line, "Helvetica", 9.5, CONTENT_W)
        c.setFont("Helvetica", size)
        c.drawString(CONTENT_LEFT, _y(1.78 * INCH), line)

    c.setStrokeColor(RULE)
    c.setLineWidth(0.8)
    c.line(CONTENT_LEFT, _y(2.05 * INCH), right, _y(2.05 * INCH))


def _question_grid(c: canvas.Canvas, session: GradingSession, s: StudentResult,
                   show_answers: bool):
    """Every question in order, in up to four columns."""
    all_q = list(range(1, session.num_questions + 1))
    cols = 1 if len(all_q) <= 25 else 2 if len(all_q) <= 50 else 3 if len(all_q) <= 75 else 4
    rows = -(-len(all_q) // cols)
    row_h = min(MAX_ROW_H, (GRID_BOTTOM - GRID_TOP - 16) / rows)
    gap = 14
    col_w = (CONTENT_W - gap * (cols - 1)) / cols
    # Column positions inside one column: number, student's answer, key, mark.
    x_num, x_yours = 26, 34
    x_key = x_yours + min(110, (col_w - x_yours - 18) / 2)
    font = 10 if row_h >= 16 else 8.5

    open_qs = set(session.open_questions)
    key = session.key_for(s.version)
    unchecked = {f.question for f in s.open_flags}

    for col in range(cols):
        x0 = CONTENT_LEFT + col * (col_w + gap)
        c.setFont("Helvetica-Bold", 7.5)
        c.setFillColor(INK_2)
        c.drawRightString(x0 + x_num - 4, _y(GRID_TOP - 6), "#")
        c.drawString(x0 + x_yours, _y(GRID_TOP - 6), "YOUR ANSWER" if cols < 3 else "YOURS")
        if show_answers:
            c.drawString(x0 + x_key, _y(GRID_TOP - 6), "CORRECT" if cols < 4 else "KEY")

        for row in range(rows):
            i = col * rows + row
            if i >= len(all_q):
                break
            q = all_q[i]
            top = GRID_TOP + row * row_h
            mid = _y(top + row_h / 2) - font * 0.35

            if q in open_qs:
                # Open questions show the teacher's score instead of answers.
                fill, mark = OPEN_FILL, None
                yours, correct = "open", f"score {_fmt(s.open_scores.get(q))}"
            else:
                given = s.answers.get(q, frozenset())
                yours = letters(given) or "blank"
                correct = letters(key.answers[q]) if key else "?"
                if q in unchecked:
                    fill, mark = CHECKING_FILL, "checking"
                elif key is None:
                    fill, mark = None, None
                elif session.is_correct(q, given, s.version):
                    fill, mark = None, CHECK
                else:
                    fill, mark = MISSED_FILL, CROSS

            if fill is not None:
                c.setFillColor(fill)
                c.rect(x0, _y(top + row_h) + 1, col_w, row_h - 2, stroke=0, fill=1)

            missed = mark == CROSS
            c.setFillColor(MISSED_INK if missed else Color(0, 0, 0))
            c.setFont("Helvetica-Bold", font)
            c.drawRightString(x0 + x_num - 4, mid, f"{q}.")
            c.setFont("Helvetica-Bold" if missed else "Helvetica", font)
            c.drawString(x0 + x_yours, mid, yours)
            if q in open_qs:
                c.setFillColor(INK_2)
                c.setFont("Helvetica", font - 1)
                c.drawString(x0 + x_key, mid, correct)
            elif show_answers:
                c.setFillGray(0)
                c.setFont("Helvetica", font)
                c.drawString(x0 + x_key, mid, correct)

            cx, cy = x0 + col_w - 9, _y(top + row_h / 2)
            if mark in (CHECK, CROSS):
                _draw_mark(c, mark, cx, cy, font * 0.36)
            elif mark == "checking":
                label = "being checked" if cols <= 2 else "?"
                c.setFont("Helvetica-Oblique" if cols <= 2 else "Helvetica-Bold", font - 1.5)
                c.setFillColor(INK_2)
                c.drawRightString(x0 + col_w - 4, mid, label)


def _draw_mark(c: canvas.Canvas, mark: str, cx: float, cy: float, r: float) -> None:
    """A check (correct) or cross (missed) drawn with lines, centred on (cx, cy)."""
    c.setLineWidth(1.4)
    c.setLineCap(1)
    if mark == CHECK:
        c.setStrokeColor(RIGHT_INK)
        path = c.beginPath()
        path.moveTo(cx - r, cy)
        path.lineTo(cx - r * 0.3, cy - r * 0.8)
        path.lineTo(cx + r, cy + r * 0.9)
        c.drawPath(path, stroke=1, fill=0)
    else:
        c.setStrokeColor(MISSED_INK)
        c.line(cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8)
        c.line(cx - r * 0.8, cy + r * 0.8, cx + r * 0.8, cy - r * 0.8)


def _legend(c: canvas.Canvas):
    y = _y(GRID_BOTTOM + 22)
    x = CONTENT_LEFT
    c.setFont("Helvetica", 8.5)
    items = [(MISSED_FILL, "missed"), (CHECKING_FILL, "being checked by your teacher"),
             (OPEN_FILL, "open response (scored by hand)")]
    for fill, label in items:
        c.setFillColor(fill)
        c.rect(x, y - 2, 10, 10, stroke=0, fill=1)
        c.setFillColor(INK_2)
        c.drawString(x + 14, y, label)
        x += 14 + c.stringWidth(label, "Helvetica", 8.5) + 22


def student_reports_pdf(session: GradingSession, show_answers: bool = True) -> bytes:
    """One page per student whose answer sheet was scanned, in class/roster order."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"{session.spec.exam_name} - student reports")
    c.setAuthor("BubbleSheetTools")
    students = [s for s in session.ordered_students() if s.answers is not None]
    for s in students:
        _header(c, session, s, show_answers)
        _score_box(c, session, s)
        _question_grid(c, session, s, show_answers)
        _legend(c)
        c.showPage()
    if not students:
        c.setFont("Helvetica", 12)
        c.drawString(CONTENT_LEFT, _y(INCH), "No student answer sheets have been graded yet.")
        c.showPage()
    c.save()
    return buf.getvalue()
