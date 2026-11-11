"""Nhờ AI (API kiểu OpenAI, mặc định OpenRouter) xem lại 2 việc model trên máy làm chưa chắc:

1. Gợi ý "có thể là cùng một người" (ReID giống 0,84–0,88): đặt 2 ảnh cạnh nhau hỏi AI. AI chắc là cùng
   người thì tự gộp, chắc là khác người thì bỏ gợi ý, còn lại để người dùng chọn kèm ý kiến của AI.
2. Biển số: AI tự đọc lại ảnh biển. Đọc giống OCR thì thôi; khác mà AI chắc chắn và OCR không chắc thì
   tự sửa, còn lại ghi "AI đọc là …" để người dùng chọn.
3. Biển OCR không đọc được (plate_candidates, xe đi nhanh / biển nhỏ / nghiêng): AI đọc được thì ghi thành
   một lần xuất hiện, kèm cảnh báo Telegram nếu biển nằm trong danh sách theo dõi.

Chạy thành tiến trình riêng (lpr-ai.service) để mạng chậm / API lỗi không làm trễ worker camera.
Chỉ gửi ảnh đã cắt (người, biển số) và ảnh toàn cảnh của biển số, không gửi luồng video.
"""

import base64
import json
import os
import re
import time
from datetime import datetime

import cv2
import httpx
import numpy as np

from alerts import NotifyPolicy, clock
from common import IMAGES_DIR, connect, get_settings, init_db, merge_visitors, notify_matrix
from notify import Notifier
from plates import normalize_plate, plate_key

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
MERGE_CONF = float(os.environ.get("LPR_AI_MERGE_CONF", 0.85))   # AI chắc từ mức này mới tự gộp / bỏ gợi ý
PLATE_CONF = float(os.environ.get("LPR_AI_PLATE_CONF", 0.85))   # AI chắc từ mức này mới tự sửa biển
PLATE_TRUST_OCR = 0.95                                           # OCR chắc từ mức này thì không tự sửa, chỉ ghi ý kiến
CANDIDATE_CONF = 0.6                                             # biển OCR bỏ sót: AI chắc từ mức này mới ghi nhận
BOX_GREEN = 80                                                   # độ xanh tối thiểu của cạnh khung vẽ trên ảnh toàn cảnh
SIGHTING_GAP = float(os.environ.get("LPR_SIGHTING_GAP", 120))    # trùng lần xuất hiện đã có trong khoảng này
BACKFILL = 7 * 86400                                             # xem lại cả dữ liệu cũ trong khoảng này
DEFAULT_MIN_OCR = 0.3  # biển OCR đọc được dưới mức này thường quá mờ, AI đọc cũng không chuẩn: không gửi
# ... trừ khi ảnh biển đủ to: OCR trên máy hay đọc trắng biển xe máy nghiêng 25–40° dù ảnh rất rõ
MIN_PLATE_SIDE = 50
PARKED_WINDOW = 1800   # xe đỗ trước camera: so với các lần đã xem ở đúng chỗ đó trong khoảng này (giây)
PARKED_IOU = 0.6       # khung biển trùng nhau từ mức này
PARKED_SIM = 0.9       # ảnh biển giống nhau từ mức này (tương quan ảnh xám thu nhỏ)
# hai bước dưới chỉ chạy trên các camera này (camera nhìn chéo, xe đi nhanh có thể bị bỏ nhầm xe thật). Để trống = tắt.
TIDY_CAMS = set(filter(None, os.environ.get("LPR_PLATE_TIDY_CAMS", "").split(",")))
SPOT_WINDOW = 86400    # chỗ xe đỗ: xét các lần đã bị coi là xe đỗ trong khoảng này (giây)
SPOT_MIN = 3           # đủ chừng này lần ở cùng chỗ mới coi là chỗ có xe đỗ
SPOT_COVER = 0.7       # khung nhỏ hơn nằm trong khung kia từ mức này là cùng chỗ (AI có thể chỉ thấy một phần biển)
SPOT_TRUST_CONF = 0.85 # AI chắc từ mức này và biển khác hẳn xe đỗ quen thuộc: là xe khác vào đúng chỗ đó
MERGE_WINDOW = 30      # xe chạy bị tách thành 2 lần đi qua: gộp nếu cách nhau trong khoảng này (giây)
MERGE_DIST = 2         # ... và biển chỉ lệch từng này ký tự
# gộp chỉ đụng tới lần xuất hiện của chính xe đó nên bật được cả ở camera chưa dọn chỗ xe đỗ
MERGE_CAMS = TIDY_CAMS | set(filter(None, os.environ.get("LPR_PLATE_MERGE_CAMS", "").split(",")))
# xe quen: chạy thử, chỉ ghi vào plate_suggestions chứ không đổi biển
SUGGEST_BELOW = 0.85   # chỉ xét lần đọc chưa chắc tới mức này
KNOWN_DAYS = 10        # biển đọc chắc trong khoảng này mới tính
KNOWN_CONF = 0.9       # đọc chắc từ mức này
KNOWN_PASSES = 3       # xe quen: đọc chắc từng này lần riêng biệt...
KNOWN_GAP = 600        # ... cách nhau ít nhất chừng này giây
KNOWN_CAMERAS = 2      # ... hoặc từng này camera cùng đọc ra đúng biển đó
SUGGEST_DIST_CAR = 2   # ô tô, xe tải: được lệch từng này ký tự, phải khớp cả hãng và màu
SUGGEST_DIST_BIKE = 1  # xe máy khó phân biệt: chỉ được lệch từng này, màu không được khác hẳn
SUGGESTION_DAYS = 30   # giữ dữ liệu chạy thử trong khoảng này
MODES = ("off", "suggest", "auto")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _error_message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return err.get("message") or r.text[:200]
    except ValueError:
        return r.text[:200]


def chat(base_url: str, key: str, model: str, messages: list, timeout: float = 90) -> tuple[str, int, float]:
    """Gọi /chat/completions, trả về (nội dung trả lời, số token, chi phí USD nếu nhà cung cấp báo)."""
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 2000}
    headers = {"Authorization": f"Bearer {key}"}
    if "openrouter.ai" in base_url:
        # model suy luận: nghĩ ít thôi cho nhanh / rẻ, câu hỏi ở đây không cần suy luận dài
        body["reasoning"] = {"effort": "low"}
        headers["X-Title"] = "Camera AI"
    r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", timeout=timeout, headers=headers, json=body)
    if r.status_code != 200:
        raise ApiError(r.status_code, _error_message(r))
    data = r.json()
    content = data["choices"][0]["message"].get("content") or ""
    if isinstance(content, list):  # một số model trả nội dung dạng nhiều phần
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    usage = data.get("usage") or {}
    return content, int(usage.get("total_tokens") or 0), float(usage.get("cost") or 0)


def list_models(base_url: str, key: str) -> list[dict]:
    """[{id, name, vision (True / False / None = không rõ), price_in, price_out (USD / 1M token)}]."""
    r = httpx.get(f"{base_url.rstrip('/')}/models", timeout=20, headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        raise ApiError(r.status_code, _error_message(r))
    out = []
    for m in r.json().get("data") or []:
        mid = m.get("id")
        if not mid or mid.endswith(":batch"):  # bản chạy theo lô của OpenRouter, không trả lời ngay
            continue
        modalities = (m.get("architecture") or {}).get("input_modalities")
        caps = m.get("capabilities")
        vision = ("image" in modalities) if isinstance(modalities, list) else \
                 ("vision" in caps) if isinstance(caps, list) else None
        pricing = m.get("pricing") or {}
        try:  # OpenRouter: USD / token (chuỗi); kiểu OxionAI: USD / 1k token
            price_in = float(pricing["prompt"]) * 1e6 if "prompt" in pricing else float(pricing["input_per_1k"]) * 1e3
            price_out = float(pricing["completion"]) * 1e6 if "completion" in pricing else float(pricing["output_per_1k"]) * 1e3
        except (KeyError, TypeError, ValueError):
            price_in = price_out = None
        out.append({"id": mid, "name": m.get("name") or mid, "vision": vision, "price_in": price_in, "price_out": price_out})
    return out


def parse_json(text: str) -> dict:
    """Lấy object JSON trong câu trả lời (model hay bọc trong ```json … ``` hoặc kèm lời giải thích)."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"AI không trả JSON: {text[:120]!r}")
    return json.loads(m.group(0))


def image_part(rel: str, min_h: int = 0, min_w: int = 0, max_side: int = 1280) -> dict:
    """Ảnh trong IMAGES_DIR -> phần image_url (data URI). Ảnh cắt nhỏ được phóng to cho dễ nhìn."""
    im = cv2.imread(str(IMAGES_DIR / rel))
    if im is None:
        raise FileNotFoundError(rel)
    h, w = im.shape[:2]
    scale = min(max(1.0, min_h / h, min_w / w), max_side / max(h, w))
    if scale != 1.0:
        im = cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    return array_part(im)


def array_part(im) -> dict:
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf).decode()}}


def plate_surroundings(frame_img: str, bbox: list):
    """Vùng quanh khung xanh trong ảnh toàn cảnh, None nếu không tìm ra khung. Gửi cả khung hình thì AI hay trả về
    biển rõ nhất trong hình (xe đỗ gần camera) thay vì biển mờ trong khung, kể cả khi đã dặn chỉ đọc biển trong khung."""
    im = cv2.imread(str(IMAGES_DIR / frame_img))
    if im is None:
        return None
    # bbox tính theo khung gốc, ảnh toàn cảnh đã thu nhỏ: tìm tỉ lệ khớp với khung xanh đã vẽ
    green = im[..., 1].astype(int) - im[..., [0, 2]].max(axis=2)
    x1, y1, x2, y2 = bbox
    best, scale = BOX_GREEN, None
    for s in np.arange(0.25, 1.0005, 0.001):
        ax, bx = int(x1 * s), int(x2 * s)
        ay, by = int(y1 * s), int(y2 * s)
        if bx > im.shape[1] or by >= im.shape[0] or bx <= ax:
            continue
        top = green[max(0, ay - 1):ay + 2, ax:bx]
        bottom = green[max(0, by - 1):by + 2, ax:bx]
        score = min(top.max(axis=0).mean(), bottom.max(axis=0).mean())
        if score > best:
            best, scale = score, s
    if scale is None:
        return None
    x1, y1, x2, y2 = [int(v * scale) for v in bbox]
    pad = int(max(x2 - x1, y2 - y1) * 2.5)
    return im[max(0, y1 - pad):y2 + pad, max(0, x1 - pad):x2 + pad]


def plate_signature(rel: str):
    """Ảnh biển -> vector xám 64×24 đã chuẩn hoá, để so hai ảnh biển có phải cùng một cảnh không."""
    im = cv2.imread(str(IMAGES_DIR / rel), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return None
    v = cv2.resize(im, (64, 24), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    v -= v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def iou(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def cover(a, b) -> float:
    """Phần trăm khung nhỏ hơn nằm trong khung kia."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))


def clean_vehicle(v):
    """Phần vehicle AI trả về -> {type, brand, color} chữ thường, None nếu không có."""
    if not isinstance(v, dict):
        return None
    return {k: str(v.get(k) or "unknown").strip().lower() for k in ("type", "brand", "color")}


def vehicle_profile(vehicles: list) -> dict:
    """Hồ sơ xe từ các lần đọc chắc: giá trị gặp nhiều nhất của mỗi mục, bỏ qua 'unknown'."""
    out = {}
    for k in ("type", "brand", "color"):
        vals = [v[k] for v in vehicles if v.get(k) and v[k] != "unknown"]
        out[k] = max(set(vals), key=vals.count) if vals else "unknown"
    return out


def suggestion_decision(vehicle, profile, single: bool) -> str:
    """So xe của lần đọc này với hồ sơ xe quen (biển đã đủ gần). 'suggest' là được gợi ý."""
    if not single:
        return "ambiguous"
    kind = (vehicle or {}).get("type", "unknown")
    if kind == "unknown":
        return "vehicle_unknown"
    if profile["type"] != "unknown" and profile["type"] != kind:
        return "vehicle_mismatch"
    color, known_color = vehicle.get("color", "unknown"), profile["color"]
    if kind == "motorbike":
        return "vehicle_mismatch" if "unknown" not in (color, known_color) and color != known_color else "suggest"
    if profile["brand"] == "unknown" or known_color == "unknown":
        return "no_profile"
    if vehicle.get("brand", "unknown") == "unknown" or color == "unknown":
        return "vehicle_unknown"
    return "suggest" if vehicle["brand"] == profile["brand"] and color == known_color else "vehicle_mismatch"


def clamp_conf(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


PERSON_SYSTEM = (
    "You compare two CCTV crops from home security cameras at a house gate in Vietnam and decide whether they show "
    "the same individual. Compare specific details: helmet or hat (shape, colour, visor, stickers), hair, jacket and "
    "shirt (colour, pattern, logos, sleeves), trousers, shoes, bags, face mask, gloves, body build, and the vehicle "
    "(motorbike model, colour, basket, box, plate area). The two crops may come from different cameras, angles and "
    "lighting; night IR footage is grayscale, so ignore colour differences between colour and grayscale crops. Many "
    "riders wear similar dark jackets and helmets: generic similarity is NOT enough for 'yes'. Answer 'yes' only when "
    "at least two distinctive details match and nothing clearly contradicts; answer 'no' when a clear detail differs "
    "(different helmet, different clothing item, different vehicle); otherwise 'unsure'. Reply with JSON only."
)

VEHICLE_FIELD = (
    ', "vehicle": {"type": "car, motorbike, truck, other or unknown", "brand": "make from a visible logo or unmistakable '
    'shape, e.g. Mazda, VinFast, Honda; otherwise unknown", "color": "body colour of the vehicle carrying that plate: '
    'white, black, silver, gray, red, blue, green, teal, yellow, orange, brown, other, or unknown if the image is '
    'grayscale or the body is not visible"}'
)

PLATE_SYSTEM = (
    "You read Vietnamese vehicle licence plates from CCTV images. Format: 2-digit province code, then a series of "
    "1-2 letters that may be followed by one digit (motorbikes, e.g. 'B1', 'AA'), then 4 or 5 digits. Car plates are "
    "usually one row ('30A-123.45'); motorbike plates have two rows (top '29-B1', bottom '123.45'). Read every "
    "character from the image yourself; if a character is not legible, say so with lower confidence instead of "
    "guessing. Reply with JSON only."
)


class Reviewer:
    def __init__(self, conn):
        self.conn = conn
        self.settings = {}
        self.paused_until = 0.0
        self.paused_for = None      # (key, model) lúc tạm dừng; đổi key / model thì chạy lại ngay
        self.paused_plate_model = ""
        self.failures = {}          # (bảng, id) -> số lần gọi lỗi
        self.state = {}
        self.notifier = Notifier()  # cảnh báo biển theo dõi mà chỉ AI đọc được
        self.notifier.start()
        self.policy = NotifyPolicy(self.notifier)

    # ---------- trạng thái hiển thị ở trang Cài đặt ----------
    def _load_state(self):
        try:
            self.state = json.loads(self.settings.get("ai_state") or "{}")
        except ValueError:
            self.state = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("day") != today:
            self.state.update(day=today, tokens=0, calls=0, cost=0.0)

    def _save_state(self, ok: bool | None = None, message: str | None = None, tokens: int = 0, calls: int = 0,
                    cost: float = 0.0):
        self.state["tokens"] = self.state.get("tokens", 0) + tokens
        self.state["cost"] = self.state.get("cost", 0.0) + cost
        self.state["calls"] = self.state.get("calls", 0) + calls
        if ok is not None:
            self.state.update(ok=ok, message=message or "", at=time.time())
        self.conn.execute("INSERT INTO settings(key, value) VALUES ('ai_state', ?)"
                          " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(self.state),))
        self.conn.commit()

    def _pause(self, seconds: float, message: str):
        self.paused_until = time.time() + seconds
        self.paused_for = (self.settings.get("ai_api_key"), self.settings.get("ai_model"))
        self.paused_plate_model = self.settings.get("ai_plate_model", "")
        print(f"[ai] tạm dừng {seconds:.0f}s: {message}", flush=True)
        self._save_state(False, message)

    def _ask(self, system: str, content: list, model: str | None = None) -> dict:
        s = self.settings
        text, tokens, cost = chat(s.get("ai_base_url") or DEFAULT_BASE_URL, s["ai_api_key"], model or s["ai_model"],
                                  [{"role": "system", "content": system}, {"role": "user", "content": content}])
        self._save_state(True, "", tokens=tokens, calls=1, cost=cost)
        return parse_json(text)

    # ---------- vòng chạy ----------
    def step(self) -> bool:
        """Xử lý một việc. Trả về True nếu còn việc để làm ngay."""
        self.settings = get_settings(self.conn)
        s = self.settings
        if not (s.get("ai_api_key") and s.get("ai_model")):
            return False
        visit_mode = s.get("ai_visits", "off")
        plate_mode = s.get("ai_plates", "off")
        read_missed = s.get("ai_read_missed", "1") == "1"   # đọc biển OCR trên máy bỏ sót
        if visit_mode == "off" and plate_mode == "off" and not read_missed:
            return False
        if time.time() < self.paused_until and self.paused_for == (s.get("ai_api_key"), s.get("ai_model")) \
                and s.get("ai_plate_model", "") == self.paused_plate_model:
            return False
        self._load_state()
        now = time.time()
        job = None
        if read_missed:  # biển OCR bỏ sót: có thể cần cảnh báo ngay, làm trước tiên
            min_ocr = float(s.get("ai_min_ocr") or DEFAULT_MIN_OCR)
            self.conn.execute("UPDATE plate_candidates SET status = 'blurry' WHERE status IS NULL AND ts > ?"
                              " AND COALESCE(ocr_score, 0) < ? AND COALESCE(MIN(json_extract(bbox, '$[2]') - json_extract(bbox, '$[0]'),"
                              " json_extract(bbox, '$[3]') - json_extract(bbox, '$[1]')), 0) < ?",
                              (now - BACKFILL, min_ocr, MIN_PLATE_SIDE))
            self.conn.commit()  # luôn commit: UPDATE dù không đổi dòng nào vẫn giữ khoá ghi tới khi commit
            row = self.conn.execute(
                "SELECT * FROM plate_candidates WHERE status IS NULL AND ts > ? ORDER BY ts DESC LIMIT 1",
                (now - BACKFILL,)).fetchone()
            if row:
                job = ("plate_candidates", row, lambda: self.review_candidate(row))
        if job is None and plate_mode != "off":  # biển số ít và cần sớm hơn người: làm trước
            row = self.conn.execute(
                "SELECT id, plate, corrected_plate, ocr_score, plate_img, frame_img, bbox FROM sightings"
                " WHERE ai_status IS NULL AND first_seen < ? AND first_seen > ? ORDER BY first_seen DESC LIMIT 1",
                (now - 45, now - BACKFILL)).fetchone()
            if row:
                job = ("sightings", row, lambda: self.review_plate(row, plate_mode))
        if job is None and visit_mode != "off":
            # chỉ xem khi người đã đi khỏi (lượt đã xong ảnh), mới nhất trước
            row = self.conn.execute(
                "SELECT v.id, v.visitor_id, v.maybe_visitor, v.maybe_sim, v.img, v.embedding, v.ts, v.camera,"
                " c.name AS camera_name FROM visits v LEFT JOIN cameras c ON c.id = v.camera"
                " WHERE v.maybe_visitor IS NOT NULL AND v.ai_verdict IS NULL AND v.last_seen < ? AND v.ts > ?"
                " AND v.maybe_visitor != v.visitor_id AND v.maybe_visitor IN (SELECT id FROM visitors)"
                " ORDER BY v.ts DESC LIMIT 1", (now - 30, now - BACKFILL)).fetchone()
            if row:
                job = ("visits", row, lambda: self.review_visit(row, visit_mode))
        if job is None:
            return False
        table, row, run = job
        try:
            run()
            self.failures.pop((table, row["id"]), None)
            return True
        except ApiError as e:
            if e.status in (401, 403):
                self._pause(600, f"API key không hợp lệ hoặc không có quyền ({e})")
            elif e.status == 402:
                self._pause(1800, f"Hết token / số dư tài khoản AI ({e})")
            elif e.status == 404 or (e.status == 422 and "unsupported" in str(e).lower()):
                model = self.plate_model if table != "visits" else s["ai_model"]
                self._pause(3600, f"Model “{model}” không dùng được cho ảnh, hãy chọn model khác ({e})")
            elif e.status == 429:
                self._pause(60, "Gọi AI quá nhanh, chờ 1 phút")
            elif e.status >= 500:  # nhà cung cấp lỗi / bảo trì: chờ rồi thử lại, không tính là lỗi của mục này
                self._pause(120, f"Nhà cung cấp AI đang lỗi hoặc bảo trì ({e.status}: {e})")
            else:
                self._fail(table, row, f"Lỗi API {e.status}: {e}")
        except httpx.HTTPError as e:   # mất mạng / quá thời gian chờ
            self._pause(120, f"Không kết nối được tới AI ({type(e).__name__}: {e})")
        except (ValueError, KeyError, FileNotFoundError) as e:
            self._fail(table, row, f"{type(e).__name__}: {e}")
        return False

    def _fail(self, table, row, message):
        k = (table, row["id"])
        self.failures[k] = self.failures.get(k, 0) + 1
        print(f"[ai] {table} #{row['id']} lỗi lần {self.failures[k]}: {message}", flush=True)
        if self.failures[k] >= 3:  # bỏ qua mục này, không thử mãi
            col = {"sightings": "ai_status", "visits": "ai_verdict", "plate_candidates": "status"}[table]
            self.conn.execute(f"UPDATE {table} SET {col} = 'error' WHERE id = ?", (row["id"],))
            self.failures.pop(k)
        self._save_state(False, message)
        self.paused_until, self.paused_for = time.time() + 20, (self.settings.get("ai_api_key"), self.settings.get("ai_model"))
        self.paused_plate_model = self.settings.get("ai_plate_model", "")

    # ---------- người có thể trùng ----------
    def review_visit(self, v, mode):
        other = v["maybe_visitor"]
        # lấy lượt của người kia giống lượt này nhất (cũng là lượt ReID đã so)
        cands = self.conn.execute("SELECT v.id, v.img, v.embedding, v.ts, v.camera, c.name AS camera_name FROM visits v"
                                  " LEFT JOIN cameras c ON c.id = v.camera WHERE v.visitor_id = ?"
                                  " ORDER BY v.ts DESC LIMIT 50", (other,)).fetchall()
        if not cands:
            return
        best = cands[0]
        if v["embedding"] and len(v["embedding"]) == 2048:
            mine = np.frombuffer(v["embedding"], np.float32)
            scored = [(float(np.frombuffer(c["embedding"], np.float32) @ mine), c) for c in cands
                      if c["embedding"] and len(c["embedding"]) == 2048]
            if scored:
                best = max(scored, key=lambda x: x[0])[1]
        gap = abs(v["ts"] - best["ts"]) / 60
        content = [
            {"type": "text", "text": (
                f"Image A: camera '{best['camera_name'] or best['camera']}' at {datetime.fromtimestamp(best['ts']):%H:%M %d/%m}. "
                f"Image B: camera '{v['camera_name'] or v['camera']}' at {datetime.fromtimestamp(v['ts']):%H:%M %d/%m} "
                f"({gap:.0f} minutes apart). Are A and B the same person? Reply JSON: "
                '{"same": "yes" | "no" | "unsure", "confidence": 0.0-1.0, '
                '"reason": "at most 25 words in Vietnamese naming the decisive details"}')},
            image_part(best["img"], min_h=384), image_part(v["img"], min_h=384),
        ]
        ans = self._ask(PERSON_SYSTEM, content)
        same = str(ans.get("same", "")).lower()
        verdict = {"yes": "same", "no": "different"}.get(same, "unsure")
        conf = clamp_conf(ans.get("confidence"))
        reason = str(ans.get("reason") or "")[:300]
        # visitor / gợi ý có thể đã đổi trên web trong lúc chờ AI
        cur = self.conn.execute("SELECT visitor_id, maybe_visitor FROM visits WHERE id = ?", (v["id"],)).fetchone()
        if not cur or cur["maybe_visitor"] != other:
            return
        vid = cur["visitor_id"]
        self.conn.execute("UPDATE visits SET ai_verdict = ?, ai_conf = ?, ai_reason = ? WHERE id = ?",
                          (verdict, conf, reason, v["id"]))
        action = "để người dùng chọn"
        if mode == "auto" and conf >= MERGE_CONF and verdict == "same" \
                and self.conn.execute("SELECT 1 FROM visitors WHERE id = ?", (other,)).fetchone():
            merge_visitors(self.conn, vid, other)
            action = f"đã gộp #{vid} vào #{other}"
        elif mode == "auto" and conf >= MERGE_CONF and verdict == "different":
            self.conn.execute("UPDATE visits SET maybe_visitor = NULL, maybe_sim = NULL WHERE (visitor_id = ? AND maybe_visitor = ?)"
                              " OR (visitor_id = ? AND maybe_visitor = ?)", (vid, other, other, vid))
            action = "bỏ gợi ý"
        self.conn.commit()
        print(f"{datetime.now():%H:%M:%S}  [ai] lượt #{v['id']} (#{vid}~#{other}, ReID {v['maybe_sim']:.2f}):"
              f" {verdict} {conf:.2f} → {action} · {reason}", flush=True)

    # ---------- biển số ----------
    def _read_plate(self, plate_img, frame_img, bbox):
        """AI đọc biển: (biển đã chuẩn hoá hoặc None, chuỗi AI trả về, độ chắc, xe nếu có ảnh xung quanh)."""
        around = plate_surroundings(frame_img, json.loads(bbox)) if bbox else None
        content = [
            {"type": "text", "text": (
                ("Image 1 is the cropped licence plate (enlarged), image 2 is a wider view of the same spot with that "
                 "plate in a green box. Read ONLY the plate inside the green box. " if around is not None else
                 "The image is one cropped licence plate from a CCTV camera (enlarged). Read only this plate. ") +
                'Reply JSON: {"plate": "all characters, top row then bottom row, no separators, e.g. 29B112345; '
                'empty if unreadable", "confidence": 0.0-1.0, "note": "at most 15 words in Vietnamese, e.g. which '
                'characters are unclear"' + (VEHICLE_FIELD if around is not None else "") + '}')},
            image_part(plate_img, min_w=480, min_h=160),
        ]
        if around is not None:
            content.append(array_part(around))
        ans = self._ask(PLATE_SYSTEM, content, self.plate_model)
        raw = str(ans.get("plate") or "")
        return (normalize_plate(raw) if raw else None), raw, clamp_conf(ans.get("confidence")), clean_vehicle(ans.get("vehicle"))

    @property
    def plate_model(self):
        """Biển số nhỏ / nghiêng cần model đọc chữ tốt hơn model so người; để trống thì dùng chung."""
        return self.settings.get("ai_plate_model") or self.settings.get("ai_model")

    def parked_plate(self, c):
        """Xe đỗ trước camera bị worker coi là xe mới đi qua mỗi vài phút (nhất là ban đêm biển bị loá, AI
        cũng không đọc được). Biển nằm đúng chỗ và trông y hệt một lần đã xem gần đây thì không hỏi AI nữa.
        Trả về biển đã biết của lần đó ('' nếu lần đó cũng không đọc được), None nếu là xe khác."""
        sig = plate_signature(c["plate_img"])
        if sig is None:
            return None
        for r in self._parked_refs(c):
            ref = plate_signature(r["plate_img"])
            if ref is not None and float(sig @ ref) >= PARKED_SIM:
                return r["plate"]
        return None

    def parked_by_text(self, c, ai_plate):
        """Nắng/bóng đổ làm ảnh biển xe đỗ khác đi nên parked_plate không nhận ra; AI đọc gần giống
        (lệch ≤ 2 ký tự) biển đã thấy đúng chỗ đó thì vẫn là xe đỗ. Trả về biển đã biết hoặc None."""
        key = plate_key(ai_plate)
        for r in self._parked_refs(c):
            if r["plate"] and edit_distance(key, plate_key(r["plate"])) <= 2:
                return r["plate"]
        return None

    def _parked_refs(self, c):
        """Các lần đã xem gần đây có khung biển trùng chỗ với c (mới nhất trước)."""
        box = json.loads(c["bbox"])
        refs = self.conn.execute(
            "SELECT ts, bbox, plate_img, CASE WHEN status = 'unreadable' THEN '' ELSE COALESCE(ai_plate, '') END AS plate"
            " FROM plate_candidates WHERE camera = ? AND id != ? AND status IN ('unreadable', 'duplicate', 'parked')"
            " AND last_seen > ? AND ts < ?"
            " UNION ALL SELECT first_seen, bbox, plate_img, COALESCE(corrected_plate, plate) FROM sightings"
            " WHERE camera = ? AND last_seen > ? AND first_seen < ? ORDER BY 1 DESC LIMIT 40",
            (c["camera"], c["id"], c["ts"] - PARKED_WINDOW, c["ts"],
             c["camera"], c["ts"] - PARKED_WINDOW, c["ts"])).fetchall()
        return [r for r in refs if r["bbox"] and iou(box, json.loads(r["bbox"])) >= PARKED_IOU]

    def parked_spot(self, c, ai_plate, conf):
        """Biển xe đỗ bị loá thì AI đọc sai đi nhiều kiểu (lệch cả 3-4 ký tự) và các lần đã xem thường không có chữ
        nên parked_by_text không nhận ra. Chỗ này đã nhiều lần bị coi là xe đỗ thì lần đọc này cũng là xe đó, trừ khi
        AI đọc chắc ra một biển khác hẳn. Trả về biển đã biết ('' nếu chưa biết) hoặc None."""
        box = json.loads(c["bbox"])
        rows = self.conn.execute(
            "SELECT bbox, COALESCE(ai_plate, '') AS plate FROM plate_candidates"
            " WHERE camera = ? AND id != ? AND status = 'parked' AND ts > ? AND ts < ?",
            (c["camera"], c["id"], c["ts"] - SPOT_WINDOW, c["ts"])).fetchall()
        known = [r["plate"] for r in rows if r["bbox"] and cover(box, json.loads(r["bbox"])) >= SPOT_COVER]
        if len(known) < SPOT_MIN:
            return None
        known = [p for p in known if p]
        key = plate_key(ai_plate)
        similar = not known or any(key[:3] == plate_key(p)[:3] or edit_distance(key, plate_key(p)) <= 3 for p in set(known))
        if not similar and conf >= SPOT_TRUST_CONF:
            return None
        return max(set(known), key=known.count) if known else ""

    def parked_after_read(self, c, ai_plate, conf):
        parked = self.parked_by_text(c, ai_plate)
        if parked is None and c["camera"] in TIDY_CAMS:
            parked = self.parked_spot(c, ai_plate, conf)
        return parked

    def merge_same_pass(self, c, ai_plate, conf, vehicle=None):
        """Xe chạy qua khung hình giữa 2 lần đọc bị dịch xa nên worker tách thành 2 lần đi qua, AI đọc mỗi lần lệch
        vài ký tự. Có lần xuất hiện gần như vậy thì gộp vào, giữ cách đọc chắc hơn (bằng nhau thì lấy biển to hơn).
        Lần xuất hiện do OCR xác nhận hoặc người đã sửa thì giữ nguyên. Trả về True nếu đã gộp."""
        key = plate_key(ai_plate)
        rows = self.conn.execute(
            "SELECT * FROM sightings WHERE camera = ? AND last_seen > ? AND first_seen < ?",
            (c["camera"], c["ts"] - MERGE_WINDOW, c["last_seen"] + MERGE_WINDOW)).fetchall()
        rows = [r for r in rows if edit_distance(key, plate_key(r["corrected_plate"] or r["plate"])) <= MERGE_DIST]
        if not rows:
            return False
        s = min(rows, key=lambda r: edit_distance(key, plate_key(r["corrected_plate"] or r["plate"])))
        width = lambda box: (lambda b: b[2] - b[0])(json.loads(box)) if box else 0
        wins = (s["ai_status"] == "read" and s["corrected_plate"] is None
                and (conf, width(c["bbox"])) > (s["ai_conf"] or 0, width(s["bbox"])))
        self.conn.execute("UPDATE sightings SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?),"
                          " reads = reads + ? WHERE id = ?", (c["ts"], c["last_seen"], c["hits"], s["id"]))
        if wins:  # đổi chỗ: ảnh và cách đọc thua nằm lại ở candidate để lần dọn ảnh vẫn xoá được
            self.conn.execute(
                "UPDATE sightings SET plate = ?, ai_plate = ?, ai_conf = ?, ocr_score = ?, raw_text = ?, plate_img = ?,"
                " frame_img = ?, bbox = ?, vehicle = ? WHERE id = ?",
                (ai_plate, ai_plate, conf, c["ocr_score"], c["ocr_text"], c["plate_img"], c["frame_img"], c["bbox"],
                 json.dumps(vehicle) if vehicle else s["vehicle"], s["id"]))
            self.conn.execute(
                "UPDATE plate_candidates SET status = 'duplicate', ai_plate = ?, ai_conf = ?, ocr_score = ?, ocr_text = ?,"
                " plate_img = ?, frame_img = ?, bbox = ? WHERE id = ?",
                (s["plate"], s["ai_conf"], s["ocr_score"], s["raw_text"], s["plate_img"], s["frame_img"], s["bbox"], c["id"]))
        else:
            self.conn.execute("UPDATE plate_candidates SET status = 'duplicate', ai_plate = ?, ai_conf = ? WHERE id = ?",
                              (ai_plate, conf, c["id"]))
        self.conn.commit()
        print(f"{datetime.now():%H:%M:%S}  [ai] biển chờ #{c['id']} ({c['camera']}): AI đọc {ai_plate} ({conf:.2f}),"
              f" gộp vào lần xuất hiện #{s['id']} {s['plate']} ({s['ai_conf']}) → giữ {ai_plate if wins else s['plate']}", flush=True)
        if plate_key(s["plate"]) != key:  # cách đọc bị thua vẫn có thể là biển đang theo dõi
            self.watch_alert(c, ai_plate)
        return True

    def known_plates(self, ts, skip_id) -> dict:
        """Xe quen: biển đọc chắc nhiều lần riêng biệt hoặc trên nhiều camera, kèm các lần nhận ra xe. {key: (biển, [xe])}"""
        rows = self.conn.execute(
            "SELECT COALESCE(corrected_plate, plate) AS p, camera, first_seen, vehicle FROM sightings"
            " WHERE first_seen > ? AND first_seen <= ? AND id != ? AND COALESCE(corrected_plate, plate) != 'KHÔNG RÕ' AND ("
            " (ai_status IN ('read', 'ok') AND ai_conf >= ?)"
            " OR (corrected_plate IS NOT NULL AND COALESCE(ai_status, '') NOT IN ('fixed', 'unreadable')))"
            " ORDER BY first_seen",
            (ts - KNOWN_DAYS * 86400, ts, skip_id or -1, KNOWN_CONF)).fetchall()
        seen = {}
        for r in rows:
            e = seen.setdefault(plate_key(r["p"]), {"plate": r["p"], "cams": set(), "passes": 0, "last": 0.0, "vehicles": []})
            e["cams"].add(r["camera"])
            if r["first_seen"] - e["last"] >= KNOWN_GAP:
                e["passes"] += 1
                e["last"] = r["first_seen"]
            if r["vehicle"]:
                e["vehicles"].append(json.loads(r["vehicle"]))
        return {k: (e["plate"], e["vehicles"]) for k, e in seen.items()
                if e["passes"] >= KNOWN_PASSES or len(e["cams"]) >= KNOWN_CAMERAS}

    def note_suggestion(self, c, sighting_id, ai_plate, conf, vehicle):
        """Chạy thử: lần đọc chưa chắc gần đúng một xe quen thì ghi lại, không đổi biển. Lỗi ở đây không được
        làm hỏng việc đọc biển."""
        try:
            known = self.known_plates(c["ts"], sighting_id)
            key = plate_key(ai_plate)
            if key in known:
                return
            kind = (vehicle or {}).get("type", "unknown")
            limit = SUGGEST_DIST_CAR if kind in ("car", "truck") else SUGGEST_DIST_BIKE
            near = sorted((edit_distance(key, k), k) for k in known)
            near = [k for d, k in near if d <= limit]
            if not near:
                return
            plate, vehicles = known[near[0]]
            profile = vehicle_profile(vehicles)
            decision = suggestion_decision(vehicle, profile, len(near) == 1)
            self.conn.execute(
                "INSERT INTO plate_suggestions(ts, camera, sighting_id, read_plate, read_conf, known_plate, decision,"
                " vehicle, known_vehicle, plate_img, frame_img) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["ts"], c["camera"], sighting_id, ai_plate, conf, plate, decision, json.dumps(vehicle) if vehicle else None,
                 json.dumps(profile), c["plate_img"], c["frame_img"]))
            self.conn.execute("DELETE FROM plate_suggestions WHERE ts < ?", (c["ts"] - SUGGESTION_DAYS * 86400,))
            self.conn.commit()
            print(f"{datetime.now():%H:%M:%S}  [ai] xe quen (chạy thử): {c['camera']} đọc {ai_plate} ({conf:.2f})"
                  f" gần {plate} → {decision}", flush=True)
        except Exception as e:
            print(f"[ai] lỗi khi xét xe quen: {e}", flush=True)

    def watch_alert(self, c, ai_plate):
        s = self.settings
        if plate_key(ai_plate) in set(filter(None, s.get("watch_plates", "").split(","))):
            cams = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM cameras")}
            self.notifier.settings = s
            self.policy.load(s, notify_matrix(s, list(cams)))
            sent, why = self.policy.send(c["camera"], "watch", ("watch", c["camera"], plate_key(ai_plate)), time.time(),
                                         f"🚗 {ai_plate} – {cams.get(c['camera'], c['camera'])}\n{clock(c['ts'])}"
                                         f" (AI đọc)", IMAGES_DIR / c["frame_img"], at=c["ts"])
            print(f"[ai] biển theo dõi {ai_plate}: {'đã gửi Telegram' if sent else why}", flush=True)

    def review_candidate(self, c):
        parked = self.parked_plate(c)
        if parked is not None:
            self.conn.execute("UPDATE plate_candidates SET status = 'parked', ai_plate = ? WHERE id = ?",
                              (parked, c["id"]))
            self.conn.commit()
            return
        ai_plate, raw, conf, vehicle = self._read_plate(c["plate_img"], c["frame_img"], c["bbox"])
        if not ai_plate or conf < CANDIDATE_CONF:
            self.conn.execute("UPDATE plate_candidates SET status = 'unreadable', ai_plate = ?, ai_conf = ? WHERE id = ?",
                              (ai_plate or raw[:20], conf, c["id"]))
            self.conn.commit()
            print(f"{datetime.now():%H:%M:%S}  [ai] biển chờ #{c['id']} ({c['camera']}): không đọc chắc được"
                  f" ({raw!r} {conf:.2f})", flush=True)
            if ai_plate and self.parked_after_read(c, ai_plate, conf) is None:
                self.note_suggestion(c, None, ai_plate, conf, vehicle)
            return
        parked = self.parked_after_read(c, ai_plate, conf)
        if parked is not None:
            self.conn.execute("UPDATE plate_candidates SET status = 'parked', ai_plate = ?, ai_conf = ? WHERE id = ?",
                              (parked, conf, c["id"]))
            self.conn.commit()
            print(f"{datetime.now():%H:%M:%S}  [ai] biển chờ #{c['id']} ({c['camera']}): AI đọc {ai_plate},"
                  f" cùng chỗ xe đỗ {parked} → bỏ qua", flush=True)
            return
        # OCR có thể đã xác nhận được chính biển này ở lần đi qua khác ngay cạnh đó
        dup = self.conn.execute(
            "SELECT id FROM sightings WHERE camera = ? AND REPLACE(REPLACE(COALESCE(corrected_plate, plate), '-', ''), '.', '') = ?"
            " AND last_seen > ? AND first_seen < ?",
            (c["camera"], plate_key(ai_plate), c["ts"] - SIGHTING_GAP, c["last_seen"] + SIGHTING_GAP)).fetchone()
        if dup:
            self.conn.execute("UPDATE plate_candidates SET status = 'duplicate', ai_plate = ?, ai_conf = ? WHERE id = ?",
                              (ai_plate, conf, c["id"]))
            self.conn.commit()
            return
        if c["camera"] in MERGE_CAMS and self.merge_same_pass(c, ai_plate, conf, vehicle):
            return
        sid = self.conn.execute(
            "INSERT INTO sightings(plate, camera, first_seen, last_seen, reads, ocr_score, raw_text, plate_img, frame_img,"
            " bbox, ai_status, ai_plate, ai_conf, vehicle) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'read', ?, ?, ?)",
            (ai_plate, c["camera"], c["ts"], c["last_seen"], c["hits"], c["ocr_score"], c["ocr_text"], c["plate_img"],
             c["frame_img"], c["bbox"], ai_plate, conf, json.dumps(vehicle) if vehicle else None)).lastrowid
        self.conn.execute("DELETE FROM plate_candidates WHERE id = ?", (c["id"],))  # ảnh giờ thuộc về sighting
        self.conn.commit()
        print(f"{datetime.now():%H:%M:%S}  [ai] biển chờ #{c['id']} ({c['camera']}): AI đọc {ai_plate} ({conf:.2f})"
              f" → lần xuất hiện #{sid} (OCR {c['ocr_text']!r})", flush=True)
        self.watch_alert(c, ai_plate)
        if conf < SUGGEST_BELOW:
            self.note_suggestion(c, sid, ai_plate, conf, vehicle)

    def review_plate(self, s, mode):
        ai_plate, raw, conf, vehicle = self._read_plate(s["plate_img"], s["frame_img"], s["bbox"])
        cur = self.conn.execute("SELECT plate, corrected_plate, ocr_score FROM sightings WHERE id = ?", (s["id"],)).fetchone()
        if not cur:
            return
        if not ai_plate or conf < 0.5:
            status = "unreadable"
        elif plate_key(ai_plate) == plate_key(cur["corrected_plate"] or cur["plate"]):
            status = "ok"
        elif (mode == "auto" and conf >= PLATE_CONF and cur["ocr_score"] < PLATE_TRUST_OCR
              and cur["corrected_plate"] is None):
            status = "fixed"
            self.conn.execute("UPDATE sightings SET corrected_plate = ? WHERE id = ?", (ai_plate, s["id"]))
        else:
            status = "differs"
        self.conn.execute("UPDATE sightings SET ai_status = ?, ai_plate = ?, ai_conf = ?, vehicle = ? WHERE id = ?",
                          (status, ai_plate or raw[:20], conf, json.dumps(vehicle) if vehicle else None, s["id"]))
        self.conn.commit()
        print(f"{datetime.now():%H:%M:%S}  [ai] biển #{s['id']} OCR {cur['plate']} ({cur['ocr_score']:.2f})"
              f" · AI {ai_plate or raw!r} ({conf:.2f}) → {status}", flush=True)


def main():
    init_db()
    conn = connect()
    reviewer = Reviewer(conn)
    print("AI reviewer đã chạy", flush=True)
    while True:
        try:
            busy = reviewer.step()
        except Exception as e:  # lỗi bất ngờ (DB…): ghi log rồi chạy tiếp, không để service chết lặp lại
            print(f"[ai] lỗi: {type(e).__name__}: {e}", flush=True)
            conn.rollback()
            busy = False
        time.sleep(1 if busy else 10)


if __name__ == "__main__":
    main()
