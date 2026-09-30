"""Decode a sheet's QR code with OpenCV, trying several image clean-ups.

Scans vary (blur, JPEG artefacts, uneven toner), so if the plain image does
not decode we retry at other scales, sharpened, binarised, and finally with
OpenCV's second (ArUco-based) QR detector, which succeeds on some images the
default detector misses.
"""

import cv2
import numpy as np

_detectors = [cv2.QRCodeDetector()]
if hasattr(cv2, "QRCodeDetectorAruco"):
    _detectors.append(cv2.QRCodeDetectorAruco())


def _variants(img: np.ndarray):
    """Yield progressively more processed versions of the image to try."""
    for scale in (1.0, 1.5, 2.0, 0.75):
        scaled = img if scale == 1.0 else cv2.resize(
            img, None, fx=scale, fy=scale,
            interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
        yield scaled
        blurred = cv2.GaussianBlur(scaled, (0, 0), 1.2)
        yield cv2.addWeighted(scaled, 1.8, blurred, -0.8, 0)  # unsharp mask
        _, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield otsu
    yield cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 51, 10)


def decode_qr_image(img: np.ndarray) -> str | None:
    """Return the QR text found in a grayscale image, or None."""
    if img is None or img.size == 0:
        return None
    for detector in _detectors:
        for attempt in _variants(img):
            try:
                text, _, _ = detector.detectAndDecode(attempt)
            except cv2.error:
                continue
            if text:
                return text
    return None
