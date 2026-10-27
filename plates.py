"""Chuẩn hoá biển số xe Việt Nam (không phụ thuộc OpenCV/ONNX)."""

import re

# Biển VN: 2 số tỉnh + seri (1-2 chữ, có thể kèm 1 số như xe máy "B1") + 4-5 số
HEAD_RE = re.compile(r"^\d{2}[A-Z]{1,2}\d?$")
TO_DIGIT = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"})
# "1" ở vị trí chữ cái thường là "L": chân chữ L trên biển VN rất ngắn nên OCR hay đọc thành số 1
TO_LETTER = str.maketrans({"0": "D", "1": "L", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"})


def plate_key(plate: str) -> str:
    """Biển chỉ còn chữ và số, để so sánh không phụ thuộc dấu gạch / chấm."""
    return re.sub(r"[^A-Z0-9]", "", plate.upper())


def normalize_plate(raw: str) -> str | None:
    """Chuẩn hoá chuỗi OCR thành biển số dạng 30A-123.45, trả None nếu không giống biển VN."""
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not 7 <= len(s) <= 10:
        return None
    # 2 ký tự đầu luôn là số, ký tự thứ 3 luôn là chữ, phần đuôi luôn là số
    s = s[:2].translate(TO_DIGIT) + s[2].translate(TO_LETTER) + s[3:]
    # ưu tiên đuôi 5 số (biển mới), sau đó mới thử 4 số (biển cũ)
    for num_len in (5, 4):
        head, num = s[:-num_len], s[-num_len:].translate(TO_DIGIT)
        if HEAD_RE.match(head) and num.isdigit():
            num = f"{num[:3]}.{num[3:]}" if num_len == 5 else num
            return f"{head}-{num}"
    return None


def plate_tail(plate: str) -> str:
    """Phần số đuôi của biển ("30A-123.45" -> "123.45"), dùng để gom các lần đọc."""
    return plate.split("-", 1)[1]
