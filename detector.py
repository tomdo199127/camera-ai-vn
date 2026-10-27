"""Phát hiện người / xe bằng YOLO11n (COCO, xuất sang ONNX) cho cảnh báo vùng và nhận diện khuôn mặt."""

import cv2
import numpy as np
import onnxruntime as ort

from common import MODELS_DIR

# lớp COCO cần dùng -> nhóm hiển thị trên web
CLASS_GROUPS = {0: "person", 1: "bike", 2: "car", 3: "bike", 5: "car", 7: "car"}
GROUP_LABELS = {"person": "Người", "car": "Ô tô", "bike": "Xe máy / xe đạp"}


class ObjectDetector:
    def __init__(self, conf: float = 0.4, threads: int = 2, size: int = 640):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(str(MODELS_DIR / "yolo11n.onnx"), opts, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name
        self.conf, self.size = conf, size

    def detect(self, frame):
        """Trả về list (group, conf, (x1, y1, x2, y2)) theo toạ độ frame gốc."""
        h, w = frame.shape[:2]
        scale = self.size / max(h, w)
        nh, nw = round(h * scale), round(w * scale)
        canvas = np.full((self.size, self.size, 3), 114, np.uint8)
        canvas[:nh, :nw] = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255, swapRB=True)
        out = self.sess.run(None, {self.input: blob})[0][0].T  # (8400, 4 + 80)
        scores = out[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = (conf >= self.conf) & np.isin(cls, list(CLASS_GROUPS))
        if not keep.any():
            return []
        out, cls, conf = out[keep], cls[keep], conf[keep]
        cx, cy, bw, bh = (out[:, i] / scale for i in range(4))
        boxes = np.stack([cx - bw / 2, cy - bh / 2, bw, bh], 1)
        # NMS theo từng lớp: dịch box mỗi lớp ra xa nhau để NMS không gộp người với xe
        shifted = boxes.copy()
        shifted[:, :2] += cls[:, None] * 10000
        idx = cv2.dnn.NMSBoxes(shifted.tolist(), conf.tolist(), self.conf, 0.5)
        results = []
        for i in np.array(idx).reshape(-1):
            x, y, bw_, bh_ = boxes[i]
            x1, y1 = max(0, int(x)), max(0, int(y))
            x2, y2 = min(w, int(x + bw_)), min(h, int(y + bh_))
            results.append((CLASS_GROUPS[int(cls[i])], float(conf[i]), (x1, y1, x2, y2)))
        return results
