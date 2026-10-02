"""Tests for typing/pasting an answer key on the create form (pre-filled key sheets)."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.answer_key import AnswerKeyError, parse_form_keys, parse_key_csv, parse_key_list
from app.grading import GradingSession
from app.main import app
from app.sheet_generator import check_keys_match, generate_sheets_pdf
from app.sheet_layout import ExamSpec

client = TestClient(app)


# ------------------------------------------------------------- parsing ----

def test_one_answer_per_line():
    key = parse_key_list("B\nac\nFR\nD\n")
    assert key.answers == {1: {1}, 2: {0, 2}, 4: {3}}
    assert key.open_questions == {3} and key.num_questions == 4


@pytest.mark.parametrize("text", [
    "1. B\n2) AC\n3 FR\n4: D",
    "Question,Answer\n1,B\n2,\"A,C\"\n3,FR\n4,D",
    "| # | Answer |\n|---|---|\n| 1 | B |\n| 2 | AC |\n| 3 | free response |\n| 4 | D |",
    "1. B\nAC\nfr\nD",          # numbering continues from the last numbered line
])
def test_numbered_csv_and_table_formats(text):
    key = parse_key_list(text)
    assert key.answers == {1: {1}, 2: {0, 2}, 4: {3}} and key.open_questions == {3}


@pytest.mark.parametrize("text,message", [
    ("", "empty"),
    ("FR\nFR", "No multiple choice"),
    ("B\nZ", "not a valid answer letter"),
    ("1. B\n3. C", "skips question"),
    ("1. B\n1. C", "more than once"),
])
def test_bad_pasted_keys(text, message):
    with pytest.raises(AnswerKeyError, match=message):
        parse_key_list(text)


def test_fr_also_works_in_grading_csv_keys():
    assert parse_key_csv("1,A\n2,FR\n").open_questions == {2}


def test_form_keys_per_version_and_csv_columns():
    keys = parse_form_keys(["A\nB\nFR", "C\nD\nFR"])
    assert sorted(keys) == [1, 2] and keys[2].answers[1] == {2}
    keys = parse_form_keys([], "Question,Version 1,Version 2\n1,A,B\n2,FR,FR\n")
    assert sorted(keys) == [1, 2] and keys[1].open_questions == {2}
    with pytest.raises(AnswerKeyError, match="same questions"):
        parse_form_keys(["A\nB\nFR", "C\nFR\nD"])
    assert parse_form_keys(["", "  "]) == {}


def test_keys_must_match_exam_settings():
    keys = {1: parse_key_list("A\nB\nFR\nE")}
    check_keys_match(ExamSpec("B", "U", 4, 5, open_questions=(3,)), keys)
    with pytest.raises(ValueError, match="set to 5"):
        check_keys_match(ExamSpec("B", "U", 5, 5, open_questions=(3,)), keys)
    with pytest.raises(ValueError, match="FR on question"):
        check_keys_match(ExamSpec("B", "U", 4, 5), keys)
    with pytest.raises(ValueError, match="only has 4 answer choices"):
        check_keys_match(ExamSpec("B", "U", 4, 4, open_questions=(3,)), keys)


# ------------------------------------------------ pre-filled key sheets ----

def test_prefilled_key_sheets_grade_from_the_downloaded_pdf():
    keys = parse_form_keys(["1. B\n2. AC\n3. FR\n4. D", "1. C\n2. B\n3. FR\n4. A"])
    spec = ExamSpec("Alg 2", "Unit 3", 4, 4, num_versions=2, open_questions=(3,))
    names = [f"Student {i}" for i in range(30)]
    pdf = generate_sheets_pdf(spec, names, keys)
    assert pymupdf.open(stream=pdf).page_count == 2 + 30

    # Upload the WHOLE downloaded PDF as the key: only the key sheets are used.
    session = GradingSession(user_id="t")
    assert session.set_key_from_upload("sheets.pdf", pdf) == [1, 2]
    assert {v: dict(k.answers) for v, k in session.keys.items()} == \
           {v: dict(k.answers) for v, k in keys.items()}
    assert session.ready


def test_unfilled_version_key_sheet_is_skipped():
    keys = parse_form_keys(["A\nB\nC", ""])  # only version 1's key entered
    spec = ExamSpec("Alg 2", "Unit 3", 3, 4, num_versions=2)
    session = GradingSession(user_id="t")
    assert session.set_key_from_upload("sheets.pdf",
                                       generate_sheets_pdf(spec, ["Ada"], keys)) == [1]
    assert session.missing_versions() == [2]


# ----------------------------------------------------------------- API ----

SHEET = dict(class_name="Alg 2", exam_name="Unit 3", num_questions=4, num_choices=4,
             open_questions="3", names=["Ada"])


def test_api_key_parse_summary():
    r = client.post("/api/key/parse", json={"key_texts": ["1. B\n2. AE\n3. FR\n4. D"]})
    assert r.status_code == 200
    assert r.json() == {"versions": [1], "num_questions": 4, "open_questions": [3],
                        "num_choices_needed": 5, "answers": {"1": ["B", "AE", "FR", "D"]}}
    r = client.post("/api/key/parse", json={"key_texts": ["B\nQ"]})
    assert r.status_code == 400 and "not a valid answer letter" in r.json()["detail"]


def test_api_sheets_with_key():
    r = client.post("/api/sheets", json={**SHEET, "key_texts": ["B\nAC\nFR\nD"]})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    r = client.post("/api/sheets", json={**SHEET, "key_texts": ["B\nAC\nD\nD"]})
    assert r.status_code == 400 and "FR on question" in r.json()["detail"]
