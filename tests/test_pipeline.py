"""
End-to-end test: generate sheets -> fill them in -> "scan" -> grade.

Uses synthetic scans (see synthetic.py) so it runs without a printer.
"""

import cv2
import numpy as np
import pymupdf
import pytest

from app.answer_key import AnswerKeyError
from app.grading import GradingSession
from app.sheet_generator import generate_sheets_pdf
from app.sheet_layout import ExamSpec

from .synthetic import distort, fill_bubbles, random_answers, render_pdf_pages

NAMES = ["Ada Lovelace", "Alan Turing", "Grace Hopper", "Katherine Johnson", "Zoë O'Brien"]


def png(img: np.ndarray) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


def jpg(img: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def images_to_pdf(images: list[np.ndarray]) -> bytes:
    """Bundle page images into one PDF, like a batch from a copier scanner."""
    doc = pymupdf.open()
    for img in images:
        page = doc.new_page(width=612, height=792)
        page.insert_image(page.rect, stream=png(img))
    return doc.tobytes()


@pytest.fixture(scope="module")
def exam():
    spec = ExamSpec("Bio 1", "Unit 3 Test", 20, 4, (2.0, 7.0))  # 2nd box forces page 2
    pages = render_pdf_pages(generate_sheets_pdf(spec, NAMES))
    rng = np.random.default_rng(42)
    key = random_answers(spec, rng)
    key[5] = {0, 2}  # a question with two correct answers
    # pages: [key, s1p1, s1p2, s2p1, s2p2, ...]
    return spec, pages, key, rng


def make_session(exam) -> GradingSession:
    spec, pages, key, rng = exam
    session = GradingSession(user_id="t")
    key_img = distort(fill_bubbles(pages[0], spec, key, rng), rng, angle=1.5, noise=3)
    session.set_key_from_upload("key.jpg", jpg(key_img))
    return session


def test_key_sheet_is_read(exam):
    spec, _, key, _ = exam
    session = make_session(exam)
    assert session.keys[1].answers == {q: frozenset(v) for q, v in key.items()}
    assert session.spec.exam_id == spec.exam_id


def test_uploads_blocked_without_key():
    with pytest.raises(AnswerKeyError):
        GradingSession(user_id="t").process_upload("x.pdf", b"%PDF-", 10)


def test_student_sheet_rejected_as_key(exam):
    spec, pages, key, rng = exam
    with pytest.raises(AnswerKeyError, match="not an ANSWER KEY"):
        GradingSession(user_id="t").set_key_from_upload("k.png", png(pages[1]))


def test_blank_key_sheet_rejected(exam):
    _, pages, _, _ = exam
    with pytest.raises(AnswerKeyError, match="is blank"):
        GradingSession(user_id="t").set_key_from_upload("k.png", png(pages[0]))


def test_partly_filled_key_sheet_rejected(exam):
    spec, pages, key, rng = exam
    partial = {q: v for q, v in key.items() if q <= 10}
    with pytest.raises(AnswerKeyError, match="no answer marked for question"):
        GradingSession(user_id="t").set_key_from_upload(
            "k.png", png(fill_bubbles(pages[0], spec, partial, rng)))


def test_full_batch(exam):
    spec, pages, key, rng = exam
    session = make_session(exam)

    student_answers = {}
    scans = []
    for i in range(len(NAMES)):
        ans = random_answers(spec, rng)
        if i == 0:
            ans = {q: set(v) for q, v in key.items()}      # perfect score
        student_answers[i + 1] = ans
        light = {(3, (min(ans[3]) + 1) % 4): 165} if i == 1 else None  # smudge -> flag
        p1 = fill_bubbles(pages[1 + 2 * i], spec, ans, rng, light=light)
        scans.append(distort(p1, rng, angle=rng.uniform(-4, 4), perspective=0.02,
                             upside_down=(i == 2), lighting=0.3, noise=4))
        scans.append(pages[2 + 2 * i])  # written response page
    scans.append(pages[0])                            # key sheet mixed into the batch
    scans.append(np.full((800, 600), 255, np.uint8))  # a blank page

    summary = session.process_upload("batch.pdf", images_to_pdf(scans), max_pages=100)
    assert summary["pages"] == len(scans)
    assert sorted(summary["graded"]) == sorted(NAMES)
    levels = sorted(m["level"] for m in summary["messages"])
    assert levels == ["error", "info"]  # blank page error, key sheet skipped

    for student in session.students.values():
        idx = student.roster_index
        assert student.name == NAMES[idx - 1]
        assert student.pages_seen == {1, 2}
        expected = student_answers[idx]
        for q in range(1, 21):
            if q in student.flags:
                continue
            assert student.answers[q] == frozenset(expected[q]), (student.name, q)

    by_roster = {s.roster_index: s for s in session.students.values()}
    assert session.score(by_roster[1]) == 20

    # The smudged question for student 2 is flagged; resolving it fixes the answer.
    s2 = by_roster[2]
    assert 3 in s2.flags and s2.flags[3].reason in ("unclear", "multiple")
    assert s2.flags[3].snippet_png.startswith(b"\x89PNG")
    session.resolve(s2.id, 3, "".join("ABCD"[c] for c in student_answers[2][3]))
    assert s2.answers[3] == frozenset(student_answers[2][3]) and not s2.open_flags


def test_multi_answer_scoring_modes(exam):
    session = make_session(exam)
    session.spec = exam[0]
    from app.grading import StudentResult
    s = StudentResult(1, "x", answers={q: v for q, v in session.keys[1].answers.items()})
    s.answers[5] = frozenset({0})  # only one of the two correct answers
    assert session.score(s) == 19
    session.set_multi_mode("any")
    assert session.score(s) == 20
    s.answers[5] = frozenset({0, 1})  # includes a wrong one
    assert session.score(s) == 19


def test_other_exam_rejected(exam):
    session = make_session(exam)
    other = ExamSpec("Bio 1", "Unit 3 Test", 20, 4, (2.0, 7.0))  # new exam id
    page = render_pdf_pages(generate_sheets_pdf(other, ["Someone"]))[1]
    summary = session.process_upload("s.png", png(page), max_pages=10)
    assert "different exam" in summary["messages"][0]["message"]


def test_csv_key_checked_against_sheets(exam):
    spec, pages, _, rng = exam
    session = GradingSession(user_id="t")
    session.set_key_from_upload("key.csv", b"1,A\n2,B\n")
    summary = session.process_upload("s.png", png(pages[1]), max_pages=10)
    assert "has 2 questions but this sheet has 20" in summary["messages"][0]["message"]
