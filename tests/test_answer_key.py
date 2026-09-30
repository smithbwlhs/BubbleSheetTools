"""Tests for CSV answer key parsing."""

import pytest

from app.answer_key import AnswerKeyError, parse_key_csv


def test_basic_key_with_header():
    key = parse_key_csv("Question,Answer\n1,B\n2,a\n3,D\n")
    assert key.answers == {1: {1}, 2: {0}, 3: {3}}
    assert key.num_questions == 3
    assert key.letters(1) == "B"


@pytest.mark.parametrize("row", ['1,"A,C"', "1,A;C", "1,AC", "1,A C", "1,A,C", "1.,A|C"])
def test_multiple_correct_answers(row):
    assert parse_key_csv(row).answers == {1: {0, 2}}


@pytest.mark.parametrize("text,message", [
    ("", "empty"),
    ("Question,Answer\n", "No answers"),
    ("1,B\n3,C\n", "missing question"),
    ("1,B\n1,C\n", "more than once"),
    ("1,Z\n", "not a valid answer"),
    ("1,\n", "blank"),
    ("1,A\nfoo,B\n", "not a question number"),
])
def test_bad_keys(text, message):
    with pytest.raises(AnswerKeyError, match=message):
        parse_key_csv(text)
