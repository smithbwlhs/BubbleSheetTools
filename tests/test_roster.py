"""Tests for roster parsing (pasted text and CSV uploads)."""

import pytest

from app.roster import RosterError, parse_csv, parse_pasted


def test_pasted_one_per_line():
    assert parse_pasted("Ada Lovelace\n  Alan   Turing \n\nGrace Hopper\n") == [
        "Ada Lovelace", "Alan Turing", "Grace Hopper"]


def test_pasted_comma_separated_single_line():
    assert parse_pasted("Ada, Alan ,Grace,") == ["Ada", "Alan", "Grace"]


def test_pasted_lines_keep_commas():
    # "Last, First" style lines stay intact when names are on separate lines.
    assert parse_pasted("Lovelace, Ada\nTuring, Alan") == ["Lovelace, Ada", "Turing, Alan"]


@pytest.mark.parametrize("text", ["", "   ", "\n\n", ", ,"])
def test_pasted_empty_is_an_error(text):
    with pytest.raises(RosterError):
        parse_pasted(text)


def test_csv_first_last_headers():
    csv = "\ufeffFirst Name,Last Name,Period\nAda,Lovelace,1\nAlan,Turing,2\n"
    assert parse_csv(csv) == ["Ada Lovelace", "Alan Turing"]


def test_csv_name_header():
    assert parse_csv("Student,ID\nAda Lovelace,1\n,2\nGrace Hopper,3") == [
        "Ada Lovelace", "Grace Hopper"]


def test_csv_no_header():
    assert parse_csv("Ada Lovelace\nAlan Turing\n") == ["Ada Lovelace", "Alan Turing"]


def test_csv_empty_is_an_error():
    with pytest.raises(RosterError):
        parse_csv("First,Last\n")
