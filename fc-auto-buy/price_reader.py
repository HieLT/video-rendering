"""Small-region price reader with Windows OCR and conservative validation."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from windows_ocr import OcrWorker, normalize_price

_worker = None

def _ocr(image: Image.Image) -> str:
    global _worker
    if _worker is None:
        _worker = OcrWorker()
    return _worker.recognize(image)


def read_price(image: Image.Image) -> tuple[str, int | None]:
    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    # The configured box is already the numeric price. One high-contrast
    # pass is substantially faster than invoking Windows OCR three times.
    _, variant = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    prepared = Image.fromarray(cv2.resize(variant, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC))
    text = _ocr(prepared).strip()
    return text, normalize_price(text)
