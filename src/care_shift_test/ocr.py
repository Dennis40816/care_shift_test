from __future__ import annotations
import cv2
import numpy as np
import pytesseract

def read_4digit_from_png_bytes(png_bytes: bytes) -> str:
    # Heuristic pre-processing for 4-digit captcha
    arr = np.frombuffer(png_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    # Binarize and denoise
    img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    img = cv2.medianBlur(img, 3)
    cfg = r"--psm 7 -c tessedit_char_whitelist=0123456789"
    text = pytesseract.image_to_string(img, config=cfg)
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:4] if len(digits) >= 4 else ""
