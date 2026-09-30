"""
Turn an uploaded file (PDF, JPG, PNG or HEIC) into grayscale page images.

Everything happens in memory; uploaded files are never written to disk.
"""

import io
import logging

import numpy as np
import pymupdf
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# pillow-heif lets Pillow open .heic / .heif photos (iPhones). Its native
# library can be unavailable (e.g. blocked by Windows Application Control on
# a managed laptop), so the app keeps working without it.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # ImportError or a DLL load failure
    HEIC_SUPPORTED = False
    log.warning("HEIC support is unavailable (pillow-heif could not be loaded).")

PDF_RENDER_DPI = 200
MAX_IMAGE_SIDE = 3200  # downscale very large phone photos to save memory/time

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"}


class UploadError(ValueError):
    """Raised when an uploaded file cannot be opened."""


def _extension(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot >= 0 else ""


def _to_gray_array(img: Image.Image) -> np.ndarray:
    """Apply EXIF rotation, flatten transparency, downscale, convert to 8-bit gray."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, "white")
        img = Image.alpha_composite(background, img)
    img = img.convert("L")
    longest = max(img.size)
    if longest > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / longest
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return np.array(img)


def load_pages(filename: str, data: bytes, max_pages: int = 200) -> list[np.ndarray]:
    """Return one grayscale numpy image per page in the uploaded file."""
    if not data:
        raise UploadError(f"{filename} is empty.")
    ext = _extension(filename)

    if ext == ".pdf" or data[:5] == b"%PDF-":
        try:
            doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:  # PyMuPDF raises several exception types
            raise UploadError(f"{filename} could not be opened as a PDF.") from exc
        if doc.page_count == 0:
            raise UploadError(f"{filename} has no pages.")
        if doc.page_count > max_pages:
            raise UploadError(f"{filename} has {doc.page_count} pages; the limit is {max_pages}.")
        pages = []
        for page in doc:
            pix = page.get_pixmap(dpi=PDF_RENDER_DPI, colorspace=pymupdf.csGRAY, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            pages.append(arr.copy())
        doc.close()
        return pages

    if ext in (".heic", ".heif") and not HEIC_SUPPORTED:
        raise UploadError(f"{filename}: HEIC photos are not supported on this server. "
                          "Export the photo as JPG and upload it again.")

    if ext in IMAGE_EXTENSIONS or ext == "":
        try:
            with Image.open(io.BytesIO(data)) as img:
                frames = []
                # Multi-page TIFFs have several frames; ordinary photos have one.
                for i in range(min(getattr(img, "n_frames", 1), max_pages)):
                    img.seek(i)
                    frames.append(_to_gray_array(img.copy()))
                return frames
        except UploadError:
            raise
        except Exception as exc:
            raise UploadError(f"{filename} could not be opened as an image.") from exc

    raise UploadError(f"{filename}: unsupported file type. Upload PDF, JPG, PNG or HEIC files.")
