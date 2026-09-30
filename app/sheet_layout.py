"""
Single source of truth for the geometry of a bubble sheet.

The PDF generator draws everything at the positions computed here, and the
scanner looks for bubbles at exactly the same positions after straightening
the scanned page. Keeping both sides on one layout function is what makes
the sheets reliably machine-readable.

Coordinates are in PDF points (1/72 inch) on a US Letter page, with the
origin at the TOP-LEFT corner and y increasing downwards (the same way the
scanner sees an image). The generator converts to ReportLab's bottom-left
origin when drawing.
"""

import json
import math
import secrets
from dataclasses import dataclass, field

# ---------------------------------------------------------------- page ----
PAGE_W = 612.0  # 8.5 in
PAGE_H = 792.0  # 11 in
INCH = 72.0

# Solid black squares in each corner, used to find and straighten the page.
FIDUCIAL_SIZE = 0.3 * INCH
FIDUCIAL_INSET = 0.45 * INCH  # distance from page edge to square centre
FIDUCIAL_CENTERS = [
    (FIDUCIAL_INSET, FIDUCIAL_INSET),                    # top-left
    (PAGE_W - FIDUCIAL_INSET, FIDUCIAL_INSET),           # top-right
    (PAGE_W - FIDUCIAL_INSET, PAGE_H - FIDUCIAL_INSET),  # bottom-right
    (FIDUCIAL_INSET, PAGE_H - FIDUCIAL_INSET),           # bottom-left
]

# Printable content area (inside the corner squares).
CONTENT_LEFT = 0.75 * INCH
CONTENT_RIGHT = PAGE_W - 0.75 * INCH
CONTENT_W = CONTENT_RIGHT - CONTENT_LEFT

# QR code in the top-right of the header: (x, y, size).
QR_BOX = (CONTENT_RIGHT - 1.0 * INCH, 0.64 * INCH, 1.0 * INCH)

# Vertical extent of the answer grid / written-response area.
BODY_TOP = 2.05 * INCH
BODY_BOTTOM = PAGE_H - 0.7 * INCH

# ------------------------------------------------------------- limits ----
MAX_QUESTIONS = 100
MAX_CHOICES = 10
MIN_CHOICES = 2
MAX_WRITTEN = 20
MIN_WRITTEN_HEIGHT_IN = 0.5
CHOICE_LETTERS = "ABCDEFGHIJ"

# Grid tuning.
COLUMN_HEADER_H = 16.0   # row of "A B C D" letters above each column
MAX_ROW_PITCH = 18.0
QUESTION_LABEL_W = 30.0
MAX_BUBBLE_PITCH = 22.0
WRITTEN_LABEL_H = 14.0
WRITTEN_GAP = 10.0


class LayoutError(ValueError):
    """Raised when an exam specification is invalid."""


@dataclass(frozen=True)
class ExamSpec:
    """Everything needed to (re)build the layout of a sheet.

    All of this is encoded in each sheet's QR code, so the grader can read
    any sheet without the server remembering anything about the exam.
    """

    class_name: str
    exam_name: str
    num_questions: int
    num_choices: int
    written_heights: tuple[float, ...] = ()  # height of each box, inches
    exam_id: str = field(default_factory=lambda: secrets.token_hex(4))

    def validate(self) -> None:
        if not self.class_name.strip():
            raise LayoutError("Please enter a class name.")
        if not self.exam_name.strip():
            raise LayoutError("Please enter an exam name.")
        if len(self.class_name) > 60 or len(self.exam_name) > 60:
            raise LayoutError("Class and exam names must be 60 characters or fewer.")
        if not 1 <= self.num_questions <= MAX_QUESTIONS:
            raise LayoutError(
                f"Number of multiple choice questions must be between 1 and {MAX_QUESTIONS}."
            )
        if not MIN_CHOICES <= self.num_choices <= MAX_CHOICES:
            raise LayoutError(
                f"Answer choices per question must be between {MIN_CHOICES} and {MAX_CHOICES}."
            )
        if len(self.written_heights) > MAX_WRITTEN:
            raise LayoutError(f"At most {MAX_WRITTEN} written responses are supported.")
        max_h = max_written_height_in()
        for h in self.written_heights:
            if not MIN_WRITTEN_HEIGHT_IN <= h <= max_h:
                raise LayoutError(
                    f"Written response boxes must be between {MIN_WRITTEN_HEIGHT_IN} "
                    f"and {max_h:.1f} inches tall."
                )


@dataclass(frozen=True)
class SheetIdentity:
    """Who a particular printed sheet belongs to."""

    kind: str          # "s" = student sheet, "k" = answer key sheet
    student_name: str  # "" for the key
    student_index: int  # position in the roster (0 for the key)


# ------------------------------------------------------------ QR codes ----

def encode_qr(spec: ExamSpec, who: SheetIdentity, page: int) -> str:
    """Build the compact JSON string stored in a sheet's QR code."""
    payload = {
        "v": 1,
        "x": spec.exam_id,
        "c": spec.class_name,
        "e": spec.exam_name,
        "q": spec.num_questions,
        "k": spec.num_choices,
        "w": list(spec.written_heights),
        "t": who.kind,
        "n": who.student_name,
        "i": who.student_index,
        "p": page,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def decode_qr(text: str) -> tuple[ExamSpec, SheetIdentity, int]:
    """Parse a QR string back into (spec, identity, page). Raises ValueError."""
    try:
        d = json.loads(text)
        if d.get("v") != 1:
            raise ValueError("unsupported sheet version")
        spec = ExamSpec(
            class_name=str(d["c"]),
            exam_name=str(d["e"]),
            num_questions=int(d["q"]),
            num_choices=int(d["k"]),
            written_heights=tuple(float(h) for h in d.get("w", [])),
            exam_id=str(d["x"]),
        )
        who = SheetIdentity(kind=str(d["t"]), student_name=str(d["n"]), student_index=int(d["i"]))
        page = int(d["p"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("This QR code is not from a BubbleSheetTools sheet.") from exc
    if who.kind not in ("s", "k"):
        raise ValueError("Unknown sheet type in QR code.")
    return spec, who, page


# -------------------------------------------------------------- layout ----

@dataclass(frozen=True)
class Bubble:
    question: int  # 1-based question number
    choice: int    # 0-based choice index (0 = "A")
    x: float       # centre, points from left
    y: float       # centre, points from top
    r: float       # radius, points


@dataclass(frozen=True)
class QuestionLabel:
    question: int
    x: float  # right edge of the label text
    y: float  # vertical centre


@dataclass(frozen=True)
class ColumnHeader:
    letter: str
    x: float
    y: float


@dataclass(frozen=True)
class WrittenBox:
    number: int  # 1-based written response number
    x: float
    y: float     # top edge
    w: float
    h: float


@dataclass
class PageLayout:
    page: int  # 1-based
    bubbles: list[Bubble] = field(default_factory=list)
    labels: list[QuestionLabel] = field(default_factory=list)
    headers: list[ColumnHeader] = field(default_factory=list)
    written: list[WrittenBox] = field(default_factory=list)


def max_written_height_in() -> float:
    """Tallest written box that fits on a page (inches)."""
    return math.floor((BODY_BOTTOM - BODY_TOP - WRITTEN_LABEL_H) / INCH * 10) / 10


def grid_columns(num_questions: int) -> int:
    """Short exams use one column; longer ones are split across two."""
    return 1 if num_questions <= 15 else 2


def build_layout(spec: ExamSpec) -> list[PageLayout]:
    """Compute every page of a sheet: bubble grid first, then written boxes."""
    spec.validate()
    pages = [PageLayout(page=1)]
    cursor_y = BODY_TOP  # next free y position on the current page

    # ---- multiple choice grid (always fits on page 1) ----
    cols = grid_columns(spec.num_questions)
    rows = math.ceil(spec.num_questions / cols)
    col_w = CONTENT_W / cols
    grid_h = BODY_BOTTOM - BODY_TOP - COLUMN_HEADER_H
    row_pitch = min(MAX_ROW_PITCH, grid_h / rows)
    bubble_pitch = min(MAX_BUBBLE_PITCH, (col_w - QUESTION_LABEL_W - 12) / spec.num_choices)
    radius = min(row_pitch, bubble_pitch) * 0.38

    page = pages[0]
    for col in range(cols):
        col_x = CONTENT_LEFT + col * col_w
        first_bubble_x = col_x + QUESTION_LABEL_W + 6 + bubble_pitch / 2
        for c in range(spec.num_choices):
            page.headers.append(
                ColumnHeader(CHOICE_LETTERS[c], first_bubble_x + c * bubble_pitch,
                             BODY_TOP + COLUMN_HEADER_H / 2)
            )
        for row in range(rows):
            q = col * rows + row + 1
            if q > spec.num_questions:
                break
            y = BODY_TOP + COLUMN_HEADER_H + row_pitch * (row + 0.5)
            page.labels.append(QuestionLabel(q, col_x + QUESTION_LABEL_W, y))
            for c in range(spec.num_choices):
                page.bubbles.append(Bubble(q, c, first_bubble_x + c * bubble_pitch, y, radius))
    cursor_y = BODY_TOP + COLUMN_HEADER_H + row_pitch * rows + WRITTEN_GAP * 2

    # ---- written response boxes (flow onto extra pages as needed) ----
    for i, height_in in enumerate(spec.written_heights, start=1):
        box_h = height_in * INCH
        needed = WRITTEN_LABEL_H + box_h
        if cursor_y + needed > BODY_BOTTOM:
            pages.append(PageLayout(page=len(pages) + 1))
            cursor_y = BODY_TOP
        pages[-1].written.append(
            WrittenBox(i, CONTENT_LEFT, cursor_y + WRITTEN_LABEL_H, CONTENT_W, box_h)
        )
        cursor_y += needed + WRITTEN_GAP

    return pages
