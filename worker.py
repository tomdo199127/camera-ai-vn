"""
Worker: đọc các camera đang bật tính năng, nhận diện biển số, cảnh báo vùng và khuôn mặt.

Một "lần xuất hiện" (sighting) bắt đầu khi biển được đọc trùng đủ số lần và kéo dài
chừng nào biển còn được thấy trên camera đó (khoảng trống < SIGHTING_GAP giây).
Chỉ những khung có chuyển động (cộng một khung định kỳ) mới được đưa qua AI.
"""

import json
import os
import shutil
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime

import cv2

import alerts
from alerts import FaceMonitor, MotionGate, NotifyPolicy, ZoneMonitor, clock
from common import IMAGES_DIR, RTSP_HOST, go2rtc_sources, connect, get_settings, init_db, notify_matrix
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
MODEL = os.environ.get("LPR_MODEL", "yolo-v9-s-608-license-plate-end2end")
FRAME_MAX_WIDTH = 1280
# biển chỉ được tìm trong vùng quanh xe (YOLO thấy xe máy / ô tô); thỉnh thoảng quét cả khung phòng YOLO bỏ sót xe
FULL_SCAN_EVERY = float(os.environ.get("LPR_FULL_SCAN_EVERY", 20))
# xe đứng yên (vùng không đổi) chỉ quét lại sau chừng này giây; phải đủ ngắn để xe đỗ vẫn đọc được VOTES lần
# trong VOTE_WINDOW giây và lần đi qua của biển không bị coi là đã kết thúc (PlatePass.GAP)
STATIC_RESCAN = 4


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


def scan_regions(scanned, dets, frame, now):
    """Vùng quanh xe cần tìm biển trong khung này; bỏ qua xe đứng yên vừa quét chưa lâu.
    scanned: [(vùng, lúc quét)] của camera, được cập nhật tại chỗ."""
    scanned[:] = [(r, t) for r, t in scanned if now - t < STATIC_RESCAN]
    regions = []
    for r in vehicle_regions(dets, frame.shape):
        if any(iou(r, old) > 0.8 for old, _ in scanned):
            continue
        regions.append(r)
        scanned.append((r, now))
    return regions


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
    used = sum(f.stat().st_size for f in IMAGES_DIR.rglob("*.jpg")) / 1e6
    if used > MAX_IMAGES_MB:
        # mỗi lần xuất hiện / sự kiện ~150KB ảnh; xoá dần các bản ghi cũ nhất của cả hai loại
        n = int((used - MAX_IMAGES_MB * 0.9) / 0.15) + 1
        old += conn.execute("SELECT id, plate_img, frame_img FROM sightings ORDER BY first_seen LIMIT ?", (n,)).fetchall()
        old_events += conn.execute("SELECT id, img, frame_img FROM events ORDER BY ts LIMIT ?", (n,)).fetchall()
        old_visits += conn.execute("SELECT id, img, frame_img FROM visits ORDER BY ts LIMIT ?", (n,)).fetchall()
    for table, items in (("sightings", old), ("events", old_events), ("visits", old_visits),
                         ("plate_candidates", old_cands)):
        for row in items:
            for rel in tuple(row)[1:]:
                (IMAGES_DIR / rel).unlink(missing_ok=True)
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
    conn.execute("DELETE FROM visitors WHERE id NOT IN (SELECT DISTINCT visitor_id FROM visits)")
    conn.commit()
    for d in sorted(IMAGES_DIR.rglob("*"), reverse=True):  # thư mục con trước thư mục cha
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
    if old or old_events:
        print(f"[cleanup] đã xoá {len(old)} lần xuất hiện, {len(old_events)} sự kiện cũ (ảnh dùng {used:.0f} MB)",
              flush=True)


def p2p_streams(previous: set) -> set:
    """Luồng go2rtc nối kiểu P2P (Xiaomi). Camera RTSP mất hình là do camera, go2rtc tự nối lại."""
    sources = go2rtc_sources()
    if sources is None:
        return previous
    return {name for name, src in sources.items() if any(s.startswith("xiaomi:") for s in src)}


class Go2rtcWatchdog:
    """go2rtc đôi khi treo kết nối P2P tới một camera Xiaomi (camera vẫn ping được nhưng không ra hình)
    và không tự nối lại. Camera P2P nào mất hình quá STALL giây thì khởi động lại go2rtc;
    nếu camera tắt hẳn thì giãn dần khoảng cách giữa các lần khởi động lại (10 → 20 → … tối đa 120 phút)."""
    STALL = 180

    def __init__(self):
        self.started = time.time()
        self.last_restart = 0.0
        self.backoff = 600.0

    def check(self, grabbers, now):
        stalled = [cam for cam, g in grabbers.items()
                   if now - max(g.last_frame_at or 0, g.started_at, self.started) > self.STALL]
        if not stalled:
            self.backoff = 600.0
            return
        if now - self.last_restart < self.backoff:
            return
        print(f"[watchdog] {', '.join(stalled)} mất hình > {self.STALL}s, khởi động lại go2rtc", flush=True)
        if self.last_restart:
            self.backoff = min(self.backoff * 2, 7200.0)
        self.last_restart = now
        subprocess.run(["systemctl", "--user", "restart", "go2rtc"], timeout=60, check=False)


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
    last_full_scan = defaultdict(float)
    last_frame_id = defaultdict(int)
    fps_counter = Counter()
    next_camera_sync = next_status = next_cleanup = 0.0
    watchdog = Go2rtcWatchdog()
    p2p_cams = set()
    print("Worker đã chạy", flush=True)

    while True:
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

        for cam, g in list(grabbers.items()):
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
                regions = None
                if dets is not None and now - last_full_scan[cam] < FULL_SCAN_EVERY:
                    regions = scan_regions(scanned[cam], dets, frame, now)
                else:
                    last_full_scan[cam] = now
                reads = reader.read(frame, regions) if regions is None or regions else []
                for plate, score, bbox, raw in reads:
                    p = next((p for p in passes[cam] if p.near(bbox, now)), None)
                    if p is None:
                        p = PlatePass(now, bbox)
                        passes[cam].append(p)
                    p.add(bbox, raw, score, frame, now, regions is not None)
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
            watchdog.check({c: g for c, g in grabbers.items() if c in p2p_cams}, now)
            next_status = now + 10

        if now >= next_cleanup:
            cleanup(conn)
            next_cleanup = now + 3600

        time.sleep(max(0.0, INTERVAL - (time.time() - loop_start)))


if __name__ == "__main__":
    main()
