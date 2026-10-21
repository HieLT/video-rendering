"""Windows built-in OCR adapter; avoids an external tesseract.exe install."""
from __future__ import annotations

import asyncio
from io import BytesIO

from PIL import Image
import re
import queue
import threading


async def recognize(image: Image.Image) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR language pack is unavailable")
    buf = BytesIO()
    image.convert("RGBA").save(buf, format="PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return " ".join(line.text for line in result.lines)


class OcrWorker:
    """Persistent Windows OCR engine and event loop for repeated small images."""
    def __init__(self) -> None:
        self._requests: queue.Queue = queue.Queue()
        self._ready_queue: queue.Queue = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready = self._ready_queue.get(timeout=5)
        if isinstance(self._ready, Exception):
            raise self._ready

    def _run(self) -> None:
        from winrt.windows.media.ocr import OcrEngine
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                self._ready_queue.put(RuntimeError("Windows OCR language pack is unavailable"))
                return
            self._ready_queue.put(True)
            while True:
                item = self._requests.get()
                if item is None:
                    return
                image, result_q = item
                try:
                    result_q.put(loop.run_until_complete(_recognize_with_engine(image, engine)))
                except Exception as exc:
                    result_q.put(exc)
        finally:
            loop.close()

    def recognize(self, image: Image.Image, timeout: float = 2.0) -> str:
        result_q: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((image, result_q))
        result = result_q.get(timeout=timeout)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self._requests.put(None)
        self._thread.join(timeout=1)


async def _recognize_with_engine(image: Image.Image, engine) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    buf = BytesIO()
    image.convert("RGBA").save(buf, format="PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return " ".join(line.text for line in result.lines)


def recognize_sync(image: Image.Image) -> str:
    return asyncio.run(recognize(image))


def normalize_price(text: str) -> int | None:
    cleaned = re.sub(r"[^0-9BMK.,]", "", text.upper())
    if not cleaned:
        return None
    suffix = cleaned[-1] if cleaned[-1] in "BMK" else ""
    number = cleaned[:-1] if suffix else cleaned
    try:
        value = float(number if suffix else number.replace(".", "").replace(",", ""))
    except ValueError:
        return None
    return round(value * {"B": 1_000_000_000, "M": 1_000_000, "K": 1_000}.get(suffix, 1))
