"""
Find the four corner squares on a scanned/photographed sheet and warp the
page into a straight, standard-size "canonical" image.

Canonical images are PAGE_W x PAGE_H points rendered at CANONICAL_DPI, so a
position from sheet_layout (in points) maps to pixels by multiplying by SCALE.

Page orientation (upright, upside down or sideways) is resolved by trying each
rotation and keeping the one whose QR code decodes in the expected spot.
"""

import cv2
import numpy as np

from ..sheet_layout import FIDUCIAL_CENTERS, FIDUCIAL_SIZE, PAGE_H, PAGE_W, QR_BOX
from .qr import decode_qr_image

CANONICAL_DPI = 200
SCALE = CANONICAL_DPI / 72.0
CANON_W = round(PAGE_W * SCALE)
CANON_H = round(PAGE_H * SCALE)

# Expected width/height ratio of the rectangle joining the square centres.
_EXPECTED_ASPECT = (FIDUCIAL_CENTERS[1][0] - FIDUCIAL_CENTERS[0][0]) / (
    FIDUCIAL_CENTERS[3][1] - FIDUCIAL_CENTERS[0][1]
)


class AlignError(ValueError):
    """Raised when a page cannot be located in an image."""


def normalize_background(gray: np.ndarray, kernel: int) -> np.ndarray:
    """Divide out uneven lighting. Returns float32 where paper ~1.0, ink ~0.0.

    A large grey-level dilation estimates the local paper brightness (it
    "paints over" anything darker and smaller than the kernel).
    """
    kernel = max(3, kernel | 1)
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    k = max(3, (kernel // 4) | 1)
    bg = cv2.dilate(small, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    bg = cv2.GaussianBlur(bg, (k, k), 0)
    bg = cv2.resize(bg, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    return np.clip(gray.astype(np.float32) / np.maximum(bg.astype(np.float32), 1.0), 0, 1)


def _find_square_candidates(gray: np.ndarray) -> list[tuple[float, float, float]]:
    """Return (cx, cy, side) for solid dark squares that could be corner marks."""
    h, w = gray.shape
    longest = max(h, w)
    norm = normalize_background(gray, kernel=longest // 12)
    binary = (norm < 0.55).astype(np.uint8) * 255
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_side, max_side = longest * 0.008, longest * 0.08
    found = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_side ** 2 or area > max_side ** 2:
            continue
        rect = cv2.minAreaRect(cnt)
        (cx, cy), (rw, rh), _ = rect
        if rw == 0 or rh == 0 or not 0.7 < rw / rh < 1.4:
            continue
        if area / (rw * rh) < 0.85:  # square-shaped outline (circles are ~0.785)
            continue
        # Must be solid ink inside, which rules out QR codes and hollow boxes.
        mask = np.zeros_like(binary)
        cv2.drawContours(mask, [cnt], -1, 255, thickness=cv2.FILLED)
        if cv2.mean(binary, mask=mask)[0] / 255 < 0.85:
            continue
        found.append((cx, cy, (rw + rh) / 2))
    return found


def _pick_corners(cands: list[tuple[float, float, float]]) -> np.ndarray:
    """Choose the four outermost squares (TL, TR, BR, BL) from the candidates."""
    if len(cands) < 4:
        raise AlignError("Could not find the four black corner squares.")
    # Squares on one sheet are all printed the same size; use the median to
    # ignore stray dark blobs of very different size.
    med = float(np.median([c[2] for c in cands]))
    pts = np.array([(x, y) for x, y, s in cands if 0.6 * med < s < 1.6 * med], dtype=np.float32)
    if len(pts) < 4:
        raise AlignError("Could not find the four black corner squares.")
    s, d = pts.sum(axis=1), pts[:, 0] - pts[:, 1]
    corners = np.array([pts[s.argmin()], pts[d.argmax()], pts[s.argmax()], pts[d.argmin()]])
    if len({tuple(p) for p in corners}) < 4:
        raise AlignError("Could not find the four black corner squares.")
    return corners


def _warp(gray: np.ndarray, corners: np.ndarray) -> np.ndarray:
    dst = np.array([(x * SCALE, y * SCALE) for x, y in FIDUCIAL_CENTERS], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    return cv2.warpPerspective(gray, matrix, (CANON_W, CANON_H), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def _qr_crop(canon: np.ndarray) -> np.ndarray:
    """The QR code area of a canonical page, with some margin."""
    qx, qy, qs = (v * SCALE for v in QR_BOX)
    m = qs * 0.25
    return canon[int(qy - m):int(qy + qs + m), int(qx - m):int(qx + qs + m)]


def _side(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.hypot(*(a - b)))


def locate_page(gray: np.ndarray) -> tuple[np.ndarray, str]:
    """Find, straighten and orient a sheet. Returns (canonical image, QR text)."""
    corners = _pick_corners(_find_square_candidates(gray))

    # Try the 4 possible orientations, most plausible (by shape) first.
    orders = []
    for rot in range(4):
        c = np.roll(corners, -rot, axis=0)
        width = (_side(c[0], c[1]) + _side(c[3], c[2])) / 2
        height = (_side(c[0], c[3]) + _side(c[1], c[2])) / 2
        aspect_error = abs(np.log((width / max(height, 1)) / _EXPECTED_ASPECT))
        orders.append((aspect_error, rot, c))
    orders.sort(key=lambda o: o[0])
    if orders[0][0] > 0.35:
        raise AlignError("The corner squares found do not match the shape of a sheet.")

    for aspect_error, _, c in orders:
        if aspect_error > 0.35:
            break
        canon = _warp(gray, c)
        text = decode_qr_image(_qr_crop(canon))
        if text:
            return canon, text
    raise AlignError("The page was found but its QR code could not be read.")


def fiducial_check(canon: np.ndarray) -> bool:
    """Sanity check: are the corner squares dark where they should be?"""
    half = FIDUCIAL_SIZE * SCALE * 0.3
    for x, y in FIDUCIAL_CENTERS:
        cx, cy = x * SCALE, y * SCALE
        patch = canon[int(cy - half):int(cy + half), int(cx - half):int(cx + half)]
        if patch.size == 0 or patch.mean() > 110:
            return False
    return True
