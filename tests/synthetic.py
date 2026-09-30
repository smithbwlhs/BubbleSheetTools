"""
Helpers that fake a filled-in, scanned bubble sheet.

We render a generated sheet, "pencil in" chosen bubbles, then distort the
image like a phone photo or scanner would (rotation, perspective, uneven
lighting, blur, noise, JPEG compression). This lets the whole pipeline be
tested without printing anything.
"""

import cv2
import numpy as np
import pymupdf

from app.scanner.align import SCALE
from app.sheet_layout import ExamSpec, build_layout


def render_pdf_pages(pdf: bytes, dpi: int = 200) -> list[np.ndarray]:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
    return out


def fill_bubbles(page_img: np.ndarray, spec: ExamSpec, answers: dict[int, set[int]],
                 rng: np.random.Generator, darkness: int = 60, light: dict | None = None,
                 coverage: float = 0.95) -> np.ndarray:
    """Pencil in bubbles on a 200 dpi rendering of page 1.

    answers: {question: {choice, ...}}. light: {(q, choice): gray level} for
    faint marks / smudges drawn instead of a full mark.
    """
    img = page_img.copy()
    s = page_img.shape[1] / 612.0  # pixels per point
    layout = build_layout(spec)[0]
    for b in layout.bubbles:
        level = None
        if b.choice in answers.get(b.question, set()):
            level = darkness
        if light and (b.question, b.choice) in light:
            level = light[(b.question, b.choice)]
        if level is None:
            continue
        cx = b.x * s + rng.normal(0, 0.6)
        cy = b.y * s + rng.normal(0, 0.6)
        r = b.r * s * coverage * rng.uniform(0.9, 1.05)
        axes = (int(r * rng.uniform(0.95, 1.1)), int(r * rng.uniform(0.85, 1.0)))
        overlay = img.copy()
        cv2.ellipse(overlay, (int(cx), int(cy)), axes, rng.uniform(0, 180), 0, 360,
                    int(level + rng.normal(0, 8)), -1, lineType=cv2.LINE_AA)
        img = np.minimum(img, overlay)
    return img


def distort(img: np.ndarray, rng: np.random.Generator, angle: float = 0.0,
            perspective: float = 0.0, upside_down: bool = False, sideways: bool = False,
            lighting: float = 0.0, noise: float = 0.0, blur: bool = False,
            jpeg_quality: int = 0, background: int = 90) -> np.ndarray:
    """Place the page on a background and apply photo-like distortions."""
    h, w = img.shape
    pad = int(0.08 * max(h, w))
    canvas = np.full((h + 2 * pad, w + 2 * pad), background, np.uint8)
    canvas[pad:pad + h, pad:pad + w] = img

    src = np.float32([[pad, pad], [pad + w, pad], [pad + w, pad + h], [pad, pad + h]])
    jitter = rng.uniform(-perspective, perspective, (4, 2)) * w
    dst = src + jitter.astype(np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    canvas = cv2.warpPerspective(canvas, m, (canvas.shape[1], canvas.shape[0]),
                                 borderValue=background)

    if angle:
        center = (canvas.shape[1] / 2, canvas.shape[0] / 2)
        rot = cv2.getRotationMatrix2D(center, angle, 1.0)
        canvas = cv2.warpAffine(canvas, rot, (canvas.shape[1], canvas.shape[0]),
                                borderValue=background)
    if upside_down:
        canvas = cv2.rotate(canvas, cv2.ROTATE_180)
    if sideways:
        canvas = cv2.rotate(canvas, cv2.ROTATE_90_CLOCKWISE)

    out = canvas.astype(np.float32)
    if lighting:
        # Brightness gradient across the page, like a lamp off to one side.
        gx = np.linspace(1 - lighting, 1, out.shape[1])[None, :]
        gy = np.linspace(1, 1 - lighting / 2, out.shape[0])[:, None]
        out *= gx * gy
    if noise:
        out += rng.normal(0, noise, out.shape)
    out = np.clip(out, 0, 255).astype(np.uint8)
    if blur:
        out = cv2.GaussianBlur(out, (5, 5), 0)
    if jpeg_quality:
        _, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        out = cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)
    return out


def random_answers(spec: ExamSpec, rng: np.random.Generator, multi_rate: float = 0.0,
                   blank_rate: float = 0.0) -> dict[int, set[int]]:
    answers = {}
    for q in range(1, spec.num_questions + 1):
        if rng.random() < blank_rate:
            answers[q] = set()
            continue
        chosen = {int(rng.integers(spec.num_choices))}
        if rng.random() < multi_rate:
            chosen.add(int(rng.integers(spec.num_choices)))
        answers[q] = chosen
    return answers


