"""Tests for the shared sheet geometry and QR payload."""

import pytest

from app.sheet_layout import (
    BODY_BOTTOM, CONTENT_LEFT, CONTENT_RIGHT, ExamSpec, LayoutError, SheetIdentity,
    build_layout, decode_qr, encode_qr,
)


def spec(**kw):
    base = dict(class_name="Bio 1", exam_name="Unit 2", num_questions=40, num_choices=4)
    base.update(kw)
    return ExamSpec(**base)


def test_qr_round_trip():
    s = spec(written_heights=(1.5, 2.0))
    who = SheetIdentity("s", "Zoë O'Brien", 7)
    s2, who2, page = decode_qr(encode_qr(s, who, 2))
    assert (s2, who2, page) == (s, who, 2)


def test_bubbles_stay_inside_content_area():
    for n in (1, 15, 16, 55, 100):
        for k in (2, 5, 10):
            page = build_layout(spec(num_questions=n, num_choices=k))[0]
            assert len(page.bubbles) == n * k
            for b in page.bubbles:
                assert CONTENT_LEFT < b.x - b.r and b.x + b.r < CONTENT_RIGHT
                assert b.y + b.r < BODY_BOTTOM


def test_bubbles_do_not_overlap():
    page = build_layout(spec(num_questions=100, num_choices=10))[0]
    by_row = {}
    for b in page.bubbles:
        by_row.setdefault(b.question, []).append(b)
    q1 = sorted(by_row[1], key=lambda b: b.x)
    assert q1[1].x - q1[0].x > 2 * q1[0].r
    assert by_row[2][0].y - by_row[1][0].y > 2 * q1[0].r


def test_written_boxes_flow_to_new_pages():
    pages = build_layout(spec(num_questions=100, written_heights=(3.0, 3.0, 3.0)))
    assert len(pages) >= 2
    boxes = [w for p in pages for w in p.written]
    assert [w.number for w in boxes] == [1, 2, 3]
    for p in pages:
        for w in p.written:
            assert w.y + w.h <= BODY_BOTTOM


def test_short_exam_fits_written_on_page_one():
    pages = build_layout(spec(num_questions=10, written_heights=(2.0,)))
    assert len(pages) == 1 and len(pages[0].written) == 1


@pytest.mark.parametrize("kw", [
    dict(class_name=" "), dict(exam_name=""), dict(num_questions=101),
    dict(num_choices=1), dict(num_choices=11), dict(written_heights=(0.1,)),
    dict(written_heights=(20.0,)), dict(num_questions=0),
])
def test_invalid_specs(kw):
    with pytest.raises(LayoutError):
        spec(**kw).validate()
