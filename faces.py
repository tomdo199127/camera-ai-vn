"""Phát hiện khuôn mặt (SCRFD det_500m) và trích đặc trưng (ArcFace MobileFaceNet) từ bộ buffalo_sc của InsightFace.

Model InsightFace chỉ được phép dùng phi thương mại — đủ cho camera gia đình.
"""

import os

import cv2
import numpy as np
import onnxruntime as ort

from common import MODELS_DIR

FACE_MATCH = float(os.environ.get("LPR_FACE_MATCH", 0.42))  # độ giống tối thiểu để coi là cùng một người

# vị trí chuẩn của 2 mắt, mũi, 2 khoé miệng trên ảnh 112x112 mà ArcFace được huấn luyện
ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)


class FaceAnalyzer:
    def __init__(self, threads: int = 2, det_thresh: float = 0.5):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.log_severity_level = 3  # model ghi sẵn kích thước 640; chạy ở 320 thì ORT cảnh báo mỗi lần
        cpu = ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(MODELS_DIR / "det_500m.onnx"), opts, providers=cpu)
        self.rec = ort.InferenceSession(str(MODELS_DIR / "w600k_mbf.onnx"), opts, providers=cpu)
        self.det_input = self.det.get_inputs()[0].name
        self.rec_input = self.rec.get_inputs()[0].name
        self.det_thresh = det_thresh
        self._anchors = {}

    def detect(self, img, size: int = 320):
        """Trả về list (score, bbox[x1,y1,x2,y2], kps[5x2]) theo toạ độ ảnh gốc, mặt lớn trước."""
        h, w = img.shape[:2]
        scale = size / max(h, w)
        nh, nw = round(h * scale), round(w * scale)
        canvas = np.zeros((size, size, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (size, size), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det_input: blob})
        scores_all, boxes_all, kps_all = [], [], []
        for i, stride in enumerate((8, 16, 32)):
            scores = outs[i].reshape(-1)
            boxes = outs[i + 3].reshape(-1, 4) * stride
            kps = outs[i + 6].reshape(-1, 10) * stride
            centers = self._centers(size // stride, stride)
            pos = scores >= self.det_thresh
            if not pos.any():
                continue
            c = centers[pos]
            b = boxes[pos]
            scores_all.append(scores[pos])
            boxes_all.append(np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], 1))
            kps_all.append((kps[pos].reshape(-1, 5, 2) + c[:, None, :]))
        if not scores_all:
            return []
        scores = np.concatenate(scores_all)
        boxes = np.concatenate(boxes_all) / scale
        kps = np.concatenate(kps_all) / scale
        xywh = np.column_stack([boxes[:, :2], boxes[:, 2:] - boxes[:, :2]])
        idx = np.array(cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), self.det_thresh, 0.4)).reshape(-1)
        faces = [(float(scores[i]), boxes[i], kps[i]) for i in idx]
        return sorted(faces, key=lambda f: -(f[1][2] - f[1][0]))

    def _centers(self, n, stride):
        """Tâm anchor của 1 mức stride (mỗi vị trí có 2 anchor)."""
        if (n, stride) not in self._anchors:
            ys, xs = np.mgrid[:n, :n]
            centers = (np.stack([xs, ys], -1).reshape(-1, 2) * stride).astype(np.float32)
            self._anchors[(n, stride)] = np.repeat(centers, 2, axis=0)
        return self._anchors[(n, stride)]

    def embed(self, img, kps):
        """Căn thẳng mặt theo 5 điểm mốc rồi trích vector đặc trưng 512 chiều (đã chuẩn hoá)."""
        m, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST, method=cv2.LMEDS)
        aligned = cv2.warpAffine(img, m, (112, 112), borderValue=0)
        blob = cv2.dnn.blobFromImage(aligned, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        v = self.rec.run(None, {self.rec_input: blob})[0][0]
        return (v / np.linalg.norm(v)).astype(np.float32)


def is_frontal(kps, bbox) -> bool:
    """Mặt gần chính diện: mũi nằm giữa 2 mắt và giữa mắt-miệng, 2 mắt đủ cách nhau.

    Đầu nghiêng sang bên (xoay trong mặt phẳng) vẫn được, vì các điểm được xoay cho 2 mắt nằm ngang trước.
    Mặt quay ngang / cúi gằm cho vector đặc trưng kém, dễ thành "người lạ" giả hoặc nhận nhầm.
    """
    le, re_ = kps[0], kps[1]
    angle = np.arctan2(re_[1] - le[1], re_[0] - le[0])
    c, s_ = np.cos(-angle), np.sin(-angle)
    le, re_, nose, lm, rm = (kps - le) @ np.array([[c, s_], [-s_, c]])
    eye_dist = re_[0]
    if eye_dist < max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 0.25:
        return False
    if not eye_dist * 0.15 < nose[0] < eye_dist * 0.85:
        return False
    mouth_y = (lm[1] + rm[1]) / 2
    return mouth_y * 0.2 < nose[1] < mouth_y * 0.95


def face_crop(img, bbox, out_size: int = 160):
    """Cắt mặt có lề rộng để người xem dễ nhận ra, thu/phóng về out_size."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    side = max(x2 - x1, y2 - y1) * 1.6
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    a, b = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    c, d = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    crop = img[b:d, a:c]
    s = out_size / max(crop.shape[:2])
    return cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA)


def to_blob(v) -> bytes:
    return np.asarray(v, np.float32).tobytes()


def from_blob(b) -> np.ndarray:
    return np.frombuffer(b, np.float32)
