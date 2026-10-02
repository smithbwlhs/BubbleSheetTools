"""Tests for multi-version exams (up to 4 versions, one answer key each)."""

import io
import json
import zipfile

import cv2
import numpy as np
import pytest

from app.answer_key import AnswerKeyError
from app.export import all_exports_zip, export_files, item_analysis_csv, results_csv
from app.grading import Flag, GradingSession
from app.sheet_generator import generate_sheets_pdf
from app.sheet_layout import (
    ExamSpec, LayoutError, SheetIdentity, build_layout, decode_qr, encode_qr,
)

from .synthetic import distort, fill_bubbles, render_pdf_pages
from .test_pipeline import images_to_pdf

NAMES = ["Ada", "Alan", "Grace", "Katherine", "Mae"]
KEY1 = {q: {q % 4} for q in range(1, 11)}
KEY2 = {q: {(q + 1) % 4} for q in range(1, 11)}
KEY2[3] = {0, 2}  # a multi-answer question on version 2 only


def png(img) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


def letters(ans: dict) -> dict:
    return {q: "".join("ABCD"[c] for c in sorted(v)) for q, v in ans.items()}


# ------------------------------------------------------------- layout ----

def test_version_row_layout_and_qr():
    spec = ExamSpec("Bio", "U4", 100, 5, num_versions=3)
    page = build_layout(spec)[0]
    assert [b.choice for b in page.version_bubbles] == [0, 1, 2]
    # The grid starts below the version row, and still fits 100 questions.
    single = build_layout(ExamSpec("Bio", "U4", 100, 5))[0]
    assert page.bubbles[0].y > single.bubbles[0].y
    assert len(page.bubbles) == 500

    who = SheetIdentity("k", "", 0, version=2)
    spec2, who2, _ = decode_qr(encode_qr(spec, who, 1))
    assert spec2.num_versions == 3 and who2.version == 2


def test_old_qr_codes_still_decode():
    old = json.dumps({"v": 1, "x": "ab12cd34", "c": "Bio", "e": "U4", "q": 5, "k": 4, "w": [],
                      "t": "s", "n": "Ada", "i": 1, "p": 1})
    spec, who, _ = decode_qr(old)
    assert spec.num_versions == 1 and who.version == 0
    assert build_layout(spec)[0].version_bubbles == []


@pytest.mark.parametrize("n", [0, 5])
def test_invalid_version_counts(n):
    with pytest.raises(LayoutError, match="versions"):
        ExamSpec("Bio", "U4", 10, 4, num_versions=n).validate()


def test_pdf_has_one_key_sheet_per_version():
    spec = ExamSpec("Bio", "U4", 10, 4, num_versions=3)
    pages = render_pdf_pages(generate_sheets_pdf(spec, ["Ada", "Alan"]), dpi=72)
    assert len(pages) == 3 + 2


# ------------------------------------------------------------ grading ----

@pytest.fixture(scope="module")
def exam():
    spec = ExamSpec("Bio P3", "Unit 4", 10, 4, num_versions=2)
    pages = render_pdf_pages(generate_sheets_pdf(spec, NAMES))
    rng = np.random.default_rng(11)
    key_pdf = images_to_pdf([fill_bubbles(pages[0], spec, KEY1, rng),
                             fill_bubbles(pages[1], spec, KEY2, rng)])
    return spec, pages, rng, key_pdf


def test_both_key_sheets_in_one_pdf(exam):
    spec, pages, rng, key_pdf = exam
    session = GradingSession(user_id="t")
    assert session.set_key_from_upload("keys.pdf", key_pdf) == [1, 2]
    assert session.num_versions == 2 and session.ready
    assert session.keys[2].answers[3] == frozenset({0, 2})


def test_uploads_wait_for_every_version_key(exam):
    spec, pages, rng, _ = exam
    session = GradingSession(user_id="t")
    session.set_key_from_upload("k1.png", png(fill_bubbles(pages[0], spec, KEY1, rng)))
    assert session.missing_versions() == [2] and not session.ready
    with pytest.raises(AnswerKeyError, match="version 2"):
        session.process_upload("s.png", png(pages[2]), 10)


def test_students_graded_against_their_version(exam):
    spec, pages, rng, key_pdf = exam
    session = GradingSession(user_id="t")
    session.set_key_from_upload("keys.pdf", key_pdf)

    sheets = [
        fill_bubbles(pages[2], spec, KEY1, rng, version=1),         # Ada: perfect on v1
        fill_bubbles(pages[3], spec, KEY2, rng, version=2),         # Alan: perfect on v2
        fill_bubbles(pages[4], spec, KEY1, rng, version=2),         # Grace: v1 answers, bubbled v2
        fill_bubbles(pages[5], spec, KEY2, rng, version=0),         # Katherine: forgot version
        distort(fill_bubbles(pages[6], spec, KEY1, rng, version={1, 2}), rng, angle=2, noise=3),
    ]
    summary = session.process_upload("batch.pdf", images_to_pdf(sheets), 20)
    assert sorted(summary["graded"]) == sorted(NAMES)
    by_name = {s.name: s for s in session.students.values()}

    assert (by_name["Ada"].version, session.score(by_name["Ada"])) == (1, 10)
    assert (by_name["Alan"].version, session.score(by_name["Alan"])) == (2, 10)
    assert by_name["Grace"].version == 2 and session.score(by_name["Grace"]) < 10
    # Alan marked two answers on v2 Q3, which has two correct answers: no flag.
    assert 3 not in by_name["Alan"].flags

    kath, mae = by_name["Katherine"], by_name["Mae"]
    assert kath.version is None and session.score(kath) is None
    assert kath.flags[0].reason == "version-blank" and kath.flags[0].snippet_png
    assert mae.flags[0].reason == "version-multiple"

    session.resolve(kath.id, 0, "2")
    assert kath.version == 2 and session.score(kath) == 10 and kath.flags[0].resolved
    with pytest.raises(ValueError, match="versions"):
        session.resolve(mae.id, 0, "3")


def test_csv_keys_with_version_chosen():
    session = GradingSession(user_id="t")
    session.set_num_versions(2)
    session.set_key_from_upload("v1.csv", b"1,A\n2,B\n", version=1)
    assert session.missing_versions() == [2]
    with pytest.raises(AnswerKeyError, match="no version 3"):
        session.set_key_from_upload("v3.csv", b"1,A\n2,B\n", version=3)
    with pytest.raises(AnswerKeyError, match="All versions must match"):
        session.set_key_from_upload("v2.csv", b"1,A\n2,B\n3,C\n", version=2)
    session.set_key_from_upload("v2.csv", b"1,C\n2,D\n", version=2)
    assert session.ready and session.keys[2].answers[1] == frozenset({2})


def test_csv_with_version_columns_sets_all_versions():
    session = GradingSession(user_id="t")
    assert session.set_key_from_upload(
        "keys.csv", b"Question,Version 1,Version 2,Version 3\n1,A,B,C\n2,D,C,AB\n") == [1, 2, 3]
    assert session.num_versions == 3 and session.ready
    assert session.keys[3].answers[2] == frozenset({0, 1})


# ------------------------------------------------------------ exports ----

def graded_two_version_session() -> GradingSession:
    session = GradingSession(user_id="t")
    session.set_key_from_upload(
        "keys.csv", b"Question,Version 1,Version 2\n1,A,C\n2,B,D\n3,C,AC\n")
    session.spec = ExamSpec("Bio P3", "Unit 4", 3, 4, (2.0,), exam_id="ab12cd34", num_versions=2)
    session.exam_ids = {"ab12cd34"}
    for i, (name, version, ans) in enumerate([
        ("Ada", 1, {1: {0}, 2: {1}, 3: {2}}),
        ("Alan", 2, {1: {2}, 2: {3}, 3: {0, 2}}),
        ("Grace", 2, {1: {0}, 2: {1}, 3: {2}}),
        ("Mae", None, {1: {0}, 2: {1}, 3: {2}}),
    ], start=1):
        s = session._add_student(name=name, class_name="Bio P3", exam_id="ab12cd34",
                                 roster_index=i, version=version, written_scores={1: None})
        s.answers = {q: frozenset(v) for q, v in ans.items()}
        s.pages_seen = {1}
        if version is None:
            s.flags[0] = Flag(0, "version-blank", frozenset(), b"x")
    session.done = True
    return session


def test_exports_are_per_version():
    session = graded_two_version_session()
    names = set(export_files(session))
    assert {"most_missed_v1.png", "most_missed_v2.png", "choice_distribution_v1.png",
            "choice_distribution_v2.png", "score_distribution.png"} <= names
    assert "most_missed.png" not in names

    csv = results_csv(session).decode("utf-8-sig").splitlines()
    assert csv[0].startswith("Student,Class,Version,Sheet ID,")
    assert any(line.startswith("ANSWER KEY V1,") for line in csv)
    assert any(line.startswith("ANSWER KEY V2,") for line in csv)
    mae = next(line for line in csv if line.startswith("Mae,"))
    assert mae.startswith("Mae,Bio P3,?,") and "Version" in mae

    items = item_analysis_csv(session).decode("utf-8-sig").splitlines()
    assert items[0].startswith("Version,Question,")
    v2_q1 = next(line for line in items if line.startswith("2,1,"))
    assert v2_q1.startswith("2,1,C,50.0,1,1,")  # Alan right, Grace wrong; Mae excluded

    z = zipfile.ZipFile(io.BytesIO(all_exports_zip(session)))
    assert "most_missed_v2.png" in z.namelist()


def test_results_csv_round_trip_with_versions():
    original = graded_two_version_session()
    data = results_csv(original)
    restored = GradingSession(user_id="t")
    restored.import_results("results.csv", data)
    assert restored.num_versions == 2
    assert {v: k.answers for v, k in restored.keys.items()} == \
           {v: k.answers for v, k in original.keys.items()}
    by_name = {s.name: s for s in restored.students.values()}
    assert by_name["Alan"].version == 2 and restored.score(by_name["Alan"]) == 3
    assert by_name["Mae"].version is None and by_name["Mae"].flags[0].reason == "version-csv"
    restored.resolve(by_name["Mae"].id, 0, "1")
    assert restored.score(by_name["Mae"]) == 3


def test_mixed_key_files_for_four_versions():
    """A PDF holding two key sheets, a photo of a third, and a CSV for the fourth."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    spec = ExamSpec("Bio P3", "Unit 4", 6, 4, num_versions=4)
    pages = render_pdf_pages(generate_sheets_pdf(spec, ["Ada"]))
    rng = np.random.default_rng(9)
    keys = {v: {q: {(q + v) % 4} for q in range(1, 7)} for v in range(1, 5)}
    pdf_v1_v2 = images_to_pdf([fill_bubbles(pages[0], spec, keys[1], rng),
                               fill_bubbles(pages[1], spec, keys[2], rng)])
    photo_v3 = distort(fill_bubbles(pages[2], spec, keys[3], rng), rng, angle=3, noise=4,
                       jpeg_quality=70)
    csv_v4 = "\n".join(f"{q},{letters(keys[4])[q]}" for q in range(1, 7)).encode()

    client.post("/api/grade/start")
    state = client.post("/api/grade/settings", json={"num_versions": 4}).json()
    assert [k["loaded"] for k in state["keys"]] == [False] * 4
    state = client.post("/api/grade/key", files={"file": ("keys_1_2.pdf", pdf_v1_v2)}).json()
    assert [k["loaded"] for k in state["keys"]] == [True, True, False, False]
    state = client.post("/api/grade/key", files={"file": ("v3.jpg", png(photo_v3))}).json()
    assert [k["loaded"] for k in state["keys"]] == [True, True, True, False]
    assert not state["ready"]
    state = client.post("/api/grade/key", files={"file": ("key.csv", csv_v4)},
                        data={"version": "4"}).json()
    assert state["ready"] and [k["source"] for k in state["keys"]] == \
        ["keys_1_2.pdf", "keys_1_2.pdf", "v3.jpg", "key.csv"]
    client.post("/api/grade/clear")


def test_key_is_for_all_versions_or_one(exam):
    spec, pages, rng, key_pdf = exam  # key_pdf holds the v1 and v2 key sheets

    # "All versions": a one-key CSV can't be placed without a version.
    session = GradingSession(user_id="t")
    session.set_num_versions(2)
    with pytest.raises(AnswerKeyError, match="choose which version"):
        session.set_key_from_upload("key.csv", b"1,A\n2,B\n")

    # Choosing a version takes only that version's sheet from the full PDF.
    assert session.set_key_from_upload("keys.pdf", key_pdf, version=2) == [2]
    assert session.missing_versions() == [1]
    assert session.keys[2].answers[3] == frozenset({0, 2})

    # A chosen version that isn't in the file is reported.
    one_sheet = png(fill_bubbles(pages[0], spec, KEY1, rng))  # version 1's key sheet only
    with pytest.raises(AnswerKeyError, match="no key for version 2"):
        GradingSession(user_id="t").set_key_from_upload("k1.png", one_sheet, version=2)
