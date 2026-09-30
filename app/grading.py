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
from .results_import import ImportedResults, parse_results_csv
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
    reason: str               # "unclear", "multiple", "blank" or "csv"
    detected: frozenset[int]  # what the scanner thinks is marked
    snippet_png: bytes        # cropped image of the row (empty when restored from a CSV)
    resolved: bool = False

    def describe(self) -> str:
        return {
            "unclear": "A bubble is faint or partly erased.",
            "multiple": "More than one bubble is filled in.",
            "blank": "No bubble is filled in.",
            "csv": "Still marked for checking in the uploaded results CSV "
                   "(no scan image is available; check the paper sheet).",
        }[self.reason]


@dataclass
class StudentResult:
    id: int          # unique within this grading session (what the web page refers to)
    name: str
    class_name: str = ""
    exam_id: str = ""                # print batch, from the QR code
    roster_index: int | None = None  # position in that batch's roster, if known
    answers: dict[int, frozenset[int]] | None = None  # None until page 1 is scanned
    flags: dict[int, Flag] = field(default_factory=dict)
    written_scores: dict[int, float | None] = field(default_factory=dict)
    pages_seen: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)   # e.g. "rescanned", "late scan"

    @property
    def open_flags(self) -> list[Flag]:
        return [f for f in self.flags.values() if not f.resolved]

    @property
    def sheet_id(self) -> str:
        """Batch + roster number, e.g. "3f9c01ab-07" (blank if unknown)."""
        if self.exam_id and self.roster_index is not None:
            return f"{self.exam_id}-{self.roster_index:02d}"
        return ""

    def add_note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)


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
    # Print batches whose sheets belong to this session. Usually one; several
    # when results CSVs from different class periods are combined.
    exam_ids: set[str] = field(default_factory=set)
    students: dict[int, StudentResult] = field(default_factory=dict)
    messages: list[PageMessage] = field(default_factory=list)
    imported_files: list[str] = field(default_factory=list)
    multi_mode: str = "all"
    done: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _next_id: int = 1

    # -------------------------------------------------------- students ----

    def ordered_students(self) -> list[StudentResult]:
        """Students grouped by class, then in roster (or name) order."""
        return sorted(self.students.values(), key=lambda s: (
            s.class_name.lower(), s.exam_id,
            s.roster_index if s.roster_index is not None else 10_000, s.name.lower()))

    def class_names(self) -> list[str]:
        names = sorted({s.class_name for s in self.students.values() if s.class_name})
        return names or ([self.spec.class_name] if self.spec and self.spec.class_name else [])

    def _add_student(self, **fields) -> StudentResult:
        student = StudentResult(id=self._next_id, **fields)
        self.students[student.id] = student
        self._next_id += 1
        return student

    def _find_student(self, exam_id: str, roster_index: int | None,
                      name: str) -> StudentResult | None:
        """Is this student already in the session? Match on sheet ID when both
        sides know it, otherwise on name (older CSVs have no sheet IDs)."""
        for s in self.students.values():
            if s.exam_id and exam_id and s.roster_index is not None and roster_index is not None:
                if s.exam_id == exam_id and s.roster_index == roster_index:
                    return s
            elif s.name.strip().lower() == name.strip().lower():
                return s
        return None
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
            self.exam_ids = {spec.exam_id} if spec else set()
            self.touch()

    # ------------------------------------------------- results imports ----

    def import_results(self, filename: str, data: bytes) -> dict:
        """Load (or merge in) a results CSV previously downloaded from this site."""
        if not data:
            raise AnswerKeyError(f"{filename} is empty.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")  # some Excel "CSV" saves
        imported = parse_results_csv(filename, text)

        with self.lock:
            self._merge_exam(imported, filename)
            added = replaced = 0
            for row in imported.students:
                existing = self._find_student(row.exam_id, row.roster_index, row.name)
                if existing is None:
                    student = self._add_student(name=row.name, class_name=row.class_name,
                                                exam_id=row.exam_id,
                                                roster_index=row.roster_index)
                    added += 1
                else:
                    student = existing
                    student.add_note(f"replaced by {filename}")
                    replaced += 1
                student.answers = dict(row.answers) if row.answers is not None else None
                student.written_scores = dict(row.written)
                student.pages_seen = {1} if row.answers is not None else set()
                student.flags = {
                    q: Flag(q, "csv", student.answers.get(q, frozenset()), b"")
                    for q in row.uncertain
                }
            self.imported_files.append(filename)
            self.done = True
            self.touch()
        return {"file": filename, "added": added, "replaced": replaced,
                "warnings": imported.warnings}

    def _merge_exam(self, imported: ImportedResults, filename: str) -> None:
        """Adopt the imported key/exam, or check it matches the one loaded."""
        if self.key is None:
            self.key = AnswerKey(answers=dict(imported.key), num_choices=imported.num_choices)
            first_class = next((s.class_name for s in imported.students if s.class_name), "")
            self.spec = ExamSpec(
                class_name=first_class, exam_name=imported.exam_name,
                num_questions=imported.num_questions, num_choices=imported.num_choices,
                written_heights=imported.written_heights,
                exam_id=min(imported.exam_ids, default="restored"))
            self.key_source = filename
            self.multi_mode = imported.multi_mode
        else:
            if dict(self.key.answers) != dict(imported.key):
                raise AnswerKeyError(
                    f"{filename} has a different answer key from the results already loaded, "
                    "so it can't be combined with them.")
            if len(self.spec.written_heights) != len(imported.written_heights):
                raise AnswerKeyError(
                    f"{filename} has a different number of written responses from the "
                    "results already loaded.")
            if imported.num_choices > self.spec.num_choices:
                self.spec = ExamSpec(**{**self.spec.__dict__, "num_choices": imported.num_choices})
        self.exam_ids |= imported.exam_ids

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
            student = self._find_student(spec.exam_id, who.student_index, who.student_name)
            if student is None:
                student = self._add_student(
                    name=who.student_name, class_name=spec.class_name, exam_id=spec.exam_id,
                    roster_index=who.student_index,
                    written_scores={n: None for n in range(1, len(spec.written_heights) + 1)})
                if self.imported_files:
                    student.add_note("late scan")
            elif not student.exam_id:
                # Matched by name to a row from an older CSV; remember the sheet ID now.
                student.exam_id, student.roster_index = spec.exam_id, who.student_index

            if page != 1:
                student.pages_seen.add(page)
                return None
            if student.answers is not None:
                student.add_note("rescanned")

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
        if not self.exam_ids:
            # Key came from a CSV (or an older results CSV without sheet IDs):
            # the first sheet defines the print batch, but it must match the
            # key's number of questions and choices.
            if self.key.num_questions != spec.num_questions:
                raise PageProblem(
                    f"The answer key has {self.key.num_questions} questions but this sheet has "
                    f"{spec.num_questions}. Check that the key matches this exam.")
            too_high = [q for q, ch in self.key.answers.items() if max(ch) >= spec.num_choices]
            if too_high:
                raise PageProblem(
                    f"The answer key uses choices this sheet doesn't have (question {too_high[0]}).")
            if self.spec is None:
                self.spec = spec
            self.exam_ids.add(spec.exam_id)
        elif spec.exam_id not in self.exam_ids:
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

    def resolve(self, student_id: int, question: int, answer: str) -> None:
        """Teacher-confirmed answer for a flagged (or any) question."""
        with self.lock:
            student = self._student(student_id)
            if student.answers is None or question not in student.answers:
                raise ValueError("That question was not found on this student's sheet.")
            student.answers[question] = parse_letters(answer, self.spec.num_choices)
            if question in student.flags:
                student.flags[question].resolved = True
            self.touch()

    def set_written_score(self, student_id: int, number: int, score: float | None) -> None:
        with self.lock:
            student = self._student(student_id)
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

    def _student(self, student_id: int) -> StudentResult:
        student = self.students.get(student_id)
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
