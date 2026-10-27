#!/usr/bin/env bash
# Tải các model AI vào models/ (file đã có thì bỏ qua). install.sh tự gọi script này.
# Model biển số (open-image-models) và OCR (RapidOCR) do thư viện tự tải lúc chạy lần đầu, không cần ở đây.
set -euo pipefail
cd "$(dirname "$0")/../models"
export PATH="$HOME/.local/bin:$PATH"

fetch() {  # fetch <tệp> <url>
  [ -s "$1" ] && return 0
  echo "  tải $1"
  curl -fSL --retry 3 --progress-bar -o "$1.part" "$2"
  mv "$1.part" "$1"
}

unzip_to() {  # unzip_to <zip> <thư mục>; dùng python có sẵn thay cho lệnh unzip (máy chủ có thể chưa cài)
  python3 -m zipfile -e "$1" "$2"
}

# Khuôn mặt: InsightFace buffalo_sc (phát hiện + nhận diện). Giấy phép: chỉ dùng phi thương mại.
if [ ! -s det_500m.onnx ] || [ ! -s w600k_mbf.onnx ]; then
  echo "Model khuôn mặt (InsightFace buffalo_sc, ~16 MB)"
  tmp=$(mktemp -d)
  curl -fSL --retry 3 --progress-bar -o "$tmp/b.zip" https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_sc.zip
  unzip_to "$tmp/b.zip" "$tmp/b"
  find "$tmp/b" \( -name det_500m.onnx -o -name w600k_mbf.onnx \) -exec cp {} . \;
  rm -rf "$tmp"
fi

# Người qua lại: OSNet x1.0 (ReID) từ Qualcomm AI Hub. Trọng số nằm ở osnet.data, phải để cạnh file .onnx.
if [ ! -s osnet_x1_0.onnx ]; then
  echo "Model người qua lại (OSNet, ~9 MB)"
  tmp=$(mktemp -d)
  curl -fSL --retry 3 --progress-bar -o "$tmp/o.zip" \
    https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/osnet/releases/v0.65.0/osnet-onnx-float.zip
  unzip_to "$tmp/o.zip" "$tmp/o"
  cp "$(find "$tmp/o" -name osnet.data)" osnet.data
  cp "$(find "$tmp/o" -name osnet.onnx)" osnet_x1_0.onnx
  rm -rf "$tmp"
fi

# Tìm theo mô tả ("áo trắng", "xe máy vàng"): CLIP ViT-B/32 bản int8 (MIT), ~155 MB
echo "Model tìm theo mô tả (CLIP)"
B=https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main
fetch clip_vision_q.onnx "$B/onnx/vision_model_quantized.onnx"
fetch clip_text_q.onnx "$B/onnx/text_model_quantized.onnx"
fetch clip_vocab.json "$B/vocab.json"
fetch clip_merges.txt "$B/merges.txt"

# Phát hiện người / xe: YOLO11n (Ultralytics, AGPL-3.0). Ultralytics chỉ phát hành bản PyTorch nên phải tự
# xuất sang ONNX: tạo môi trường tạm với PyTorch bản CPU (~1 GB tải về, xoá sau khi xong), làm một lần.
if [ ! -s yolo11n.onnx ]; then
  echo "Model người / xe (YOLO11n): xuất sang ONNX, mất vài phút"
  command -v uv >/dev/null || { echo "Cần uv (install.sh tự cài)"; exit 1; }
  tmp=$(mktemp -d)
  (
    cd "$tmp"
    export UV_CACHE_DIR="$tmp/cache"
    uv venv -q --python 3.12 v
    uv pip install -q --python v/bin/python torch torchvision --index-url https://download.pytorch.org/whl/cpu
    uv pip install -q --python v/bin/python ultralytics onnx onnxslim
    v/bin/yolo export model=yolo11n.pt format=onnx imgsz=640 opset=17 simplify=True > export.log 2>&1 \
      || { tail -20 export.log; exit 1; }
  )
  cp "$tmp/yolo11n.onnx" yolo11n.onnx
  rm -rf "$tmp"
fi

echo "Đủ model:"
ls -lh ./*.onnx ./*.data clip_vocab.json clip_merges.txt | awk '{print "  " $5 "\t" $NF}'
