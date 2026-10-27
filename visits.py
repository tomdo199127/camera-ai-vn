"""Đếm người qua lại: nhận ra lại cùng một người bằng model ReID (OSNet) chuyên phân biệt người.

Model nhìn cả quần áo, dáng người, đồ mang theo (xe, mũ, túi…), không cần thấy mặt nên chạy được
khi người quay lưng / đứng xa. Chỉ đáng tin trong ngày: người thay quần áo sẽ thành người mới.
Nếu camera bật khuôn mặt và nhận ra người quen, lượt đó được gắn tên người quen.
"""

import os
import time
from collections import defaultdict
from datetime import datetime

import cv2
import numpy as np
import onnxruntime as ort

import alerts
from alerts import crop_box, same_object, save_event_images
from common import MODELS_DIR

REID_MATCH = float(os.environ.get("LPR_REID_MATCH", 0.88))       # từ mức này: tự gộp vào người cũ
REID_MAYBE = float(os.environ.get("LPR_REID_MAYBE", 0.84))       # từ mức này: gợi ý "có thể trùng" để người dùng gộp
# chỉ so với người gặp trong khoảng này (giờ); chỉnh trong trang Cài đặt
REID_WINDOW_HOURS = float(os.environ.get("LPR_REID_WINDOW_HOURS", 12))
MAX_WINDOW_HOURS = 168
MIN_HEIGHT = int(os.environ.get("LPR_REID_MIN_HEIGHT", 120))     # người thấp hơn (px) thì quá nhỏ để nhận ra
VISIT_GAP = float(os.environ.get("LPR_VISIT_GAP", 60))           # quay lại cùng camera trong khoảng này = vẫn lượt cũ
# tính vector ReID tốn ~200 ms/ảnh: mỗi người chỉ tính tối đa REID_MAX_EMBEDS lần, cách nhau REID_MIN_GAP giây,
# để dành CPU cho đọc biển số và cảnh báo vùng
REID_MAX_EMBEDS = int(os.environ.get("LPR_REID_MAX_EMBEDS", 4))
REID_MIN_GAP = float(os.environ.get("LPR_REID_MIN_GAP", 1.0))
DIM = 512


class ReidModel:
    """OSNet x1.0 (bản ONNX của Qualcomm AI Hub, giấy phép MIT). Ảnh người 256x128 -> vector 512 chiều.

    Thử trên ảnh thật từ 2 camera cổng: cùng một người thường giống 0,80–1,0, khác người thường dưới 0,8,
    nhưng người đi xe máy chụp từ phía sau (ảnh lẫn nhiều xe và nền) có thể giống nhau tới 0,88. Vì vậy chỉ
    tự gộp từ REID_MATCH, khoảng REID_MAYBE–REID_MATCH thì để người dùng xác nhận trên web.
    """

    def __init__(self, threads: int = 1):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(MODELS_DIR / "osnet_x1_0.onnx"), opts, providers=["CPUExecutionProvider"])

    def embed(self, crop):
        x = cv2.resize(crop, (128, 256), interpolation=cv2.INTER_AREA)[:, :, ::-1].astype(np.float32) / 255
        v = self.sess.run(None, {"image": np.ascontiguousarray(x.transpose(2, 0, 1)[None])})[0][0]
        return (v / np.linalg.norm(v)).astype(np.float32)


def good_crop(box, shape) -> bool:
    """Bỏ khung người bị cắt ở mép ảnh hoặc bè ngang (bị che / chỉ thấy một phần): vector của chúng
    quá chung chung, giống với rất nhiều người khác."""
    x1, y1, x2, y2 = box
    h, w = shape[:2]
    if x1 <= 4 or y1 <= 4 or x2 >= w - 4 or y2 >= h - 4:
        return False
    return (y2 - y1) >= MIN_HEIGHT and (y2 - y1) / max(1, x2 - x1) >= 1.2


def near(t, box, now) -> bool:
    """Người đi nhanh: khung mới cách khung cũ xa hơn bình thường nhưng chỉ sau vài giây."""
    if now - t["last_seen"] > 3:
        return False
    a = t["box"]
    diag = ((a[2] - a[0]) ** 2 + (a[3] - a[1]) ** 2) ** 0.5
    dx = (a[0] + a[2] - box[0] - box[2]) / 2
    dy = (a[1] + a[3] - box[1] - box[3]) / 2
    return (dx * dx + dy * dy) ** 0.5 < diag * 0.8


class VisitTracker:
    """Mỗi người được nhớ bằng vector của từng lượt (không lấy trung bình, để một lần gộp nhầm không
    làm "trôi" hình ảnh của người đó). Lượt mới được gộp vào người cũ khi giống ít nhất một lượt trước
    đó từ REID_MATCH trở lên."""

    def __init__(self, conn, camera_names):
        self.conn, self.camera_names = conn, camera_names
        self.model = ReidModel()
        self.tracks = defaultdict(list)   # camera -> [track]
        self.people = {}
        self.cache = None                 # visitor id -> {"embs": [vector mỗi lượt], "last_seen", "pid"}
        self.window = REID_WINDOW_HOURS * 3600

    def load(self, settings: dict | None = None):
        try:
            hours = float((settings or {}).get("reid_window_hours") or REID_WINDOW_HOURS)
        except ValueError:
            hours = REID_WINDOW_HOURS
        self.window = min(MAX_WINDOW_HOURS, max(1.0, hours)) * 3600
        self.people = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM people")}
        self.cache = None  # đọc lại từ DB: người dùng có thể đã đổi tên / gộp / xoá trên web

    def _visitors(self, now):
        if self.cache is None:
            self.cache = {}
            for r in self.conn.execute(
                    "SELECT v.visitor_id, v.embedding, p.last_seen, p.person_id FROM visits v"
                    " JOIN visitors p ON p.id = v.visitor_id WHERE p.last_seen > ? AND v.embedding IS NOT NULL",
                    (now - self.window,)):
                if len(r["embedding"]) != DIM * 4:
                    continue
                c = self.cache.setdefault(r["visitor_id"], {"embs": [], "last_seen": r["last_seen"], "pid": r["person_id"]})
                c["embs"].append(np.frombuffer(r["embedding"], np.float32))
        return self.cache

    def process(self, cam, frame, dets, now):
        ttl = max(20, alerts.HEARTBEAT * 2.5)
        tracks = self.tracks[cam]
        for t in [t for t in tracks if now - t["last_seen"] > ttl]:
            if t["visit_id"] is None and t["desc"] is not None:  # người đi nhanh: quyết định bằng số ảnh đang có
                self._start_visit(cam, t, t["last_seen"])
            self._flush(t)
            tracks.remove(t)
        for group, conf, box in dets:
            if group != "person" or conf < 0.5:
                continue
            t = next((t for t in tracks if same_object(t["box"], box) or near(t, box, now)), None)
            if t is None:
                t = {"box": box, "start": now, "last_seen": now, "hits": 0, "visit_id": None, "visitor_id": None,
                     "own": False, "best_h": 0, "desc": None, "n": 0, "flushed": now, "embed_at": 0.0}
                tracks.append(t)
            t["box"], t["last_seen"] = box, now
            t["hits"] += 1
            height = box[3] - box[1]
            # vector của lượt lấy từ ảnh người to / rõ nhất. Thử trung bình nhiều ảnh thì kém hơn: người đi xe
            # máy ở xa có nhiều nền, kéo mọi người đi xe về giống nhau
            if (good_crop(box, frame.shape) and height > t["best_h"] * 1.15 and t["n"] < REID_MAX_EMBEDS
                    and now - t["embed_at"] >= REID_MIN_GAP):
                t["best_h"], t["embed_at"] = height, now
                t["desc"] = self.model.embed(frame[box[1]:box[3], box[0]:box[2]])
                t["crop"], t["frame"] = crop_box(frame, box, 0.05), frame
                t["n"] += 1
                if t["visit_id"]:
                    self._update_visit(t)
            # đợi vài khung để người đi lại gần (ảnh to hơn) rồi mới quyết định là ai
            ready = t["desc"] is not None and (t["n"] >= 3 or now - t["start"] >= 4 or t["n"] >= REID_MAX_EMBEDS)
            if t["visit_id"] is None and ready and t["hits"] >= 2:
                self._start_visit(cam, t, now)
            elif t["visit_id"] and now - t["flushed"] > 5:
                self._flush(t)

    def link(self, cam, pbox, pid):
        """Khuôn mặt trong khung người pbox được nhận ra là người quen pid."""
        t = next((t for t in self.tracks[cam] if t["visitor_id"] and same_object(t["box"], pbox)), None)
        if t is None:
            return
        cache = self._visitors(time.time())
        mine = cache.get(t["visitor_id"])
        if mine and mine["pid"] == pid:
            return
        # đã có visitor gắn với người quen này trong ngày -> gộp lượt hiện tại vào đó
        other = next((vid for vid, v in cache.items() if v["pid"] == pid and vid != t["visitor_id"]), None)
        if other:
            self.conn.execute("UPDATE visits SET visitor_id = ? WHERE visitor_id = ?", (other, t["visitor_id"]))
            self.conn.execute("DELETE FROM visitors WHERE id = ?", (t["visitor_id"],))
            if mine:
                cache[other]["embs"] += mine["embs"]
            cache.pop(t["visitor_id"], None)
            t["visitor_id"] = other
        else:
            self.conn.execute("UPDATE visitors SET person_id = ?, label = ? WHERE id = ?",
                              (pid, self.people.get(pid, ""), t["visitor_id"]))
            if mine:
                mine["pid"] = pid
        self.conn.commit()

    def _start_visit(self, cam, t, now):
        cache = self._visitors(now)
        best, best_sim = None, 0.0
        for vid, c in cache.items():
            if now - c["last_seen"] > self.window or not c["embs"]:
                continue
            sim = float(max(e @ t["desc"] for e in c["embs"]))
            if sim > best_sim:
                best, best_sim = vid, sim
        emb = t["desc"].tobytes()
        if best is not None and best_sim >= REID_MATCH:
            # người đi nhanh làm mất dấu giữa 2 khung: vừa thấy trên camera này thì nối vào lượt đó
            recent = self.conn.execute(
                "SELECT id FROM visits WHERE visitor_id = ? AND camera = ? AND last_seen > ? ORDER BY ts DESC LIMIT 1",
                (best, cam, now - VISIT_GAP)).fetchone()
            if recent:
                t["visit_id"], t["visitor_id"], t["flushed"] = recent[0], best, now
                t["frame"] = None
                self._flush(t)
                return
        img, frame_img = save_event_images(cam, t["crop"], t["frame"], now, self._draw(t))
        if best is not None and best_sim >= REID_MATCH:
            vid = best
            self.conn.execute("UPDATE visitors SET descriptor = ?, last_seen = ?, img = ? WHERE id = ?",
                              (emb, now, img, vid))
            cache[vid]["embs"].append(t["desc"])
            cache[vid]["last_seen"] = now
            maybe = (None, None)
        else:
            vid = self.conn.execute(
                "INSERT INTO visitors(descriptor, img, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (emb, img, now, now)).lastrowid
            cache[vid] = {"embs": [t["desc"]], "last_seen": now, "pid": None}
            maybe = (best, best_sim) if best is not None and best_sim >= REID_MAYBE else (None, None)
            best_sim = 0.0
        t["visit_id"] = self.conn.execute(
            "INSERT INTO visits(visitor_id, camera, ts, last_seen, similarity, img, frame_img, embedding,"
            " maybe_visitor, maybe_sim) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, cam, now, now, best_sim, img, frame_img, emb, *maybe)).lastrowid
        self.conn.commit()
        t["visitor_id"], t["flushed"], t["own"] = vid, now, True
        t["frame"] = None
        n = self.conn.execute("SELECT COUNT(*) FROM visits WHERE visitor_id = ?", (vid,)).fetchone()[0]
        print(f"{datetime.now():%H:%M:%S}  {cam:<8}  người qua lại #{vid}: lần {n} (giống {best_sim:.2f})", flush=True)

    def _update_visit(self, t):
        """Thấy người rõ hơn: thay ảnh của lượt."""
        r = self.conn.execute("SELECT img, frame_img FROM visits WHERE id = ?", (t["visit_id"],)).fetchone()
        if r is None:  # lượt đã bị xoá trên web
            t["visit_id"] = None
            return
        alerts.overwrite_event_images(self.conn, None, t["crop"], t["frame"], self._draw(t), paths=(r[0], r[1]))
        t["frame"] = None

    def _flush(self, t):
        if t["visit_id"] and t["own"] and t["desc"] is not None:  # vector đã thêm ảnh từ lúc tạo lượt
            self.conn.execute("UPDATE visits SET embedding = ? WHERE id = ?", (t["desc"].tobytes(), t["visit_id"]))
        if t["visit_id"]:
            self.conn.execute("UPDATE visits SET last_seen = MAX(last_seen, ?) WHERE id = ?", (t["last_seen"], t["visit_id"]))
            self.conn.execute("UPDATE visitors SET last_seen = MAX(last_seen, ?) WHERE id = ?",
                              (t["last_seen"], t["visitor_id"]))
            self.conn.commit()
            t["flushed"] = t["last_seen"]

    @staticmethod
    def _draw(t):
        x1, y1, x2, y2 = t["box"]
        return lambda img: cv2.rectangle(img, (x1, y1), (x2, y2), (255, 160, 0), 4)
