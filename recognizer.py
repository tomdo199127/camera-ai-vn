"""Phát hiện + đọc biển số VN và đọc luồng RTSP (tách ra từ lpr.py)."""

import os
import threading
import time

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")  # tắt cảnh báo HEVC lúc chờ keyframe

import cv2
import numpy as np
import onnxruntime as ort
from open_image_models import create_detector
from rapidocr_onnxruntime import RapidOCR

from plates import normalize_plate


class PlateReader:
    def __init__(self, model: str = "yolo-v9-s-608-license-plate-end2end", conf: float = 0.4, threads: int = 2):
        # model "s" bắt biển xe máy nhỏ / ngược nắng tốt hơn hẳn model "t" (0.87 so với 0.19), đổi lại chậm hơn ~1.5x
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        # CoreML lỗi khi frame không có biển nào; CPU là đủ cho model nhỏ này
        self.detector = create_detector(model, conf_thresh=conf, providers=["CPUExecutionProvider"], sess_options=opts)
        # giới hạn ảnh đưa vào model dò chữ: mặc định nó phóng ảnh lên ~736px, mất vài giây trên CPU server
        self.ocr = RapidOCR(det_limit_side_len=320, det_limit_type="max")

    def read(self, frame, regions=None):
        """Trả về list (plate, ocr_score, bbox, raw_text) cho mọi biển phát hiện được trong frame.

        regions: chỉ tìm biển trong các vùng này (vùng quanh xe), None = tìm cả khung.
        plate là None khi thấy biển nhưng không đọc ra chữ đúng dạng biển VN (biển nhỏ / nghiêng / mờ).
        """
        results = []
        h, w = frame.shape[:2]
        boxes = self._detect(frame) if regions is None else self._detect_regions(frame, regions)
        for x1, y1, x2, y2 in boxes:
            # bỏ dòng ngày giờ camera in trên hình (dài ~9 lần cao; biển dài 1 dòng chỉ ~4–5 lần)
            # và khung quá nhỏ, chữ chưa đọc nổi kể cả với AI
            if x2 - x1 > 6 * (y2 - y1) or x2 - x1 < 30:
                continue
            pad_x, pad_y = int((x2 - x1) * 0.08), int((y2 - y1) * 0.15)
            x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue
            raw, score = self._read_text(crop) or ("", 0.0)
            plate = normalize_plate(raw)
            if plate is None or score < 0.85:
                # biển xe máy chụp chéo hay bị nghiêng 15–25°: xoay thẳng theo dòng chữ rồi đọc lại
                angle = text_angle(crop)
                if abs(angle) > 4:
                    raw2, score2 = self._read_text(rotate(crop, angle)) or ("", 0.0)
                    plate2 = normalize_plate(raw2)
                    if plate2 and (plate is None or score2 > score):
                        plate, raw, score = plate2, raw2, score2
            results.append((plate, score, (x1, y1, x2, y2), raw))
        return results

    def _detect(self, frame):
        """Phát hiện biển trên 2 ô trái/phải chồng nhau.

        Model chạy ở 640px: cả khung 1920-2304px bị thu nhỏ khiến biển xe máy (~70px) còn ~25px và bị bỏ sót.
        """
        h, w = frame.shape[:2]
        if w < 1600:
            tiles = [(0, w)]
        else:
            half, overlap = w // 2, 200
            tiles = [(0, half + overlap), (half - overlap, w)]
        boxes = []  # (conf, x1, y1, x2, y2)
        for x0, x1_tile in tiles:
            for det in self.detector.predict(frame[:, x0:x1_tile]):
                bx1, by1, bx2, by2 = det.bounding_box.xyxy
                boxes.append((det.confidence, bx1 + x0, by1, bx2 + x0, by2))
        return dedupe(boxes)

    def _detect_regions(self, frame, regions):
        """Tìm biển trong từng vùng quanh xe. Vùng nhỏ được model phóng lên 608px nên biển xe máy to gấp
        2–3 lần so với khi quét cả khung, và mỗi vùng chỉ tốn bằng một ô thay vì cả khung."""
        boxes = []
        for rx1, ry1, rx2, ry2 in regions:
            for det in self.detector.predict(frame[ry1:ry2, rx1:rx2]):
                bx1, by1, bx2, by2 = det.bounding_box.xyxy
                boxes.append((det.confidence, bx1 + rx1, by1 + ry1, bx2 + rx1, by2 + ry1))
        return dedupe(boxes)


    def _rec(self, img):
        lines, _ = self.ocr(img, use_det=False, use_cls=False)
        return (lines[0][0], float(lines[0][1])) if lines and lines[0][0] else None

    def _read_text(self, crop):
        """Đọc chữ trên ảnh biển, trả (text, score). ~30 ms/biển trên CPU."""
        ch, cw = crop.shape[:2]
        if cw / ch >= 2.5:  # biển dài 1 dòng: chỉ cần model đọc chữ
            return self._rec(crop)
        # biển vuông 2 dòng (xe máy, ô tô biển vuông): dò vùng chữ rồi đọc từng dòng.
        # Biển nhỏ (~60-80px) phải phóng lên ~160px thì model dò chữ mới bắt được dòng, nhất là biển nghiêng.
        scale = max(1.0, 160 / ch)
        big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        lines, _ = self.ocr(big, use_cls=False)
        if lines and len(lines) >= 2:
            lines.sort(key=lambda l: (l[0][0][1] + l[0][2][1]) / 2)
            return "".join(l[1] for l in lines), min(float(l[2]) for l in lines)
        # dự phòng: ghép 2 nửa thành 1 hàng ngang rồi đọc một lần
        mid = int(ch * 0.55)
        top, bottom = crop[:mid], crop[mid:]
        top = cv2.resize(top, (int(top.shape[1] * bottom.shape[0] / top.shape[0]), bottom.shape[0]))
        return self._rec(cv2.hconcat([top, bottom]))


def dedupe(boxes):
    """Bỏ box trùng (ô / vùng chồng nhau), giữ box tin cậy hơn. boxes: (conf, x1, y1, x2, y2)."""
    kept = []
    for box in sorted(boxes, reverse=True):
        if all(iou(box[1:], k[1:]) < 0.3 for k in kept):
            kept.append(box)
    return [k[1:] for k in kept]


def vehicle_regions(dets, shape, min_width=0.05, limit=4):
    """Vùng cần tìm biển: khung xe máy / ô tô (nới rộng ra, nhất là phía dưới nơi gắn biển), gộp các vùng
    chồng nhau. Bỏ xe quá nhỏ (ở xa, biển chưa đọc nổi).

    Xe máy nhìn từ phía sau bị người lái che gần hết nên YOLO thường chỉ thấy người: lấy thêm vùng từ
    giữa người xuống dưới chân, nơi biển xe máy nằm."""
    h, w = shape[:2]
    boxes = []
    for group, conf, (x1, y1, x2, y2) in dets:
        bw, bh = x2 - x1, y2 - y1
        if group in ("bike", "car") and bw >= w * min_width:
            boxes.append([x1 - bw * 0.15, y1 - bh * 0.05, x2 + bw * 0.15, y2 + bh * 0.2])
        elif group == "person" and conf >= 0.5 and bh >= h * 0.12:
            boxes.append([x1 - bw * 0.4, y1 + bh * 0.35, x2 + bw * 0.4, y2 + bh * 0.45])
    boxes = [[max(0, int(b[0])), max(0, int(b[1])), min(w, int(b[2])), min(h, int(b[3]))] for b in boxes]
    merged = []
    for b in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True):
        m = next((m for m in merged if b[0] < m[2] and m[0] < b[2] and b[1] < m[3] and m[1] < b[3]), None)
        if m:
            m[:] = [min(m[0], b[0]), min(m[1], b[1]), max(m[2], b[2]), max(m[3], b[3])]
        else:
            merged.append(b)
    return [tuple(b) for b in merged[:limit]]


def rotate(img, angle):
    """Xoay ảnh quanh tâm, nới khung để không mất góc."""
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def text_angle(crop) -> float:
    """Góc nghiêng (độ) của dòng chữ trên ảnh biển: lấy tâm các khối tối cỡ ký tự, chia 2 dòng,
    kẻ đường thẳng qua tâm mỗi dòng. Không đủ ký tự thì trả 0."""
    h, w = crop.shape[:2]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV,
                               max(11, (min(h, w) // 3) | 1), 8)
    n, _, stats, cent = cv2.connectedComponentsWithStats(th)
    pts = np.float32([cent[i] for i in range(1, n)
                      if 0.15 * h < stats[i][3] < 0.6 * h and stats[i][2] < 0.4 * w and stats[i][4] > 8
                      and stats[i][0] > 0 and stats[i][1] > 0
                      and stats[i][0] + stats[i][2] < w and stats[i][1] + stats[i][3] < h])
    if len(pts) < 4:
        return 0.0
    ys = pts[:, 1]
    mid = (ys.min() + ys.max()) / 2
    rows = [pts[ys < mid], pts[ys >= mid]] if ys.max() - ys.min() > 0.25 * h else [pts]
    angles = []
    for row in rows:
        if len(row) >= 3:
            vx, vy, _, _ = cv2.fitLine(row, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            if vx < 0:
                vx, vy = -vx, -vy
            angles.append(np.degrees(np.arctan2(vy, vx)))
    return float(np.mean(angles)) if angles else 0.0


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


class StreamGrabber(threading.Thread):
    """Đọc RTSP liên tục ở thread riêng, chỉ giữ frame mới nhất để không bị trễ."""

    def __init__(self, name: str, url: str):
        super().__init__(daemon=True)
        self.name, self.url = name, url
        self.frame, self.frame_id = None, 0
        self.connected = False
        self.last_frame_at = None
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.running = True

    def run(self):
        while self.running:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"[{self.name}] không mở được luồng, thử lại sau 5s", flush=True)
                time.sleep(5)
                continue
            print(f"[{self.name}] đã kết nối", flush=True)
            connected_at = time.time()
            while self.running:
                ok, frame = cap.read()
                if not ok:
                    print(f"[{self.name}] mất luồng, đang kết nối lại", flush=True)
                    break
                # bỏ các frame đầu: H.265 chưa có keyframe nên ảnh bị nhoè, OCR đọc sai
                if time.time() - connected_at < 2:
                    continue
                with self.lock:
                    self.frame, self.frame_id = frame, self.frame_id + 1
                    self.connected, self.last_frame_at = True, time.time()
            self.connected = False
            cap.release()
            time.sleep(2)

    def latest(self):
        with self.lock:
            return self.frame, self.frame_id
