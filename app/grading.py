"""
Grading: read uploaded sheets, compare them to the key and keep the results
for one teacher's grading session.

Everything here lives in memory only (see sessions.py). Student names come
from the QR codes on the sheets and are discarded when the session ends.
"""

import threading
import time
from dataclasses import dataclass, field

from .answer_key import AnswerKey, AnswerKeyError, parse_key_csv
from .scanner.align import AlignError, locate_page
from .scanner.bubbles import QuestionRead, read_bubbles, row_snippet_png
from .scanner.loader import UploadError, load_pages
from .sheet_layout import CHOICE_LETTERS, ExamSpec, build_layout, decode_qr

MULTI_MODES = ("all", "any")  # how questions with several correct answers are scored


def letters(choices) -> str:
    """{0, 2} -> "AC"."""
    return "".join(CHOICE_LETTERS[c] for c in sorted(choices))


def parse_letters(text: str, num_choices: int) -> frozenset[int]:
    """"ac" -> {0, 2}. Empty string means a blank answer."""
    out = set()
    for ch in (text or "").upper():
        if ch.isspace() or ch == ",":
            continue
        idx = CHOICE_LETTERS.find(ch)
        if idx < 0 or idx >= num_choices:
            raise ValueError(f"'{ch}' is not a valid choice for this exam.")
        out.add(idx)
    return frozenset(out)


# ------------------------------------------------------------ records ----

@dataclass
class Flag:
    """A question that could not be read with confidence."""

    question: int
    reason: str               # "unclear", "multiple" or "blank"
    detected: frozenset[int]  # what the scanner thinks is marked
    snippet_png: bytes        # cropped image of the row, for the teacher
    resolved: bool = False

    def describe(self) -> str:
        return {
            "unclear": "A bubble is faint or partly erased.",
            "multiple": "More than one bubble is filled in.",
            "blank": "No bubble is filled in.",
        }[self.reason]


@dataclass
class StudentResult:
    index: int                                   # roster position from the QR code
    name: str
    answers: dict[int, frozenset[int]] | None = None  # None until page 1 is scanned
    flags: dict[int, Flag] = field(default_factory=dict)
    written_scores: dict[int, float | None] = field(default_factory=dict)
    pages_seen: set[int] = field(default_factory=set)
    rescanned: bool = False

    @property
    def open_flags(self) -> list[Flag]:
        return [f for f in self.flags.values() if not f.resolved]


@dataclass
class PageMessage:
    source: str   # e.g. "scan.pdf, page 3"
    message: str
    level: str = "error"  # "error" or "info"


@dataclass
class GradingSession:
    user_id: str
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    key: AnswerKey | None = None
    key_source: str = ""
    spec: ExamSpec | None = None
    students: dict[int, StudentResult] = field(default_factory=dict)
    messages: list[PageMessage] = field(default_factory=list)
    multi_mode: str = "all"
    done: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------ key ----

    def set_key_from_upload(self, filename: str, data: bytes) -> None:
        """Load the answer key from a CSV file or a scanned key sheet."""
        if not data:
            raise AnswerKeyError("The answer key file is empty.")
        if filename.lower().endswith(".csv") or filename.lower().endswith(".txt"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
            key, spec = parse_key_csv(text), None
        else:
            key, spec = _read_key_sheet(filename, data)

        with self.lock:
            if self.students:
                raise AnswerKeyError(
                    "Sheets have already been graded with the current key. "
                    "Start a new grading session to use a different key.")
            self.key, self.spec, self.key_source = key, spec, filename
            self.touch()

    # --------------------------------------------------------- uploads ----

    def process_upload(self, filename: str, data: bytes, max_pages: int) -> dict:
        """Read every page of an uploaded file. Returns a short summary."""
        if self.key is None:
            raise AnswerKeyError("Upload the answer key before uploading student sheets.")
        try:
            images = load_pages(filename, data, max_pages=max_pages)
        except UploadError as exc:
            msg = PageMessage(filename, str(exc))
            with self.lock:
                self.messages.append(msg)
            return {"file": filename, "pages": 0, "graded": [], "messages": [msg.__dict__]}

        graded, messages = [], []
        for page_no, image in enumerate(images, start=1):
            source = filename if len(images) == 1 else f"{filename}, page {page_no}"
            try:
                name = self._process_page(image, source)
                if name:
                    graded.append(name)
            except PageProblem as exc:
                messages.append(PageMessage(source, str(exc), exc.level))

        with self.lock:
            self.messages.extend(messages)
            self.touch()
        return {"file": filename, "pages": len(images), "graded": graded,
                "messages": [m.__dict__ for m in messages]}

    def _process_page(self, image, source: str) -> str | None:
        """Grade one page image. Returns the student's name if page 1 was read."""
        try:
            canon, qr_text = locate_page(image)
        except AlignError as exc:
            raise PageProblem(
                f"{exc} Make sure the whole sheet, including all four corner squares, "
                "is visible and in focus.") from None
        try:
            spec, who, page = decode_qr(qr_text)
        except ValueError as exc:
            raise PageProblem(str(exc)) from None

        if who.kind == "k":
            raise PageProblem("This is the answer key sheet; it was skipped.", level="info")

        with self.lock:
            self._check_same_exam(spec)
            student = self.students.get(who.student_index)
            if student is None:
                student = StudentResult(index=who.student_index, name=who.student_name)
                student.written_scores = {n: None for n in range(1, len(spec.written_heights) + 1)}
                self.students[who.student_index] = student

            if page != 1:
                student.pages_seen.add(page)
                return None
            if student.answers is not None:
                student.rescanned = True

        # Bubble reading is the slow part; do it outside the lock.
        layout = build_layout(spec)[0]
        reads = read_bubbles(canon, layout)
        answers, flags = {}, {}
        for q, read in reads.items():
            answers[q] = read.marked
            reason = self._flag_reason(q, read)
            if reason:
                flags[q] = Flag(q, reason, read.marked, row_snippet_png(canon, layout, q))

        with self.lock:
            student.answers, student.flags = answers, flags
            student.pages_seen.add(1)
        return student.name

    def _check_same_exam(self, spec: ExamSpec) -> None:
        """Make sure an uploaded sheet belongs to the same exam as the key."""
        if self.spec is None:
            # Key came from a CSV: the first sheet defines the exam, but it
            # must match the key's number of questions and choices.
            if self.key.num_questions != spec.num_questions:
                raise PageProblem(
                    f"The answer key has {self.key.num_questions} questions but this sheet has "
                    f"{spec.num_questions}. Check that the key matches this exam.")
            too_high = [q for q, ch in self.key.answers.items() if max(ch) >= spec.num_choices]
            if too_high:
                raise PageProblem(
                    f"The answer key uses choices this sheet doesn't have (question {too_high[0]}).")
            self.spec = spec
        elif spec.exam_id != self.spec.exam_id:
            raise PageProblem(
                f"This sheet is from a different exam ({spec.class_name} - {spec.exam_name}, "
                "different print batch) and was skipped.")

    def _flag_reason(self, q: int, read: QuestionRead) -> str | None:
        if read.unclear:
            return "unclear"
        if not read.marked:
            return "blank"
        correct = self.key.answers.get(q, frozenset())
        if len(read.marked) > 1 and len(correct) <= 1:
            return "multiple"
        return None

    # --------------------------------------------------------- editing ----

    def resolve(self, student_index: int, question: int, answer: str) -> None:
        """Teacher-confirmed answer for a flagged (or any) question."""
        with self.lock:
            student = self._student(student_index)
            if student.answers is None or question not in student.answers:
                raise ValueError("That question was not found on this student's sheet.")
            student.answers[question] = parse_letters(answer, self.spec.num_choices)
            if question in student.flags:
                student.flags[question].resolved = True
            self.touch()

    def set_written_score(self, student_index: int, number: int, score: float | None) -> None:
        with self.lock:
            student = self._student(student_index)
            if number not in student.written_scores:
                raise ValueError("That written response does not exist on this exam.")
            if score is not None and not 0 <= score <= 1000:
                raise ValueError("Written scores must be between 0 and 1000.")
            student.written_scores[number] = score
            self.touch()

    def set_multi_mode(self, mode: str) -> None:
        if mode not in MULTI_MODES:
            raise ValueError("Unknown scoring mode.")
        with self.lock:
            self.multi_mode = mode
            self.touch()

    def _student(self, index: int) -> StudentResult:
        student = self.students.get(index)
        if student is None:
            raise ValueError("Student not found in this grading session.")
        return student

    def touch(self) -> None:
        self.touched = time.time()

    # --------------------------------------------------------- scoring ----

    def is_correct(self, question: int, given: frozenset[int]) -> bool:
        correct = self.key.answers.get(question, frozenset())
        if len(correct) > 1 and self.multi_mode == "any":
            return bool(given) and given <= correct
        return given == correct

    def score(self, student: StudentResult) -> int:
        if student.answers is None:
            return 0
        return sum(self.is_correct(q, student.answers.get(q, frozenset()))
                   for q in self.key.answers)


class PageProblem(Exception):
    """A single page could not be graded; reported to the teacher."""

    def __init__(self, message: str, level: str = "error"):
        super().__init__(message)
        self.level = level


def _read_key_sheet(filename: str, data: bytes) -> tuple[AnswerKey, ExamSpec]:
    """Read the ANSWER KEY sheet from a scanned PDF or photo."""
    try:
        images = load_pages(filename, data, max_pages=20)
    except UploadError as exc:
        raise AnswerKeyError(str(exc)) from None

    problems = []
    for image in images:
        try:
            canon, qr_text = locate_page(image)
            spec, who, page = decode_qr(qr_text)
        except (AlignError, ValueError) as exc:
            problems.append(str(exc))
            continue
        if who.kind != "k":
            problems.append(f"That is {who.student_name}'s sheet, not the ANSWER KEY sheet.")
            continue

        reads = read_bubbles(canon, build_layout(spec)[0])
        blank = [q for q, r in reads.items() if not r.marked and not r.unclear]
        unclear = [q for q, r in reads.items() if r.unclear]
        if blank or unclear:
            parts = []
            if blank:
                parts.append(f"no answer marked for question(s) {', '.join(map(str, blank[:15]))}")
            if unclear:
                parts.append(f"faint or partly erased marks on question(s) "
                             f"{', '.join(map(str, unclear[:15]))}")
            raise AnswerKeyError(
                "The answer key could not be read reliably: " + "; ".join(parts) +
                ". Darken or clean up those bubbles and re-scan, or upload a CSV key instead.")
        key = AnswerKey(answers={q: r.marked for q, r in reads.items()},
                        num_choices=spec.num_choices, exam_id=spec.exam_id)
        return key, spec

    detail = problems[0] if problems else "No pages found."
    raise AnswerKeyError(f"No answer key sheet could be read from {filename}. {detail}")
