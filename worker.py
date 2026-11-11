"""
Worker: đọc các camera đang bật tính năng, nhận diện biển số, cảnh báo vùng và khuôn mặt.

Một "lần xuất hiện" (sighting) bắt đầu khi biển được đọc trùng đủ số lần và kéo dài
chừng nào biển còn được thấy trên camera đó (khoảng trống < SIGHTING_GAP giây).
Chỉ những khung có chuyển động (cộng một khung định kỳ) mới được đưa qua AI.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from urllib.parse import urlencode, urlsplit

import cv2

import alerts
from alerts import FaceMonitor, MotionGate, NotifyPolicy, ZoneMonitor, clock
from common import GO2RTC_API, IMAGES_DIR, RTSP_HOST, go2rtc_sources, connect, get_settings, init_db, notify_matrix
from notify import Notifier
from plates import plate_key, plate_tail
from recognizer import PlateReader, StreamGrabber, iou, vehicle_regions
from visits import VisitTracker

INTERVAL = float(os.environ.get("LPR_INTERVAL", 0.5))            # giây giữa 2 vòng xử lý
MIN_SCORE = float(os.environ.get("LPR_MIN_SCORE", 0.7))          # ngưỡng tin cậy OCR
VOTES = int(os.environ.get("LPR_VOTES", 3))                      # số lần đọc trùng để xác nhận
VOTE_WINDOW = float(os.environ.get("LPR_VOTE_WINDOW", 15))
SIGHTING_GAP = float(os.environ.get("LPR_SIGHTING_GAP", 120))    # mất dấu quá lâu -> lần xuất hiện mới
RETENTION_DAYS = float(os.environ.get("LPR_RETENTION_DAYS", 30))
MAX_IMAGES_MB = float(os.environ.get("LPR_MAX_IMAGES_MB", 1000))
CLEANUP_BATCH = 100      # số bản ghi xoá mỗi lần commit khi dọn dữ liệu cũ (giữ khoá ghi ngắn)
MODEL = os.environ.get("LPR_MODEL", "yolo-v9-s-608-license-plate-end2end")
FRAME_MAX_WIDTH = 1280
# biển chỉ được tìm trong vùng quanh xe (YOLO thấy xe máy / ô tô); thỉnh thoảng quét cả khung phòng YOLO bỏ sót xe
FULL_SCAN_EVERY = float(os.environ.get("LPR_FULL_SCAN_EVERY", 20))
# xe đứng yên (vùng không đổi) chỉ quét lại sau chừng này giây; phải đủ ngắn để xe đỗ vẫn đọc được VOTES lần
# trong VOTE_WINDOW giây và lần đi qua của biển không bị coi là đã kết thúc (PlatePass.GAP)
STATIC_RESCAN = 4
# camera ưu tiên được nhiều lượt xử lý hơn trong mỗi vòng: "camera:số lượt,…", camera không ghi = 1 lượt.
# Ghi tên mà không ghi số lượt = xen sau mỗi camera thường như cách cũ. Để trống = chia đều.
PRIORITY_CAMS: dict[str, int | None] = {}
for _item in filter(None, os.environ.get("LPR_PRIORITY_CAMS", "").split(",")):
    _cam, _, _weight = _item.strip().partition(":")
    PRIORITY_CAMS[_cam] = max(1, int(_weight)) if _weight.strip().isdigit() else None
# xe đỗ đã biết biển thì không đọc lại (ParkedCars)
PARKED_RECENT = 300   # biển đã biết phải được thấy ở chỗ đó trong chừng này giây gần đây
PARKED_STAY = 60      # lần xuất hiện OCR xác nhận kéo dài từ chừng này mới tính là xe đỗ
PARKED_LOST = 20      # các khung đã xử lý không còn thấy xe đúng chỗ quá lâu (và ít nhất 3 khung) = xe đã đi


class Track:
    """Một xe đang được theo dõi trên một camera.

    Các lần đọc được gom vào cùng track nếu nằm ở cùng vị trí (xe đỗ, OCR đọc lệch vài ký tự)
    hoặc có cùng phần số đuôi (xe đang chạy, vị trí thay đổi). Biển ghi nhận là cách đọc phổ biến nhất.
    """

    def __init__(self, now):
        self.votes = Counter()
        self.tails = Counter()
        self.bbox = None
        self.last_seen = self.flushed_at = now
        self.reads = 0
        self.best = None  # (score, frame, bbox, raw) của lần đọc rõ nhất
        self.sighting_id = None

    @property
    def plate(self):
        return self.votes.most_common(1)[0][0]

    def matches(self, plate, bbox):
        return iou(self.bbox, bbox) > 0.3 or plate_tail(plate) in self.tails

    def add(self, plate, score, bbox, raw, frame, now):
        self.votes[plate] += 1
        self.tails[plate_tail(plate)] += 1
        self.bbox, self.last_seen = bbox, now
        self.reads += 1
        improved = self.best is None or score > self.best[0] + 0.02
        if improved:
            self.best = (score, frame, bbox, raw)
        return improved


class PlatePass:
    """Một lần biển đi qua camera: gom mọi lần phát hiện biển gần nhau về vị trí / thời gian, kể cả lần OCR
    không đọc ra chữ. Nếu cả lần đi qua không track nào đủ phiếu để xác nhận (xe đi nhanh, biển nhỏ / nghiêng)
    thì ảnh biển to nhất được lưu vào plate_candidates để AI đọc."""
    GAP = 6           # không thấy biển quá lâu = xe đã đi
    PARKED = 60       # biển đứng yên lâu mà vẫn không đọc được (xe đỗ): gửi AI luôn, không chờ xe đi

    def __init__(self, now, bbox):
        self.start = self.last_seen = now
        self.bbox = bbox
        self.hits = 0
        self.tracks = []
        self.best = None      # (diện tích, frame, bbox, raw, score)
        self.saved = False
        self.on_vehicle = False  # thấy biển nằm trên xe YOLO phát hiện được (không phải biển hiệu, vật giống biển)

    def near(self, bbox, now):
        if now - self.last_seen > self.GAP:
            return False
        if iou(self.bbox, bbox) > 0.1:
            return True
        w = self.bbox[2] - self.bbox[0]
        dx = (self.bbox[0] + self.bbox[2] - bbox[0] - bbox[2]) / 2
        dy = (self.bbox[1] + self.bbox[3] - bbox[1] - bbox[3]) / 2
        return (dx * dx + dy * dy) ** 0.5 < 3 * w  # xe chạy: biển dịch nhiều giữa 2 khung

    def add(self, bbox, raw, score, frame, now, on_vehicle):
        self.bbox, self.last_seen = bbox, now
        self.hits += 1
        self.on_vehicle |= on_vehicle
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if self.best is None or area > self.best[0]:
            self.best = (area, frame, bbox, raw, score)

    @property
    def confirmed(self):
        return any(t.sighting_id is not None for t in self.tracks)

    @property
    def worth_reading(self):
        # xe chạy nhanh có thể chỉ kịp bắt được 1 khung: vẫn gửi AI nếu biển nằm trên xe
        return self.hits >= 2 or self.on_vehicle


class ParkedCars:
    """Xe đỗ đã biết biển thì không quét biển xe đó nữa khi xe vẫn đứng yên đúng chỗ. Trước đây cứ vài giây lại đọc
    lại, mỗi lần lỡ mất dấu biển quá PlatePass.GAP lại thành một ảnh chờ AI mới (cả nghìn ảnh một ngày).

    Biển đã biết: lần xuất hiện OCR xác nhận kéo dài ở một chỗ, hoặc ảnh chờ AI đã nhận là xe đỗ có biển. Biển chỉ được
    gắn với xe YOLO thấy đứng yên từ trước lúc biển được thấy lần cuối, nên xe khác đến đỗ đúng chỗ đó sau khi xe cũ đi
    vẫn được đọc như thường. Xe xê dịch, rời đi, hay có xe khác che chỗ biển thì quên biển đó và đọc lại."""
    STILL_IOU = 0.85
    STILL_FOR = 5      # xe phải đứng yên ít nhất chừng này giây (xe chạy ngang trước xe đỗ không tính)

    def __init__(self):
        self.still = defaultdict(list)   # camera -> [{box, since, seen, missing_since, missed}] xe đứng yên
        self.known = defaultdict(list)   # camera -> [{plate, bbox, seen, sighting_id, car}]

    def load(self, conn, cams, now):
        for cam in list(self.known):
            if cam not in cams:
                self.known.pop(cam, None), self.still.pop(cam, None)
        rows = conn.execute(
            "SELECT camera, COALESCE(corrected_plate, plate) AS plate, bbox, last_seen, id FROM sightings"
            " WHERE last_seen > ? AND last_seen - first_seen >= ? AND bbox != ''"
            " UNION ALL SELECT camera, ai_plate, bbox, last_seen, NULL FROM plate_candidates"
            " WHERE status = 'parked' AND ai_plate != '' AND last_seen > ? AND bbox != ''",
            (now - PARKED_RECENT, PARKED_STAY, now - PARKED_RECENT)).fetchall()
        for r in rows:
            if r["camera"] not in cams:
                continue
            box = json.loads(r["bbox"])
            k = next((k for k in self.known[r["camera"]] if iou(k["bbox"], box) > 0.5), None)
            if k is None:
                self.known[r["camera"]].append({"plate": r["plate"], "bbox": box, "seen": r["last_seen"],
                                                "sighting_id": r["id"], "car": None})
            elif k["car"] is None and r["last_seen"] > k["seen"]:
                k.update(plate=r["plate"], bbox=box, seen=r["last_seen"], sighting_id=r["id"] or k["sighting_id"])
        for items in self.known.values():
            items[:] = [k for k in items if k["car"] is not None or now - k["seen"] < PARKED_RECENT]

    def _owner(self, still, bbox, now):
        """Xe đứng yên mang biển này: tâm biển nằm trong khung xe (biển có thể lòi xuống dưới chút), gần đáy xe nhất."""
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        owners = [s for s in still if now - s["since"] >= self.STILL_FOR and s["box"][0] <= cx <= s["box"][2] and s["box"][1] <= cy
                  <= s["box"][3] + (s["box"][3] - s["box"][1]) * 0.2]
        # xe đang thấy được trước xe vừa mất dấu (xe xê dịch sang chỗ mới thì nhận ra ngay)
        return min(owners, key=lambda s: (s["missing_since"] is not None, abs(s["box"][3] - cy) / (s["box"][3] - s["box"][1])),
                   default=None)

    def update(self, cam, dets, tracks, conn, now):
        """Mỗi khung đã xử lý của camera đọc biển: cập nhật xe đứng yên và gắn / bỏ biển đã biết."""
        still = self.still[cam]
        matched = set()
        for group, _, box in dets:
            if group not in ("car", "bike"):
                continue
            s = max(still, key=lambda s: iou(s["box"], box), default=None)
            if s is not None and iou(s["box"], box) > self.STILL_IOU:
                matched.add(id(s))
            else:
                s = {"box": tuple(box), "since": now, "missing_since": None, "missed": 0}
                still.append(s)
                matched.add(id(s))
        for s in list(still):
            if id(s) in matched:
                s["missing_since"], s["missed"] = None, 0
                continue
            s["missing_since"] = s["missing_since"] or now
            s["missed"] += 1
            if now - s["missing_since"] > PARKED_LOST and s["missed"] >= 3:
                still.remove(s)
        for k in list(self.known[cam]):
            owner = self._owner(still, k["bbox"], now)
            if k["car"] is not None:
                if owner is not k["car"]:
                    self.known[cam].remove(k)
                    print(f"{datetime.now():%H:%M:%S}  {cam:<8}  xe đỗ {k['plate']} đã đi / đổi chỗ → đọc biển lại",
                          flush=True)
                    continue
                if k["sighting_id"]:
                    # giữ lần xuất hiện còn tiếp diễn như lúc còn đọc liên tục: xe đi thì vẫn nối vào lần này
                    for t in tracks[cam]:
                        if t.sighting_id == k["sighting_id"]:
                            t.last_seen = now
                    if now - k.get("flushed", 0) > 30:
                        conn.execute("UPDATE sightings SET last_seen = MAX(last_seen, ?) WHERE id = ?",
                                     (now, k["sighting_id"]))
                        k["flushed"] = now
            elif owner is not None and owner["since"] <= k["seen"]:
                k["car"] = owner
                print(f"{datetime.now():%H:%M:%S}  {cam:<8}  xe đỗ {k['plate']} đã biết biển → không đọc lại", flush=True)

    def regions(self, cam, shape):
        """Vùng tìm biển của các xe đỗ đã biết biển (để bỏ qua)."""
        return [r for k in self.known[cam] if k["car"] is not None
                for r in vehicle_regions([("car", 1.0, k["car"]["box"])], shape)]

    def covers(self, cam, bbox):
        """Biển vừa đọc là biển của xe đỗ đã biết (quét cả khung, hay vùng xe đỗ bị gộp với người đi ngang)."""
        return any(k["car"] is not None and iou(k["bbox"], bbox) > 0.4 for k in self.known[cam])


def scan_regions(scanned, dets, frame, now, skip=()):
    """Vùng quanh xe cần tìm biển trong khung này; bỏ qua xe đứng yên vừa quét chưa lâu và vùng trong skip.
    scanned: [(vùng, lúc quét)] của camera, được cập nhật tại chỗ."""
    scanned[:] = [(r, t) for r, t in scanned if now - t < STATIC_RESCAN]
    regions = []
    for r in vehicle_regions(dets, frame.shape):
        if any(iou(r, old) > 0.8 for old, _ in scanned) or any(iou(r, s) > 0.8 for s in skip):
            continue
        regions.append(r)
        scanned.append((r, now))
    return regions


def frame_order(cams):
    """Thứ tự xử lý camera trong một vòng: mỗi camera có số lượt theo LPR_PRIORITY_CAMS, các lượt rải đều trong vòng
    (weighted round-robin). Khi mọi camera cùng có chuyển động, thời gian AI chia theo số lượt; camera đứng yên hoặc
    chưa có khung mới bị bỏ qua ngay, nên phần của nó dồn cho các camera đang có chuyển động."""
    others = sum(1 for c in cams if c not in PRIORITY_CAMS)
    weights = {c: (PRIORITY_CAMS[c] or max(1, others)) if c in PRIORITY_CAMS else 1 for c in cams}
    total = sum(weights.values())
    credit = dict.fromkeys(cams, 0)
    order = []
    for _ in range(total):
        for c in cams:
            credit[c] += weights[c]
        best = max(cams, key=credit.__getitem__)
        credit[best] -= total
        order.append(best)
    return order


def save_candidate(conn, cam, p):
    _, frame, bbox, raw, score = p.best
    plate_img, frame_img = save_images(cam, "", frame, bbox, f"{cam}_{datetime.now():%H%M%S}_cand{bbox[0]}")
    conn.execute("INSERT INTO plate_candidates(camera, ts, last_seen, hits, ocr_text, ocr_score, plate_img, frame_img, bbox)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (cam, p.start, p.last_seen, p.hits, raw, score, plate_img, frame_img, json.dumps(bbox)))
    p.saved, p.best = True, None
    print(f"{datetime.now():%H:%M:%S}  {cam:<8}  biển chưa đọc được ({p.hits} khung, OCR {raw!r} {score:.2f}) → chờ AI",
          flush=True)


def save_images(camera, plate, frame, bbox, stem=None):
    """Lưu ảnh biển (crop gốc) và ảnh toàn cảnh (thu nhỏ, có khung). Trả về đường dẫn tương đối."""
    now = datetime.now()
    day_dir = IMAGES_DIR / f"{now:%Y-%m-%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or f"{camera}_{now:%H%M%S}_{plate_tail(plate).replace('.', '')}"
    x1, y1, x2, y2 = bbox
    cv2.imwrite(str(day_dir / f"{stem}_plate.jpg"), frame[y1:y2, x1:x2], [cv2.IMWRITE_JPEG_QUALITY, 92])
    annotated = frame.copy()
    cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 3)
    scale = min(1.0, FRAME_MAX_WIDTH / annotated.shape[1])
    if scale < 1:
        annotated = cv2.resize(annotated, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(day_dir / f"{stem}_frame.jpg"), annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
    rel = day_dir.relative_to(IMAGES_DIR)
    return f"{rel}/{stem}_plate.jpg", f"{rel}/{stem}_frame.jpg"


def confirm(conn, cam, t, now):
    """Track đủ số lần đọc: nối tiếp lần xuất hiện gần đây (worker vừa khởi động lại) hoặc tạo lần mới.

    Trả về ảnh toàn cảnh nếu vừa tạo lần xuất hiện mới, None nếu chỉ nối tiếp.
    """
    plate = t.plate
    prev = conn.execute(
        "SELECT id, reads, plate FROM sightings WHERE camera = ? AND plate LIKE ? AND last_seen > ?"
        " ORDER BY last_seen DESC LIMIT 1",
        (cam, f"%-{plate_tail(plate)}", now - SIGHTING_GAP),
    ).fetchone()
    if not prev:
        # xe đỗ: sau khi khởi động lại OCR có thể đọc lệch 1 số (123.45 -> 123.46) nhưng biển vẫn ở đúng chỗ cũ
        prev = next((r for r in conn.execute(
            "SELECT id, reads, plate, bbox FROM sightings WHERE camera = ? AND last_seen > ? ORDER BY last_seen DESC",
            (cam, now - SIGHTING_GAP)) if r["bbox"] and iou(json.loads(r["bbox"]), t.bbox) > 0.5), None)
    if prev:
        t.sighting_id = prev["id"]
        t.reads += prev["reads"]
        t.votes[prev["plate"]] += prev["reads"]  # giữ biển đã đọc từ trước làm đa số
        return None
    score, frame, bbox, raw = t.best
    plate_img, frame_img = save_images(cam, plate, frame, bbox)
    cur = conn.execute(
        "INSERT INTO sightings(plate, camera, first_seen, last_seen, reads, ocr_score, raw_text,"
        " plate_img, frame_img, bbox) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plate, cam, now, now, t.reads, score, raw, plate_img, frame_img, json.dumps(bbox)),
    )
    conn.commit()
    t.sighting_id = cur.lastrowid
    print(f"{datetime.now():%H:%M:%S}  {cam:<8}  {plate:<12}  ocr={score:.2f}", flush=True)
    return frame_img


def update_images(conn, cam, t):
    """Có lần đọc rõ hơn: thay ảnh của lần xuất hiện bằng frame mới."""
    score, frame, bbox, raw = t.best
    stem = conn.execute("SELECT plate_img FROM sightings WHERE id = ?", (t.sighting_id,)).fetchone()[0]
    save_images(cam, t.plate, frame, bbox, stem.rsplit("/", 1)[1].removesuffix("_plate.jpg"))
    conn.execute("UPDATE sightings SET ocr_score = ?, raw_text = ?, bbox = ? WHERE id = ?",
                 (score, raw, json.dumps(bbox), t.sighting_id))


def cleanup(conn):
    """Xoá dữ liệu cũ hơn RETENTION_DAYS và giữ tổng dung lượng ảnh dưới MAX_IMAGES_MB.

    Ảnh mặt mẫu của người quen (thư mục faces/) không bao giờ bị xoá tự động.
    """
    cutoff = time.time() - RETENTION_DAYS * 86400
    old = conn.execute("SELECT id, plate_img, frame_img FROM sightings WHERE last_seen < ?", (cutoff,)).fetchall()
    old_events = conn.execute("SELECT id, img, frame_img FROM events WHERE last_seen < ?", (cutoff,)).fetchall()
    old_visits = conn.execute("SELECT id, img, frame_img FROM visits WHERE last_seen < ?", (cutoff,)).fetchall()
    old_cands = conn.execute("SELECT id, plate_img, frame_img FROM plate_candidates WHERE last_seen < ?", (cutoff,)).fetchall()
    items = {"sightings": list(old), "events": list(old_events), "visits": list(old_visits), "plate_candidates": list(old_cands)}
    used = sum(f.stat().st_size for f in IMAGES_DIR.rglob("*.jpg")) / 1e6
    if used > MAX_IMAGES_MB:
        # xoá bản ghi cũ nhất của mọi loại theo thời gian chung, vừa đủ xuống 90% giới hạn
        free = (used - MAX_IMAGES_MB * 0.9) * 1e6
        for table, row_id, *imgs in conn.execute(
                "SELECT 'sightings', id, plate_img, frame_img, first_seen AS t FROM sightings"
                " UNION ALL SELECT 'events', id, img, frame_img, ts FROM events"
                " UNION ALL SELECT 'visits', id, img, frame_img, ts FROM visits"
                " UNION ALL SELECT 'plate_candidates', id, plate_img, frame_img, ts FROM plate_candidates"
                " ORDER BY t").fetchall():
            if free <= 0:
                break
            items[table].append((row_id, imgs[0], imgs[1]))
            for rel in imgs[:2]:
                f = IMAGES_DIR / rel
                if f.exists():
                    free -= f.stat().st_size
    # Xoá theo từng lô ngắn, commit rồi mới xoá file ảnh. Trước đây xoá ảnh ngay trong transaction: ổ đang ghi hình
    # nên hơn nghìn file mất tới 2 phút, suốt lúc đó giữ khoá ghi, vòng chính chờ quá 30 s thì sập "database is locked".
    for table, rows in items.items():
        rows = list({r[0]: r for r in rows}.values())  # một bản ghi có thể vừa quá hạn vừa nằm trong phần vượt dung lượng
        for i in range(0, len(rows), CLEANUP_BATCH):
            batch = rows[i:i + CLEANUP_BATCH]
            conn.executemany(f"DELETE FROM {table} WHERE id = ?", [(r[0],) for r in batch])
            conn.commit()
            for _, *imgs in batch:
                for rel in imgs:
                    if rel:
                        (IMAGES_DIR / rel).unlink(missing_ok=True)
    conn.execute("DELETE FROM visitors WHERE id NOT IN (SELECT DISTINCT visitor_id FROM visits)")
    conn.commit()
    for d in sorted(IMAGES_DIR.rglob("*"), reverse=True):  # thư mục con trước thư mục cha
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    if any(items.values()):
        print(f"[cleanup] đã xoá {len(items['sightings'])} lần xuất hiện, {len(items['events'])} sự kiện,"
              f" {len(items['visits'])} lượt qua lại, {len(items['plate_candidates'])} biển chờ cũ (ảnh dùng {used:.0f} MB)",
              flush=True)


def cleanup_in_background() -> threading.Thread:
    """Chạy cleanup ở thread riêng với kết nối DB riêng. Quét ~200 nghìn ảnh có lúc mất ~30 s: chạy trong vòng chính
    thì mọi camera ngừng xử lý chừng ấy, rồi vùng cảnh báo còn coi là mất luồng và "học cảnh" thêm 20 s, người vào
    vùng trong lúc đó không được báo."""
    def run():
        conn = connect()
        try:
            cleanup(conn)
        except Exception as e:  # lần sau thử lại, không được làm dừng worker
            print(f"[cleanup] lỗi: {e}", flush=True)
        finally:
            conn.close()
    t = threading.Thread(target=run, name="cleanup", daemon=True)
    t.start()
    return t


def p2p_streams(previous: dict) -> dict:
    """{luồng go2rtc nối kiểu P2P (Xiaomi): IP camera}. Camera RTSP mất hình là do camera, go2rtc tự nối lại."""
    sources = go2rtc_sources()
    if sources is None:
        return previous
    return {name: urlsplit(s).hostname for name, src in sources.items() for s in src if s.startswith("xiaomi:")}


def pingable(ip: str | None) -> bool:
    if not ip:
        return True
    try:
        return subprocess.run(["ping", "-c", "2", "-W", "1", ip], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True  # không ping được vì lý do khác thì coi như camera còn sống, giữ cách cũ


def reset_stream(cam: str) -> bool:
    """Tạo lại một luồng trong go2rtc với đúng nguồn cũ: go2rtc nối lại camera đó từ đầu, các luồng khác không
    bị đụng tới. Cần vì go2rtc giữ khoá của luồng suốt lúc nối lại camera, một lần nối P2P bị treo làm kẹt
    luôn luồng đó. Người đang xem / ghi phải mở lại kết nối mới vào được luồng mới."""
    src = (go2rtc_sources() or {}).get(cam)
    if not src:
        return False
    query = urlencode([("name", cam)] + [("src", s) for s in src])
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{GO2RTC_API}/api/streams?{query}", method="PUT"),
                                    timeout=5):
            return True
    except OSError as e:
        print(f"[watchdog] không tạo lại được luồng {cam}: {e}", flush=True)
        return False


class Go2rtcWatchdog:
    """go2rtc đôi khi treo kết nối P2P tới một camera Xiaomi (camera vẫn ping được nhưng không ra hình)
    và không tự nối lại. Camera P2P nào mất hình quá STALL giây mà vẫn ping được thì trước hết chỉ tạo lại luồng
    của nó; tạo lại rồi mà vẫn mất hình thêm STALL giây nữa mới khởi động lại go2rtc, vì việc đó làm mọi camera
    khác mất hình vài chục giây. Camera tắt nguồn / mất mạng thì bỏ qua.
    Lần khởi động lại liên tiếp giãn dần (10 → 20 → … tối đa 120 phút)."""
    STALL = 180

    def __init__(self):
        self.started = time.time()
        self.last_restart = 0.0
        self.backoff = 600.0
        self.next_ping = 0.0
        self.offline = set()
        self.reset_at = {}  # camera -> lúc tạo lại luồng, chưa có hình lại từ đó

    def check(self, grabbers, ips, now) -> list[str]:
        """Trả về các camera vừa được tạo lại luồng: worker phải mở kết nối mới tới chúng."""
        stalled = [cam for cam, g in grabbers.items()
                   if now - max(g.last_frame_at or 0, g.started_at, self.started) > self.STALL]
        for cam, g in grabbers.items():
            if cam in self.reset_at and (g.last_frame_at or 0) > self.reset_at[cam]:
                print(f"[watchdog] {cam} có hình lại sau khi tạo lại luồng", flush=True)
                del self.reset_at[cam]
        if not stalled:
            self.backoff = 600.0
            self.offline = set()
            return []
        restart_wait = now - self.last_restart < self.backoff
        if now < self.next_ping or (restart_wait and all(cam in self.reset_at for cam in stalled)):
            return []
        alive = [cam for cam in stalled if pingable(ips.get(cam))]
        offline = set(stalled) - set(alive)
        if offline - self.offline:
            print(f"[watchdog] {', '.join(sorted(offline))} không ping được (tắt nguồn / mất mạng?),"
                  " không khởi động lại go2rtc", flush=True)
        self.offline = offline
        if not alive:
            self.next_ping = now + 60
            return []
        done = [cam for cam in alive if cam not in self.reset_at and reset_stream(cam)]
        if done:
            print(f"[watchdog] {', '.join(done)} mất hình > {self.STALL}s, tạo lại luồng"
                  " (camera khác không bị ảnh hưởng)", flush=True)
            self.reset_at.update(dict.fromkeys(done, now))
            return done
        if restart_wait:
            return []
        print(f"[watchdog] {', '.join(alive)} mất hình > {self.STALL}s, khởi động lại go2rtc", flush=True)
        if self.last_restart:
            self.backoff = min(self.backoff * 2, 7200.0)
        self.last_restart = now
        self.reset_at.clear()
        subprocess.run(["systemctl", "--user", "restart", "go2rtc"], timeout=60, check=False)
        return []


def main():
    init_db()
    conn = connect()
    notifier = Notifier()
    notifier.start()
    camera_names = {}
    policy = NotifyPolicy(notifier)
    zones = ZoneMonitor(conn, policy, camera_names)
    visits = VisitTracker(conn, camera_names)
    reader = objects = face_monitor = None               # model chỉ nạp khi có camera cần tới
    grabbers: dict[str, StreamGrabber] = {}
    gates = defaultdict(MotionGate)
    lpr_cams, face_cams, track_cams = set(), set(), set()
    tracks = defaultdict(list)                           # camera -> [Track]
    passes = defaultdict(list)                           # camera -> [PlatePass]
    scanned = defaultdict(list)                          # camera -> [(vùng xe, lúc quét)]
    parked = ParkedCars()
    last_full_scan = defaultdict(float)
    last_frame_id = defaultdict(int)
    fps_counter = Counter()
    next_camera_sync = next_status = next_cleanup = 0.0
    cleaner = None
    watchdog = Go2rtcWatchdog()
    p2p_cams = {}
    print("Worker đã chạy", flush=True)

    while True:
        try:
            loop_start = now = time.time()

            if now >= next_camera_sync:  # bật/tắt camera và tính năng theo cấu hình trên web
                cams = conn.execute("SELECT id, name, lpr_enabled, face_enabled, track_enabled FROM cameras").fetchall()
                camera_names.update({r["id"]: r["name"] for r in cams})
                lpr_cams = {r["id"] for r in cams if r["lpr_enabled"]}
                face_cams = {r["id"] for r in cams if r["face_enabled"]}
                track_cams = {r["id"] for r in cams if r["track_enabled"]}
                settings = get_settings(conn)
                notifier.settings = settings
                # chu kỳ quét định kỳ khi không có chuyển động, chỉnh trong trang Cài đặt
                alerts.HEARTBEAT = min(60.0, max(2.0, float(settings.get("scan_interval") or alerts.HEARTBEAT)))
                matrix = notify_matrix(settings, [r["id"] for r in cams])
                policy.load(settings, matrix)
                zones.load(settings, matrix)
                visits.load(settings)
                watch_plates = set(filter(None, settings.get("watch_plates", "").split(",")))
                parked.load(conn, lpr_cams, now)
                if lpr_cams and reader is None:
                    reader = PlateReader(MODEL)
                # camera đọc biển cũng chạy YOLO: tìm biển quanh xe rẻ và chính xác hơn quét cả khung
                object_cams = face_cams | track_cams | zones.cameras() | lpr_cams
                if object_cams and objects is None:
                    from detector import ObjectDetector
                    objects = ObjectDetector()
                if face_cams and face_monitor is None:
                    from faces import FaceAnalyzer
                    face_monitor = FaceMonitor(conn, policy, FaceAnalyzer(), camera_names)
                if face_monitor:
                    face_monitor.load()
                enabled = lpr_cams | object_cams
                for cam in enabled - grabbers.keys():
                    grabbers[cam] = StreamGrabber(cam, f"rtsp://{RTSP_HOST}/{cam}")
                    grabbers[cam].start()
                for cam in grabbers.keys() - enabled:
                    grabbers.pop(cam).running = False
                    conn.execute("DELETE FROM camera_status WHERE camera = ?", (cam,))
                    conn.commit()
                p2p_cams = p2p_streams(p2p_cams)
                next_camera_sync = now + 15

            for cam in frame_order(list(grabbers)):
                g = grabbers[cam]
                now = time.time()  # một vòng kéo dài vài giây; mỗi khung mang đúng thời điểm của nó
                frame, fid = g.latest()
                if frame is None or fid == last_frame_id[cam]:
                    continue
                last_frame_id[cam] = fid
                if not gates[cam].check(frame, now):
                    continue
                fps_counter[cam] += 1
                dets = objects.detect(frame) if cam in object_cams else None
                # đọc biển trước tiên: xe chạy nhanh chỉ ở trong khung vài giây
                if cam in lpr_cams:
                    if dets is not None:
                        parked.update(cam, dets, tracks, conn, now)
                    regions = None
                    if dets is not None and now - last_full_scan[cam] < FULL_SCAN_EVERY:
                        regions = scan_regions(scanned[cam], dets, frame, now, parked.regions(cam, frame.shape))
                    else:
                        last_full_scan[cam] = now
                    reads = reader.read(frame, regions) if regions is None or regions else []
                    # khung quét cả hình: biển vẫn tính là nằm trên xe nếu tâm biển ở trong vùng quanh một xe,
                    # nếu không xe chạy nhanh chỉ kịp đọc 1 khung đúng lúc này sẽ bị bỏ qua
                    vehicles = vehicle_regions(dets, frame.shape) if regions is None and dets else []
                    for plate, score, bbox, raw in reads:
                        if parked.covers(cam, bbox):
                            continue
                        p = next((p for p in passes[cam] if p.near(bbox, now)), None)
                        if p is None:
                            p = PlatePass(now, bbox)
                            passes[cam].append(p)
                        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                        on_vehicle = regions is not None or any(v[0] <= cx <= v[2] and v[1] <= cy <= v[3] for v in vehicles)
                        p.add(bbox, raw, score, frame, now, on_vehicle)
                        if plate is None or score < MIN_SCORE:
                            continue
                        t = next((t for t in tracks[cam] if t.matches(plate, bbox)), None)
                        if t is None:
                            t = Track(now)
                            tracks[cam].append(t)
                        if t not in p.tracks:
                            p.tracks.append(t)
                        improved = t.add(plate, score, bbox, raw, frame, now)
                        if t.sighting_id is None and t.reads >= VOTES:
                            frame_img = confirm(conn, cam, t, now)
                            if frame_img and plate_key(t.plate) in watch_plates:
                                policy.send(cam, "watch", ("watch", cam, plate_key(t.plate)), now,
                                            f"🚗 {t.plate} – {camera_names.get(cam, cam)}\n{clock(now)}", IMAGES_DIR / frame_img)
                        elif t.sighting_id is not None and improved:
                            update_images(conn, cam, t)
                if dets is not None:
                    zones.process(cam, frame, dets, now)
                    recognized = face_monitor.process(cam, frame, dets, now) if cam in face_cams else []
                    if cam in track_cams:
                        visits.process(cam, frame, dets, now)
                        for pbox, pid in recognized:
                            visits.link(cam, pbox, pid)

            now = time.time()
            # lần biển đi qua mà không xác nhận được biển nào: lưu ảnh để AI đọc
            for cam, items in passes.items():
                for p in list(items):
                    gone = now - p.last_seen > PlatePass.GAP
                    if not p.saved and not p.confirmed and p.worth_reading and (gone or now - p.start > PlatePass.PARKED):
                        save_candidate(conn, cam, p)
                    if gone:
                        items.remove(p)
                    elif p.confirmed or p.saved:
                        p.best = None  # không cần giữ frame trong RAM nữa

            # ghi last_seen / biển (theo đa số) định kỳ, đóng các track đã mất dấu
            for cam, items in tracks.items():
                for t in list(items):
                    confirmed = t.sighting_id is not None
                    expired = now - t.last_seen > (SIGHTING_GAP if confirmed else VOTE_WINDOW)
                    if confirmed and (expired or now - t.flushed_at > 5):
                        conn.execute("UPDATE sightings SET last_seen = ?, reads = ?, plate = ? WHERE id = ?",
                                     (t.last_seen, t.reads, t.plate, t.sighting_id))
                        t.flushed_at = now
                    if expired:
                        items.remove(t)
                    elif confirmed:
                        t.best = (t.best[0], None, t.best[2], t.best[3])  # đã lưu ảnh, không giữ frame trong RAM
            conn.commit()

            if now >= next_status:
                for cam, g in grabbers.items():
                    conn.execute(
                        "INSERT INTO camera_status(camera, connected, last_frame_at, fps, motion, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(camera) DO UPDATE SET connected = excluded.connected,"
                        " last_frame_at = excluded.last_frame_at, fps = excluded.fps, motion = excluded.motion,"
                        " updated_at = excluded.updated_at",
                        (cam, int(g.connected), g.last_frame_at, fps_counter[cam] / 10, gates[cam].take_ratio(), now),
                    )
                fps_counter.clear()
                conn.commit()
                for cam in watchdog.check({c: g for c, g in grabbers.items() if c in p2p_cams}, p2p_cams, now):
                    grabbers[cam].running = False  # kết nối cũ còn treo ở luồng cũ trong go2rtc
                    grabbers[cam] = StreamGrabber(cam, f"rtsp://{RTSP_HOST}/{cam}")
                    grabbers[cam].start()
                next_status = now + 10

            if now >= next_cleanup and not (cleaner and cleaner.is_alive()):
                cleaner = cleanup_in_background()
                next_cleanup = now + 3600

            time.sleep(max(0.0, INTERVAL - (time.time() - loop_start)))
        except sqlite3.OperationalError as e:
            # web / AI / dọn dữ liệu giữ khoá ghi quá busy_timeout (30 s): bỏ vòng này, đừng làm sập cả worker
            if "locked" not in str(e):
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            print(f"{datetime.now():%H:%M:%S}  DB đang bị khoá, bỏ qua một vòng ({e})", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
