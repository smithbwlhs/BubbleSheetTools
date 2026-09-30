"""
Measure how filled-in each bubble is on a straightened (canonical) page.

For every bubble we take the average darkness inside the circle (ignoring the
printed outline). Darkness is compared against the page's own "empty bubble"
level, so scans that are generally light or dark still read consistently.

Each bubble is classed as:
  * marked   - clearly filled in
  * unclear  - partly dark (light mark, smudge, incomplete erasure)
  * empty
The grader turns these into per-question answers and review flags.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from ..sheet_layout import Bubble, PageLayout
from .align import SCALE, normalize_background

# Scores are darkness above the empty-bubble baseline (0 = paper, 1 = black).
# Thresholds scale with how dark THIS student's marks are, so light pencil
# still reads, while an erasure next to a dark mark is flagged as unclear.
MARKED_FRACTION = 0.55      # >= 55% as dark as a typical mark -> marked
UNCLEAR_FRACTION = 0.25     # >= 25% -> unclear (needs a human look)
MIN_MARKED = 0.20           # absolute floors, so noise is never a mark
MIN_UNCLEAR = 0.12
DEFAULT_MARK_DARKNESS = 0.6  # used when a page has too few marks to measure

INNER_RADIUS_FRACTION = 0.72   # sample inside the printed ring
MAX_ROW_SHIFT_PX = 6           # small per-row search to absorb paper curl


@dataclass(frozen=True)
class QuestionRead:
    question: int
    marked: frozenset[int]    # choice indexes that are clearly filled
    unclear: frozenset[int]   # choice indexes that might be filled
    darkness: tuple[float, ...]  # per-choice score above baseline (for debugging)


def _disk_mean(img: np.ndarray, cx: float, cy: float, r: float) -> float:
    """Mean value of img inside a disk (img is float, paper=1)."""
    x0, x1 = int(cx - r - 1), int(cx + r + 2)
    y0, y1 = int(cy - r - 1), int(cy + r + 2)
    patch = img[max(y0, 0):y1, max(x0, 0):x1]
    if patch.size == 0:
        return 1.0
    yy, xx = np.mgrid[max(y0, 0):max(y0, 0) + patch.shape[0], max(x0, 0):max(x0, 0) + patch.shape[1]]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    return float(patch[mask].mean()) if mask.any() else 1.0


_RING_ANGLES = np.linspace(0, 2 * np.pi, 16, endpoint=False)


def _best_row_offset(img: np.ndarray, bubbles: list[Bubble]) -> tuple[int, int]:
    """Find the small shift that best lines up the printed rings of one row.

    Samples points on every printed ring in the row and picks the (dx, dy)
    offset where those points are darkest in total.
    """
    cx = np.array([b.x * SCALE for b in bubbles])[:, None]
    cy = np.array([b.y * SCALE for b in bubbles])[:, None]
    r = np.array([b.r * SCALE for b in bubbles])[:, None]
    px = np.round(cx + r * np.cos(_RING_ANGLES)).astype(int).ravel()
    py = np.round(cy + r * np.sin(_RING_ANGLES)).astype(int).ravel()
    h, w = img.shape
    m = MAX_ROW_SHIFT_PX

    best, best_score = (0, 0), np.inf  # lowest summed brightness = darkest ring
    for dy in range(-m, m + 1, 2):
        ys = np.clip(py + dy, 0, h - 1)
        for dx in range(-m, m + 1, 2):
            score = img[ys, np.clip(px + dx, 0, w - 1)].sum()
            # Prefer no shift when scores tie (e.g. a perfectly clean scan).
            if score < best_score - 1e-6:
                best, best_score = (dx, dy), score
    return best


def read_bubbles(canon: np.ndarray, layout: PageLayout) -> dict[int, QuestionRead]:
    """Read every question on the page. Returns {question number: QuestionRead}.

    On multi-version exams the version row is included as question 0 (its
    "choices" are versions 1..n), so it is judged with the same thresholds.
    """
    blur = cv2.GaussianBlur(canon, (3, 3), 0)
    norm = normalize_background(blur, kernel=int(40 * SCALE))

    rows: dict[int, list[Bubble]] = {}
    for b in layout.bubbles + layout.version_bubbles:
        rows.setdefault(b.question, []).append(b)

    raw: dict[int, list[float]] = {}
    for q, bubbles in rows.items():
        bubbles.sort(key=lambda b: b.choice)
        dx, dy = _best_row_offset(norm, bubbles)
        raw[q] = [
            1.0 - _disk_mean(norm, b.x * SCALE + dx, b.y * SCALE + dy,
                             b.r * SCALE * INNER_RADIUS_FRACTION)
            for b in bubbles
        ]

    # Baseline = darkness of an empty bubble on THIS page (printed letter,
    # paper tone, scanner noise). Most bubbles are empty, so a low percentile
    # of all values is a robust estimate.
    all_values = np.array([v for vals in raw.values() for v in vals])
    baseline = float(np.percentile(all_values, 20)) if all_values.size else 0.0

    scores = {q: tuple(round(v - baseline, 3) for v in vals) for q, vals in raw.items()}

    # Typical darkness of a real mark on this page: median of each question's
    # darkest bubble, counting only questions that look answered.
    strongest = [max(s) for s in scores.values() if max(s) >= MIN_MARKED]
    typical = float(np.median(strongest)) if len(strongest) >= 3 else DEFAULT_MARK_DARKNESS
    typical = min(max(typical, 0.3), 0.9)
    marked_t = max(MIN_MARKED, MARKED_FRACTION * typical)
    unclear_t = max(MIN_UNCLEAR, UNCLEAR_FRACTION * typical)

    results = {}
    for q, s in scores.items():
        marked = frozenset(i for i, v in enumerate(s) if v >= marked_t)
        unclear = frozenset(i for i, v in enumerate(s) if unclear_t <= v < marked_t)
        results[q] = QuestionRead(q, marked, unclear, s)
    return results


def row_snippet_png(canon: np.ndarray, layout: PageLayout, question: int) -> bytes:
    """Crop one question's row from the page as a PNG, for manual review."""
    if question == 0:  # the version row
        bubbles, label = layout.version_bubbles, layout.version_label
        label = type(label)(0, label.x - 30, label.y)  # include the word "Version"
    else:
        bubbles = [b for b in layout.bubbles if b.question == question]
        label = next(lbl for lbl in layout.labels if lbl.question == question)
    r = max(b.r for b in bubbles)
    x0 = (label.x - 32) * SCALE
    x1 = (max(b.x for b in bubbles) + r * 2.5) * SCALE
    # Bubble radius is 0.38 x the row pitch, so 1.3 r is half a row: just this row.
    y0 = (label.y - r * 1.3) * SCALE
    y1 = (label.y + r * 1.3) * SCALE
    crop = canon[max(int(y0), 0):int(y1), max(int(x0), 0):int(x1)]
    ok, png = cv2.imencode(".png", crop)
    return png.tobytes() if ok else b""
