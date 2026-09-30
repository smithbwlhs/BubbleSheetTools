"""Decode a sheet's QR code with OpenCV, trying a few image clean-ups."""

import cv2
import numpy as np

_detector = cv2.QRCodeDetector()


def _attempts(img: np.ndarray):
    """Yield progressively more processed versions of the image to try."""
    yield img
    # Upscale small crops so each QR module is several pixels wide.
    if max(img.shape) < 600:
        img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        yield img
    _, otsu = cv2.threshold(cv2.GaussianBlur(img, (3, 3), 0), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu
    yield cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 51, 10)


def decode_qr_image(img: np.ndarray) -> str | None:
    """Return the QR text found in a grayscale image, or None."""
    if img is None or img.size == 0:
        return None
    for attempt in _attempts(img):
        try:
            text, _, _ = _detector.detectAndDecode(attempt)
        except cv2.error:
            continue
        if text:
            return text
    return None
