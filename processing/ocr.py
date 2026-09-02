"""Tesseract OCR over a local image file."""
import pytesseract
from PIL import Image


def ocr_image(path: str) -> str:
    try:
        return pytesseract.image_to_string(Image.open(path)).strip()
    except Exception as e:
        return ""
