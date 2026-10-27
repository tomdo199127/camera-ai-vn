"""Tìm người qua lại bằng mô tả ("áo trắng", "xe máy vàng"…) với model CLIP chạy trên máy.

CLIP đưa ảnh và câu chữ về cùng một không gian vector: ảnh người mặc áo trắng nằm gần câu "a person wearing a
white shirt". Mỗi lượt người qua lại được tính vector ảnh một lần (chạy nền bằng service lpr-search, ưu tiên
thấp nhất để không giành CPU của đọc biển số); khi tìm, web chỉ cần tính vector câu mô tả rồi so.

Model: openai/clip-vit-base-patch32 bản ONNX nén int8 của Xenova (giấy phép MIT). Model chỉ hiểu tiếng Anh nên
mô tả tiếng Việt được dịch bằng bảng từ thường gặp (màu, áo quần, xe, mũ, túi…).
"""

import json
import re
import time
import unicodedata
from functools import lru_cache

import cv2
import numpy as np
import onnxruntime as ort

from common import IMAGES_DIR, MODELS_DIR, connect, init_db

DIM = 512
BATCH = 8
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
SOT, EOT = 49406, 49407


# ---------- tokenizer BPE của CLIP (viết lại từ simple_tokenizer của OpenAI, không cần thư viện ngoài) ----------
def _bytes_to_unicode():
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


class Tokenizer:
    PAT = re.compile(r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|[^\s\w]+", re.I)

    def __init__(self):
        self.encoder = json.loads((MODELS_DIR / "clip_vocab.json").read_text())
        merges = (MODELS_DIR / "clip_merges.txt").read_text().split("\n")[1:]
        self.ranks = {tuple(m.split()): i for i, m in enumerate(merges) if m}
        self.byte_encoder = _bytes_to_unicode()
        self.cache = {}

    def _bpe(self, token):
        if token in self.cache:
            return self.cache[token]
        word = list(token[:-1]) + [token[-1] + "</w>"]
        while len(word) > 1:
            pairs = {(word[i], word[i + 1]) for i in range(len(word) - 1)}
            best = min(pairs, key=lambda p: self.ranks.get(p, float("inf")))
            if best not in self.ranks:
                break
            merged, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and (word[i], word[i + 1]) == best:
                    merged.append(word[i] + word[i + 1])
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = merged
        self.cache[token] = word
        return word

    def encode(self, text, length=77):
        ids = [SOT]
        for tok in self.PAT.findall(" ".join(text.lower().split())):
            tok = "".join(self.byte_encoder[b] for b in tok.encode("utf-8"))
            ids += [self.encoder[t] for t in self._bpe(tok) if t in self.encoder]
        ids = ids[:length - 1] + [EOT]
        return ids + [EOT] * (length - len(ids))  # đệm bằng EOT: model lấy vector tại EOT đầu tiên


class ClipModel:
    def __init__(self, vision=True, text=True, threads=1):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        load = lambda f: ort.InferenceSession(str(MODELS_DIR / f), opts, providers=["CPUExecutionProvider"])
        self.vision = load("clip_vision_q.onnx") if vision else None
        self.text = load("clip_text_q.onnx") if text else None
        self.tokenizer = Tokenizer() if text else None

    def embed_images(self, crops):
        """Ảnh BGR -> vector đã chuẩn hoá (n, 512). Ảnh người cao gầy được đệm thành vuông (không cắt mất
        đầu / chân / xe như cách cắt giữa mặc định của CLIP)."""
        batch = []
        for img in crops:
            h, w = img.shape[:2]
            side = max(h, w)
            canvas = np.full((side, side, 3), (MEAN[::-1] * 255).astype(np.uint8), np.uint8)
            canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = img
            x = cv2.resize(canvas, (224, 224), interpolation=cv2.INTER_CUBIC)[:, :, ::-1].astype(np.float32) / 255
            batch.append(((x - MEAN) / STD).transpose(2, 0, 1))
        v = self.vision.run(None, {"pixel_values": np.stack(batch)})[0]
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def embed_text(self, texts):
        ids = np.array([self.tokenizer.encode(t) for t in texts], np.int64)
        v = self.text.run(None, {"input_ids": ids})[0]
        return v / np.linalg.norm(v, axis=1, keepdims=True)


# ---------- đo màu theo vùng ----------
# CLIP nhận màu chưa chắc (áo trắng / đỏ hay lẫn, "xe máy vàng" ra người mặc áo vàng), nên đo thêm tỉ lệ màu thật
# ở từng vùng của ảnh người rồi cộng vào điểm khi mô tả có màu
BINS = ("white", "black", "grey", "red", "orange", "yellow", "green", "teal", "blue", "purple", "pink", "brown")
REGIONS = {"head": (0.02, 0.2, 0.25, 0.75), "torso": (0.2, 0.52, 0.2, 0.8), "lower": (0.55, 0.95, 0.1, 0.9)}
COLOR_WEIGHT = 0.12


def color_profile(img):
    """Tỉ lệ điểm ảnh thuộc mỗi màu trong BINS ở từng vùng (đầu, thân áo, phần dưới = quần hoặc xe)."""
    h, w = img.shape[:2]
    small = cv2.resize(img, (48, max(8, int(48 * h / w))), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(cv2.GaussianBlur(small, (3, 3), 0), cv2.COLOR_BGR2HSV)
    out = []
    sh, sw = hsv.shape[:2]
    for y1, y2, x1, x2 in REGIONS.values():
        r = hsv[int(y1 * sh):int(y2 * sh), int(x1 * sw):int(x2 * sw)].reshape(-1, 3).astype(np.int32)
        hue, sat, val = r[:, 0], r[:, 1], r[:, 2]
        black = val < 55
        white = (sat < 40) & (val > 170)
        grey = (sat < 40) & ~black & ~white
        chroma = ~(black | white | grey)
        reddish = (hue < 9) | (hue >= 168)
        brown = chroma & (hue < 22) & (val < 120)
        pink = chroma & ~brown & (((hue >= 145) & (hue < 168)) | (reddish & (sat < 110) & (val > 160)))
        masks = [white, black, grey, chroma & reddish & ~brown & ~pink,
                 chroma & ~brown & (hue >= 9) & (hue < 21), chroma & (hue >= 21) & (hue < 35),
                 chroma & (hue >= 35) & (hue < 80), chroma & (hue >= 80) & (hue < 98),
                 chroma & (hue >= 98) & (hue < 130), chroma & (hue >= 130) & (hue < 145), pink, brown]
        out.append([m.mean() if len(r) else 0.0 for m in masks])
    return np.array(out, np.float16)


ENGLISH_BINS = {
    "white": ("white",), "black": ("black",), "grey": ("grey",), "silver": ("grey", "white"), "red": ("red",),
    "dark red": ("red", "brown"), "orange": ("orange",), "yellow": ("yellow",), "green": ("green", "teal"),
    "dark green": ("green",), "teal": ("teal", "green"), "teal green": ("teal", "green"), "blue": ("blue", "teal"),
    "light blue": ("blue", "teal"), "navy blue": ("blue", "black"), "purple": ("purple",), "pink": ("pink",),
    "brown": ("brown",), "beige": ("brown", "white", "orange"), "cream": ("white", "orange"),
    "light colored": ("white", "grey"), "dark colored": ("black", "grey"),
}
ITEM_REGION = {"hat": "head", "helmet": "head", "cap": "head", "pants": "lower", "shorts": "lower",
               "jeans": "lower", "skirt": "lower"}  # còn lại: áo -> thân, xe -> phần dưới


# ---------- dịch mô tả tiếng Việt sang câu tiếng Anh cho CLIP ----------
def _plain(s):
    """Bỏ dấu để gõ không dấu vẫn tìm được ("ao trang" = "áo trắng")."""
    s = unicodedata.normalize("NFD", s.lower().replace("đ", "d"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


COLORS = {
    "trang": "white", "den": "black", "do": "red", "vang": "yellow", "cam": "orange", "hong": "pink",
    "tim": "purple", "nau": "brown", "xam": "grey", "ghi": "grey", "bac": "silver", "kem": "cream",
    "xanh la": "green", "xanh luc": "green", "xanh la cay": "green", "xanh reu": "dark green", "xanh lam": "blue",
    "xanh duong": "blue", "xanh nuoc bien": "blue", "xanh da troi": "light blue", "xanh than": "navy blue",
    "xanh ngoc": "teal", "xanh coban": "blue", "do do": "dark red", "be": "beige", "sang mau": "light colored",
    "toi mau": "dark colored", "soc": "striped", "ke": "checkered", "hoa": "floral",
}
AMBIGUOUS = {"xanh": ("green", "blue")}  # "xanh" một mình: có thể là xanh lá hoặc xanh dương, thử cả hai
CLOTHES = {
    "ao": "shirt", "ao phong": "t-shirt", "ao thun": "t-shirt", "ao so mi": "button-up shirt", "ao khoac": "jacket",
    "ao mua": "raincoat", "ao dai": "ao dai dress", "ao len": "sweater", "ao ba lo": "tank top", "quan": "pants",
    "quan dui": "shorts", "quan short": "shorts", "quan bo": "jeans", "quan jean": "jeans", "vay": "skirt",
    "dam": "dress", "mu": "hat", "non": "hat", "mu bao hiem": "helmet", "non bao hiem": "helmet",
    "mu luoi trai": "cap", "khau trang": "face mask", "kinh": "glasses", "giay": "shoes",
    "balo": "backpack", "ba lo": "backpack", "tui": "bag", "dong phuc": "uniform",
}
VEHICLES = {"xe may": "motorbike", "xe tay ga": "scooter", "xe so": "motorbike", "xe dap": "bicycle",
            "xe dap dien": "electric bicycle", "xe dien": "electric scooter", "o to": "car", "xe hoi": "car",
            "xe": "motorbike", "xe ba gac": "cargo tricycle"}
PEOPLE = {"nu": "woman", "phu nu": "woman", "con gai": "girl", "nam": "man", "dan ong": "man", "con trai": "boy",
          "tre em": "child", "tre con": "child", "em be": "child", "nguoi gia": "elderly person", "ong": "old man",
          "ba": "old woman", "shipper": "delivery rider", "xe om": "motorbike taxi driver"}
# đồng phục tài xế công nghệ dễ nhận ra bằng màu
BRANDS = {"gsm": ("teal green", "jacket"), "xanh sm": ("teal green", "jacket"), "grab": ("green", "jacket"),
          "shopee": ("orange", "jacket"), "gojek": ("green", "jacket"), "be": ("yellow", "jacket")}
ACTIONS = {"di bo": "walking", "cho hang": "carrying goods", "cho nguoi": "carrying a passenger", "cam o": "holding an umbrella",
           "che o": "holding an umbrella", "dat cho": "walking a dog", "chay bo": "running"}
PLURAL = {"clothes", "pants", "shorts", "jeans", "glasses", "shoes"}

PHRASES = {}
for kind, table in (("color", COLORS), ("cloth", CLOTHES), ("vehicle", VEHICLES), ("person", PEOPLE),
                    ("action", ACTIONS)):
    for k, v in table.items():
        PHRASES[k] = (kind, v)
for k, v in BRANDS.items():
    PHRASES.setdefault(k, ("brand", v))  # "be" một mình là màu be; "áo be" cũng vậy
for k, v in AMBIGUOUS.items():
    PHRASES[k] = ("color", v)
MAX_WORDS = max(len(k.split()) for k in PHRASES)


@lru_cache(maxsize=256)
def translate(query: str) -> tuple[tuple[str, ...], tuple]:
    """Mô tả tiếng Việt -> (các câu tiếng Anh, các yêu cầu màu theo vùng [(vùng, (màu…))]).

    Có nhiều câu khi gặp từ nhiều nghĩa như "xanh"; điểm của ảnh lấy theo câu khớp nhất.
    Không nhận ra từ nào thì dùng nguyên câu (người dùng gõ tiếng Anh)."""
    words = re.findall(r"[a-z0-9]+", _plain(query))
    items, who, actions = [], None, []    # items: [kind, tên tiếng Anh, [màu…]]
    loose = []                            # màu đứng một mình ("màu trắng") hoặc đứng trước vật
    i = 0
    while i < len(words):
        for n in range(min(MAX_WORDS, len(words) - i), 0, -1):
            key = " ".join(words[i:i + n])
            if key in PHRASES:
                break
        else:
            i += 1
            continue
        kind, val = PHRASES[key]
        i += n
        if kind == "color":
            # tiếng Việt đặt màu sau vật: "áo trắng", "xe máy vàng"
            (items[-1][2] if items else loose).append(val)
        elif kind in ("cloth", "vehicle"):
            items.append([kind, val, loose])
            loose = []
        elif kind == "brand":  # "áo xanh GSM": màu đồng phục thay cho màu của áo vừa nhắc
            if items and items[-1][0] == "cloth":
                items[-1][2] = [val[0]]
            else:
                items.append(["cloth", val[1], [val[0]]])
        elif kind == "person":
            who = val
        elif kind == "action":
            actions.append(val)
    if loose:
        items.append(["cloth", "clothes", loose])
    if not items and not who and not actions:
        return (f"a photo of a person, {query.strip()}",), ()

    def expand(colors):
        """[màu hoặc tuple màu nhiều nghĩa] -> các cách viết màu."""
        out = [""]
        for c in colors:
            opts = c if isinstance(c, tuple) else (c,)
            out = [f"{o} {x}".strip() for o in out for x in opts]
        return out

    sentences = [""]
    for kind, name, colors in items:
        parts = [f"{c} {name}".strip() for c in expand(colors)]
        verb = "riding a" if kind == "vehicle" else "wearing" if name in PLURAL else "wearing a"
        sentences = [f"{s} {verb} {p}," for s in sentences for p in parts]
    subject = f"a photo of a {who or 'person'}"
    tail = (" " + ", ".join(actions)) if actions else ""
    constraints = []
    for kind, name, colors in items:
        bins = {b for c in colors for opt in (c if isinstance(c, tuple) else (c,)) for b in ENGLISH_BINS.get(opt, ())}
        if bins:
            region = "lower" if kind == "vehicle" else ITEM_REGION.get(name, "torso")
            constraints.append((region, tuple(sorted(bins))))
    return tuple(f"{subject}{s.rstrip(',')}{tail}" for s in sentences[:8]), tuple(constraints)


def color_score(profiles, constraints):
    """profiles: (n, 3, len(BINS)) -> điểm cộng thêm (n,): tỉ lệ màu khớp trung bình trên các yêu cầu."""
    if not constraints:
        return np.zeros(len(profiles), np.float32)
    names = list(REGIONS)
    parts = [profiles[:, names.index(region), [BINS.index(b) for b in bins]].sum(1) for region, bins in constraints]
    return COLOR_WEIGHT * np.minimum(1.0, np.mean(parts, axis=0)).astype(np.float32)


# ---------- service chạy nền: tính vector cho các lượt chưa có ----------
def main():
    init_db()
    conn = connect()
    model = ClipModel(text=False)
    print("CLIP: đã chạy", flush=True)
    while True:
        # chỉ tính khi người đã đi khỏi: ảnh của lượt có thể còn được thay bằng ảnh rõ hơn
        rows = conn.execute("SELECT id, img FROM visits WHERE clip_emb IS NULL AND last_seen < ?"
                            " ORDER BY ts DESC LIMIT ?", (time.time() - 60, BATCH)).fetchall()
        if not rows:
            time.sleep(20)
            continue
        crops, ids = [], []
        for r in rows:
            img = cv2.imread(str(IMAGES_DIR / r["img"])) if r["img"] else None
            if img is None or img.size == 0:
                conn.execute("UPDATE visits SET clip_emb = x'' WHERE id = ?", (r["id"],))  # ảnh đã bị xoá: bỏ qua
                continue
            crops.append(img)
            ids.append(r["id"])
        if crops:
            for vid, v, img in zip(ids, model.embed_images(crops), crops):
                conn.execute("UPDATE visits SET clip_emb = ?, colors = ? WHERE id = ?",
                             (v.astype(np.float16).tobytes(), color_profile(img).tobytes(), vid))
        conn.commit()


if __name__ == "__main__":
    main()
