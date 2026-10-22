"""Tests for the printable student reports (one page per student)."""

import pymupdf
from fastapi.testclient import TestClient

from app.answer_key import AnswerKey
from app.grading import Flag, GradingSession
from app.main import app
from app.reports import student_reports_pdf
from app.sheet_layout import ExamSpec

KEY = {1: {0}, 2: {1}, 3: {2}, 5: {3}}  # question 4 is open


def session_with_students() -> GradingSession:
    session = GradingSession(user_id="t")
    spec = ExamSpec("Bio P3", "Unit 4", 5, 4, (2.0,), exam_id="ab12cd34", num_versions=2,
                    open_questions=(4,))
    session.spec, session.num_versions, session.exam_ids = spec, 2, {spec.exam_id}
    for v in (1, 2):
        session.keys[v] = AnswerKey({q: frozenset(c) for q, c in KEY.items()},
                                    open_questions=frozenset({4}))
    students = [
        ("Ada", 1, {1: {0}, 2: {1}, 3: {2}, 5: {3}}),      # all correct
        ("Alan", 1, {1: {1}, 2: {1}, 3: set(), 5: {3}}),   # misses 1, blank 3
        ("Grace", None, {1: {0}, 2: {1}, 3: {2}, 5: {3}}),  # version unknown
        ("Mae", 2, None),                                   # bubble page not scanned
    ]
    for i, (name, version, answers) in enumerate(students, start=1):
        s = session._add_student(name=name, class_name="Bio P3", exam_id=spec.exam_id,
                                 roster_index=i, version=version,
                                 written_scores={1: None}, open_scores={4: None})
        if answers is not None:
            s.answers = {q: frozenset(c) for q, c in answers.items()}
            s.pages_seen = {1}
    alan = session.students[2]
    alan.flags[5] = Flag(5, "unclear", frozenset({3}), b"")  # still to be checked
    alan.open_scores[4] = 3.0
    alan.written_scores[1] = 2.5
    session.students[3].flags[0] = Flag(0, "version-blank", frozenset(), b"")
    return session


def page_texts(pdf: bytes) -> list[str]:
    return [page.get_text() for page in pymupdf.open(stream=pdf)]


def test_one_page_per_scanned_student():
    pages = page_texts(student_reports_pdf(session_with_students()))
    assert len(pages) == 3  # Mae's answer sheet was never scanned
    assert ["Ada" in pages[0], "Alan" in pages[1], "Grace" in pages[2]] == [True] * 3


def test_report_contents():
    ada, alan, grace = page_texts(student_reports_pdf(session_with_students()))
    assert "4 / 4" in ada and "100%" in ada and "Version 1" in ada
    assert "2 / 4" in alan and "blank" in alan and "being checked" in alan
    assert "Q4 (open): 3" in alan and "Written 1: 2.5" in alan and "Total: 7.5" in alan
    assert "score 3" in alan  # the open question's row shows the teacher's score
    assert "CORRECT" in alan
    assert "Score pending" in grace and "Version: being checked" in grace


def test_correct_answers_can_be_hidden():
    alan = page_texts(student_reports_pdf(session_with_students(), show_answers=False))[1]
    assert "CORRECT" not in alan and "not shown" in alan
    assert "2 / 4" in alan  # the score is still shown


def test_reports_download():
    client = TestClient(app)
    client.post("/api/grade/start")
    csv = ("Student,Class,Sheet ID,MC correct,Needs review,Notes,Q1,Q2\n"
           "EXAM INFO,exam=Quiz,questions=2,choices=4\n"
           "ANSWER KEY,,,2,,,A,C\n"
           "Ada Lovelace,Chem,0a1b2c3d-01,2,,,A,B\n")
    client.post("/api/grade/files", files={"file": ("results.csv", csv.encode())})
    r = client.get("/api/grade/export/student_reports.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = page_texts(r.content)[0]
    assert "Ada Lovelace" in text and "1 / 2" in text and "CORRECT" in text
    hidden = page_texts(client.get("/api/grade/export/student_reports.pdf?answers=false").content)
    assert "CORRECT" not in hidden[0]
    assert "student_reports.pdf" in client.get("/api/grade/state").json()["exports"]
    client.post("/api/grade/clear")
