"""Lọc chuyển động, cảnh báo vùng và nhận diện khuôn mặt cho worker."""

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime

import cv2
import numpy as np

from common import IMAGES_DIR
from detector import GROUP_LABELS
from faces import FACE_MATCH, face_crop, is_frontal, to_blob
from recognizer import iou

MOTION_THRESHOLD = float(os.environ.get("LPR_MOTION", 0.003))   # tỉ lệ điểm ảnh thay đổi; 0 = tắt lọc
MOTION_HOLD = float(os.environ.get("LPR_MOTION_HOLD", 3))        # tiếp tục xử lý vài giây sau khi hết chuyển động
HEARTBEAT = float(os.environ.get("LPR_HEARTBEAT", 10))           # vẫn xử lý định kỳ để giữ trạng thái xe đỗ
FACE_MIN_SIZE = int(os.environ.get("LPR_FACE_MIN_SIZE", 32))     # mặt nhỏ hơn (px) thì bỏ qua
FACE_GAP = float(os.environ.get("LPR_FACE_GAP", 300))            # cùng một người trong khoảng này -> 1 sự kiện
FRAME_MAX_WIDTH = 1280
FULL_FRAME = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)


class MotionGate:
    """Chỉ cho chạy AI khi khung hình thay đổi, giúp camera đứng yên gần như không tốn CPU."""

    def __init__(self):
        self.prev = None
        self.active_until = self.last_run = 0.0
        self.checked = self.passed = 0

    def check(self, frame, now) -> bool:
        self.checked += 1
        if MOTION_THRESHOLD <= 0:
            self.passed += 1
            return True
        small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        gray[:10, :90] = 0  # đồng hồ camera Xiaomi ở góc trên trái đổi mỗi giây
        if self.prev is not None:
            changed = np.count_nonzero(cv2.absdiff(gray, self.prev) > 25) / gray.size
            if changed > MOTION_THRESHOLD:
                self.active_until = now + MOTION_HOLD
        else:
            self.active_until = now + MOTION_HOLD
        self.prev = gray
        if now < self.active_until or now - self.last_run >= HEARTBEAT:
            self.last_run = now
            self.passed += 1
            return True
        return False

    def take_ratio(self):
        """Tỉ lệ khung được phân tích từ lần gọi trước (để hiển thị trên web)."""
        ratio = self.passed / self.checked if self.checked else None
        self.checked = self.passed = 0
        return ratio


def save_event_images(cam, crop, frame, now, draw):
    """Lưu ảnh cắt + ảnh toàn cảnh (thu nhỏ, đã vẽ khung). Trả đường dẫn tương đối trong IMAGES_DIR."""
    d = datetime.fromtimestamp(now)
    day_dir = IMAGES_DIR / "events" / f"{d:%Y-%m-%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{cam}_{d:%H%M%S}_{uuid.uuid4().hex[:6]}"
    annotated = frame.copy()
    draw(annotated)
    scale = min(1.0, FRAME_MAX_WIDTH / annotated.shape[1])
    if scale < 1:
        annotated = cv2.resize(annotated, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(day_dir / f"{stem}_crop.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    cv2.imwrite(str(day_dir / f"{stem}_frame.jpg"), annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
    rel = day_dir.relative_to(IMAGES_DIR)
    return f"{rel}/{stem}_crop.jpg", f"{rel}/{stem}_frame.jpg"


def overwrite_event_images(conn, event_id, crop, frame, draw, paths=None):
    """Thay ảnh của sự kiện (hoặc của paths cho sẵn) bằng khung rõ hơn, giữ nguyên tên file."""
    img, frame_img = paths or conn.execute("SELECT img, frame_img FROM events WHERE id = ?", (event_id,)).fetchone()
    annotated = frame.copy()
    draw(annotated)
    scale = min(1.0, FRAME_MAX_WIDTH / annotated.shape[1])
    if scale < 1:
        annotated = cv2.resize(annotated, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(IMAGES_DIR / img), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
    cv2.imwrite(str(IMAGES_DIR / frame_img), annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])


def crop_box(frame, box, pad=0.1):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    px, py = int((x2 - x1) * pad), int((y2 - y1) * pad)
    return frame[max(0, y1 - py):min(h, y2 + py), max(0, x1 - px):min(w, x2 + px)]


def same_object(a, b) -> bool:
    """Cùng một vật nếu 2 khung chồng nhau, hoặc tâm gần nhau (khung xe đỗ bị người đi qua che, co giãn)."""
    if iou(a, b) > 0.2:
        return True
    ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    diag = ((a[2] - a[0]) ** 2 + (a[3] - a[1]) ** 2) ** 0.5
    return ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5 < diag * 0.25


def in_schedule(start: str, end: str, now: float) -> bool:
    if not start or not end:
        return True
    t = datetime.fromtimestamp(now).strftime("%H:%M")
    return start <= t < end if start <= end else t >= start or t < end  # khung qua đêm, vd 22:00-06:00


def clock(now):
    return datetime.fromtimestamp(now).strftime("%H:%M:%S %d/%m")


# thời gian chờ mặc định giữa 2 tin cùng loại (giây), người dùng chỉnh trong trang Cài đặt
DEFAULT_COOLDOWN = {"camera": 120, "unknown": 0, "watch": 300}


class NotifyPolicy:
    """Quyết định có gửi Telegram cho một sự kiện không, và nếu không thì vì sao.

    Gửi khi: camera bật loại thông báo đó, đang trong khung giờ gửi, đã qua thời gian chờ kể từ
    tin trước có cùng khoá (vùng / camera / người / biển). Sự kiện luôn được ghi lịch sử dù có gửi hay không.
    """

    def __init__(self, notifier):
        self.notifier = notifier
        self.settings, self.matrix = {}, {}
        self.last_sent = {}

    def load(self, settings, matrix):
        self.settings, self.matrix = settings, matrix

    def times(self, mode):
        """(giờ bắt đầu, giờ kết thúc, thời gian chờ) của một loại thông báo."""
        prefix = "camera_alert" if mode == "camera" else mode
        cooldown = self.settings.get(f"{prefix}_cooldown")
        return (self.settings.get(f"{prefix}_start", ""), self.settings.get(f"{prefix}_end", ""),
                int(cooldown) if cooldown not in (None, "") else DEFAULT_COOLDOWN[mode])

    def send(self, cam, mode, key, now, text, photo, times=None, enabled=True):
        """Gửi nếu được phép. Trả (đã gửi?, lý do không gửi)."""
        if mode not in self.matrix.get(cam, ()):
            return False, "Camera không bật gửi loại thông báo này"
        if not enabled:
            return False, "Đã tắt gửi Telegram cho mục này"
        start, end, cooldown = times or self.times(mode)
        if not in_schedule(start, end, now):
            return False, f"Ngoài khung giờ gửi ({start}–{end})"
        if now - self.last_sent.get(key, -1e12) < cooldown:
            return False, f"Đang trong thời gian chờ {cooldown} giây"
        if not self.notifier.send(text, photo):
            return False, "Chưa cài đặt Telegram"
        self.last_sent[key] = now
        return True, ""


class ZoneMonitor:
    """Báo khi có người/xe MỚI đi vào vùng; xe đỗ sẵn trong vùng không bị báo lặp lại.

    Sau khi worker khởi động, camera mất luồng một lúc hoặc vừa tạo vùng, hệ thống "học lại cảnh"
    trong BASELINE giây: những gì đang có sẵn trong vùng được ghi nhận nhưng không báo.
    """

    PRESENT_TTL = 30      # vật đang di chuyển: mất dấu quá lâu thì coi như đã rời vùng
    PARKED_TTL = 300      # vật đứng yên lâu (xe đỗ): chịu được bị che khuất / nhận sót lâu hơn
    PARKED_AFTER = 60     # có mặt liên tục bao lâu thì coi là đứng yên
    BASELINE = 20
    STALL = 25            # không có khung nào lâu hơn (khung định kỳ là 10 s) = mất luồng / vừa khởi động

    def __init__(self, conn, policy, camera_names):
        self.conn, self.policy, self.camera_names = conn, policy, camera_names
        self.zones = defaultdict(list)       # camera -> [zone]
        self.present = defaultdict(list)     # zone id -> [[bbox, last_seen, hits, recorded, since]]
        self.last_frame = {}                 # camera -> lúc xử lý khung gần nhất
        self.baseline_until = {}             # camera / zone id -> hết giai đoạn học cảnh

    def load(self, settings, matrix):
        self.zones.clear()
        for r in self.conn.execute("SELECT * FROM zones WHERE enabled = 1"):
            z = dict(r)
            z["points"] = np.array(json.loads(z["points"]), np.float32)
            z["classes"] = set(filter(None, z["classes"].split(",")))
            if len(z["points"]) >= 3 and z["classes"]:
                self.zones[z["camera"]].append(z)
        # chế độ "toàn bộ camera": mỗi camera được chọn có thêm một vùng ảo phủ cả khung hình
        classes = set(filter(None, settings.get("camera_alert_classes", "person").split(",")))
        for cam in (c for c, modes in matrix.items() if "camera" in modes):
            self.zones[cam].append({
                "id": f"cam:{cam}", "name": "Toàn khung hình", "points": FULL_FRAME, "classes": classes,
                "start_time": "", "end_time": "", "cooldown": 0, "notify": 1, "virtual": True,
            })

    def cameras(self):
        return {c for c, zs in self.zones.items() if zs}

    def process(self, cam, frame, dets, now):
        h, w = frame.shape[:2]
        if now - self.last_frame.get(cam, 0) > max(self.STALL, HEARTBEAT * 2.5):
            self.baseline_until[cam] = now + self.BASELINE
        self.last_frame[cam] = now
        for z in self.zones.get(cam, []):
            present = self.present[z["id"]]
            present[:] = [p for p in present
                          if now - p[1] < (self.PARKED_TTL if p[1] - p[4] > self.PARKED_AFTER else self.PRESENT_TTL)]
            zone_start = self.baseline_until.setdefault(z["id"], now + self.BASELINE)
            quiet = now < max(self.baseline_until.get(cam, 0), zone_start)
            poly = (z["points"] * [w, h]).astype(np.int32)
            new = []
            for group, conf, box in dets:
                if group not in z["classes"]:
                    continue
                x1, y1, x2, y2 = box
                # tính theo chân / bánh xe (đáy khung) hoặc tâm đối tượng
                anchors = [((x1 + x2) / 2, float(y2)), ((x1 + x2) / 2, (y1 + y2) / 2)]
                if not any(cv2.pointPolygonTest(poly, a, False) >= 0 for a in anchors):
                    continue
                p = next((p for p in present if same_object(p[0], box)), None)
                if p is None:
                    p = [box, now, 0, quiet, now]  # vật có sẵn lúc học cảnh: coi như đã ghi
                    present.append(p)
                p[0], p[1] = box, now
                p[2] += 1
                # cần thấy ở 2 khung liên tiếp để tránh báo nhầm do bóng / nhiễu
                if p[2] >= 2 and not p[3]:
                    p[3] = True
                    if not quiet:
                        new.append((group, conf, box))
            if new:
                self.alert(cam, z, poly, frame, new, now)

    def alert(self, cam, z, poly, frame, new, now):
        group, conf, box = max(new, key=lambda d: d[1])
        labels = sorted({GROUP_LABELS[g] for g, _, _ in new})

        def draw(img):
            if z.get("virtual"):
                for _, _, (x1, y1, x2, y2) in new:
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 255), 4)
                return
            overlay = img.copy()
            cv2.fillPoly(overlay, [poly], (0, 0, 255))
            cv2.addWeighted(overlay, 0.18, img, 0.82, 0, img)
            cv2.polylines(img, [poly], True, (0, 0, 255), 3)
            for _, _, (x1, y1, x2, y2) in new:
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 255), 4)

        img, frame_img = save_event_images(cam, crop_box(frame, box), frame, now, draw)
        label = ", ".join(labels)
        cam_name = self.camera_names.get(cam, cam)
        if z.get("virtual"):
            text = f"⚠️ {label} xuất hiện – {cam_name}\n{clock(now)}"
        else:
            text = f"⚠️ {label} vào vùng «{z['name']}» – {cam_name}\n{clock(now)}"
        if z.get("virtual"):
            notified, note = self.policy.send(cam, "camera", ("camera", cam), now, text, IMAGES_DIR / frame_img)
        else:  # vùng tự vẽ dùng khung giờ / thời gian chờ riêng của vùng
            notified, note = self.policy.send(
                cam, "zone", ("zone", z["id"]), now, text, IMAGES_DIR / frame_img,
                times=(z["start_time"], z["end_time"], z["cooldown"]), enabled=bool(z["notify"]))
        self.conn.execute(
            "INSERT INTO events(kind, camera, ts, last_seen, label, zone_id, score, img, frame_img, notified, note)"
            " VALUES ('zone', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cam, now, now, label, None if z.get("virtual") else z["id"], conf, img, frame_img, int(notified), note))
        self.conn.commit()
        print(f"{datetime.now():%H:%M:%S}  {cam:<8}  vùng «{z['name']}»: {label}", flush=True)


class FaceMonitor:
    """Tìm mặt trong phần trên của mỗi người được phát hiện, so với ảnh mẫu của người quen.

    Mỗi người được theo dõi thành một "track": người lạ chỉ được ghi khi thấy ít nhất 2 lần
    (để kịp nhận ra nếu thật ra là người quen), cùng một người trong FACE_GAP giây chỉ ghi 1 sự kiện.
    """

    def __init__(self, conn, policy, analyzer, camera_names):
        self.conn, self.policy, self.analyzer, self.camera_names = conn, policy, analyzer, camera_names
        self.tracks = defaultdict(list)  # camera -> [track dict]
        self.known = np.zeros((0, 512), np.float32)
        self.known_ids = np.zeros(0, np.int64)
        self.people = {}
        self.signature = None

    def load(self):
        sig = self.conn.execute("SELECT COUNT(*), MAX(id) FROM faces").fetchone()
        self.people = {r["id"]: dict(r) for r in self.conn.execute("SELECT id, name, notify FROM people")}
        if tuple(sig) == self.signature:
            return
        self.signature = tuple(sig)
        rows = self.conn.execute("SELECT person_id, embedding FROM faces").fetchall()
        self.known = np.array([np.frombuffer(r["embedding"], np.float32) for r in rows], np.float32).reshape(-1, 512)
        self.known_ids = np.array([r["person_id"] for r in rows], np.int64)

    def match(self, emb):
        """Trả (person_id hoặc None, độ giống cao nhất)."""
        if not len(self.known):
            return None, 0.0
        sims = self.known @ emb
        i = int(sims.argmax())
        pid = int(self.known_ids[i])
        return (pid if sims[i] >= FACE_MATCH and pid in self.people else None), float(sims[i])

    def process(self, cam, frame, dets, now):
        """Trả về [(khung người, person_id)] những người nhận ra được tên trong khung này."""
        h, w = frame.shape[:2]
        recognized = []
        tracks = self.tracks[cam]
        # track người lạ mới thấy 1 lần thì chỉ giữ 10 giây, track đã ghi thì giữ FACE_GAP
        tracks[:] = [t for t in tracks if now - t["last_seen"] < (FACE_GAP if t["event_id"] else 10)]
        for group, conf, (x1, y1, x2, y2) in dets:
            if group != "person" or conf < 0.5:
                continue
            bw, bh = x2 - x1, y2 - y1
            rx1, ry1 = max(0, int(x1 - bw * 0.15)), max(0, int(y1 - bh * 0.08))
            rx2, ry2 = min(w, int(x2 + bw * 0.15)), min(h, int(y1 + max(bh * 0.5, bw * 1.2)))
            region = frame[ry1:ry2, rx1:rx2]
            if region.size == 0:
                continue
            faces = self.analyzer.detect(region, 320)
            if not faces:
                continue
            score, fbox, kps = faces[0]
            fbox = fbox + [rx1, ry1, rx1, ry1]
            kps = kps + [rx1, ry1]
            size = min(fbox[2] - fbox[0], fbox[3] - fbox[1])
            if size < FACE_MIN_SIZE or score < 0.6 or not is_frontal(kps, fbox):
                continue
            emb = self.analyzer.embed(frame, kps)
            pid, sim = self.match(emb)
            self.observe(cam, tracks, frame, (x1, y1, x2, y2), fbox.astype(int), emb, pid, sim, score * size, now)
            if pid is not None:
                recognized.append(((x1, y1, x2, y2), pid))
        return recognized

    def observe(self, cam, tracks, frame, pbox, fbox, emb, pid, sim, quality, now):
        def same(t):
            if pid is not None and t["pid"] is not None:
                return pid == t["pid"]
            close = float(t["emb"] @ emb)
            # cùng vị trí vài giây trước vẫn phải hơi giống mặt, tránh gộp 2 người đứng cùng chỗ
            return close > 0.45 or (now - t["last_seen"] < 5 and iou(t["pbox"], pbox) > 0.3 and close > 0.2)

        t = next((t for t in tracks if same(t)), None)
        if t is None:
            t = {"pid": None, "emb": emb, "pbox": pbox, "last_seen": now, "hits": 0, "quality": 0,
                 "sim": 0.0, "event_id": None, "frame": None, "fbox": None}
            tracks.append(t)
        t["pbox"], t["last_seen"] = pbox, now
        t["hits"] += 1
        upgraded = pid is not None and t["pid"] is None
        if upgraded:
            t["pid"] = pid
        # track đã biết tên chỉ lấy ảnh từ những lần cũng nhận ra đúng người đó
        improved = upgraded or (quality > t["quality"] * 1.15 and (t["pid"] is None or pid == t["pid"]))
        if improved:
            t.update(emb=emb, quality=quality, sim=sim, frame=frame, fbox=fbox)

        if t["event_id"] is None:
            if t["pid"] is not None or t["hits"] >= 2:
                self.create_event(cam, t, now)
        else:
            if improved:
                self.update_event(t)
            self.conn.execute("UPDATE events SET last_seen = ? WHERE id = ?", (now, t["event_id"]))
            self.conn.commit()

    def _draw(self, t):
        x1, y1, x2, y2 = t["fbox"]
        color = (0, 200, 0) if t["pid"] else (0, 0, 255)
        return lambda img: cv2.rectangle(img, (x1, y1), (x2, y2), color, 3)

    def _label(self, t):
        return self.people[t["pid"]]["name"] if t["pid"] in self.people else "Người lạ"

    def create_event(self, cam, t, now):
        img, frame_img = save_event_images(cam, face_crop(t["frame"], t["fbox"]), t["frame"], now, self._draw(t))
        label = self._label(t)
        icon = "👤" if t["pid"] else "❓"
        text = f"{icon} {label} – {self.camera_names.get(cam, cam)}\n{clock(now)}"
        if t["pid"] in self.people:  # người quen: chỉ báo khi người này nằm trong danh sách theo dõi
            if self.people[t["pid"]]["notify"]:
                notified, note = self.policy.send(cam, "watch", ("watch", cam, t["pid"]), now, text,
                                                  IMAGES_DIR / frame_img)
            else:
                notified, note = False, "Người này không nằm trong danh sách theo dõi"
        else:
            notified, note = self.policy.send(cam, "unknown", ("unknown", cam), now, text, IMAGES_DIR / frame_img)
        cur = self.conn.execute(
            "INSERT INTO events(kind, camera, ts, last_seen, label, person_id, score, img, frame_img, embedding,"
            " notified, note) VALUES ('face', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cam, now, now, label, t["pid"], t["sim"], img, frame_img, to_blob(t["emb"]), int(notified), note))
        self.conn.commit()
        t["event_id"] = cur.lastrowid
        t["frame"] = None  # đã lưu ảnh, không giữ frame trong RAM
        print(f"{datetime.now():%H:%M:%S}  {cam:<8}  khuôn mặt: {label} (giống {t['sim']:.2f})", flush=True)

    def update_event(self, t):
        overwrite_event_images(self.conn, t["event_id"], face_crop(t["frame"], t["fbox"]), t["frame"], self._draw(t))
        self.conn.execute("UPDATE events SET label = ?, person_id = ?, score = ?, embedding = ? WHERE id = ?",
                          (self._label(t), t["pid"], t["sim"], to_blob(t["emb"]), t["event_id"]))
        t["frame"] = None
