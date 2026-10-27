"""Gửi cảnh báo qua Telegram bot (chỉ dùng thư viện chuẩn)."""

import json
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def telegram_call(token: str, method: str, fields: dict | None = None, photo: Path | None = None, timeout: int = 20):
    url = f"https://api.telegram.org/bot{token}/{method}"
    fields = {k: str(v) for k, v in (fields or {}).items()}
    if photo:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{photo.name}"\r\n'
                     f"Content-Type: image/jpeg\r\n\r\n".encode() + photo.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # Telegram trả lỗi dạng JSON kèm mô tả
        body = json.loads(e.read() or b"{}")
        raise RuntimeError(body.get("description") or str(e)) from None


class Notifier(threading.Thread):
    """Gửi tin ở thread riêng để mạng chậm không làm trễ việc xử lý camera."""

    def __init__(self):
        super().__init__(daemon=True)
        self.queue = queue.Queue(maxsize=50)
        self.settings = {}

    @property
    def configured(self):
        return bool(self.settings.get("telegram_token") and self.settings.get("telegram_chat_id"))

    def send(self, text: str, photo: Path | None = None):
        """Đưa tin vào hàng đợi; trả về False nếu chưa cấu hình Telegram."""
        if not self.configured:
            return False
        try:
            self.queue.put_nowait((self.settings["telegram_token"], self.settings["telegram_chat_id"], text, photo))
        except queue.Full:
            return False
        return True

    def run(self):
        while True:
            token, chat_id, text, photo = self.queue.get()
            try:
                if photo and photo.exists():
                    telegram_call(token, "sendPhoto", {"chat_id": chat_id, "caption": text}, photo)
                else:
                    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
            except Exception as e:
                print(f"[telegram] gửi lỗi: {e}", flush=True)
