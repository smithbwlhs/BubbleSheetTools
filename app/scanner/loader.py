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


class PageSource:
    """An uploaded file opened for reading ONE PAGE AT A TIME.

    A page image is about 4 MB at scan resolution, so a large PDF must never be
    turned into a list of images all at once: that is what exhausted the
    server's memory. Iterate (or call page(i)) and let each page go when done.

    With first_only, a longer PDF is not an error: only its first `max_pages`
    pages are read (used for answer keys, whose sheets come first).
    """

    def __init__(self, filename: str, data: bytes, max_pages: int = 200,
                 first_only: bool = False):
        if not data:
            raise UploadError(f"{filename} is empty.")
        self.filename = filename
        self._data = data
        self._doc = None
        ext = _extension(filename)

        if ext == ".pdf" or data[:5] == b"%PDF-":
            try:
                self._doc = pymupdf.open(stream=data, filetype="pdf")
            except Exception as exc:  # PyMuPDF raises several exception types
                raise UploadError(f"{filename} could not be opened as a PDF.") from exc
            total = self._doc.page_count
            if total == 0:
                raise UploadError(f"{filename} has no pages.")
        elif ext in (".heic", ".heif") and not HEIC_SUPPORTED:
            raise UploadError(f"{filename}: HEIC photos are not supported on this server. "
                              "Export the photo as JPG and upload it again.")
        elif ext in IMAGE_EXTENSIONS or ext == "":
            try:
                with Image.open(io.BytesIO(data)) as img:
                    # Multi-page TIFFs have several frames; ordinary photos have one.
                    total = getattr(img, "n_frames", 1)
            except Exception as exc:
                raise UploadError(f"{filename} could not be opened as an image.") from exc
        else:
            raise UploadError(f"{filename}: unsupported file type. "
                              "Upload PDF, JPG, PNG or HEIC files.")

        if total > max_pages and not first_only:
            raise UploadError(f"{filename} has {total} pages; the limit is {max_pages}.")
        self.count = min(total, max_pages)

    def page(self, index: int) -> np.ndarray:
        """Render page `index` (0-based) as a grayscale image."""
        if self._doc is not None:
            pix = self._doc[index].get_pixmap(dpi=PDF_RENDER_DPI, colorspace=pymupdf.csGRAY,
                                              alpha=False)
            return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        try:
            with Image.open(io.BytesIO(self._data)) as img:
                img.seek(index)
                return _to_gray_array(img.copy())
        except Exception as exc:
            raise UploadError(f"{self.filename} could not be opened as an image.") from exc

    def __iter__(self):
        for i in range(self.count):
            yield self.page(i)

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_pages(filename: str, data: bytes, max_pages: int = 200,
               first_only: bool = False) -> list[np.ndarray]:
    """Every page as an image, all in memory. Only for small files (a key
    sheet, a few photos); use PageSource for anything that may be large."""
    with PageSource(filename, data, max_pages, first_only) as source:
        return list(source)
