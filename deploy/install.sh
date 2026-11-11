#!/usr/bin/env bash
# Cài đặt / cập nhật Camera AI. Chạy bằng user thường (KHÔNG chạy bằng root / sudo).
#
# Cài mới hoặc cập nhật, chỉ một lệnh:
#   curl -fsSL https://raw.githubusercontent.com/tomdo199127/camera-ai-vn/main/deploy/install.sh | bash
#
# Script tự cài gói hệ thống còn thiếu (git, curl, ffmpeg, python3; sẽ hỏi mật khẩu sudo), tải mã về ~/lpr-server,
# cài thư viện, model AI, bật các dịch vụ. Chạy lại bất cứ lúc nào để cập nhật: cấu hình, dữ liệu, camera được giữ nguyên.
set -euo pipefail

REPO_URL="${LPR_REPO:-https://github.com/tomdo199127/camera-ai-vn.git}"
DIR="$HOME/lpr-server"   # các service systemd trỏ tới đây

if [ "$(id -u)" = 0 ]; then
  echo "Đừng chạy bằng root / sudo. Chạy bằng user thường, script tự hỏi sudo khi cần."
  exit 1
fi

# --- gói hệ thống ---
missing=()
for c in git curl ffmpeg python3; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null && command -v sudo >/dev/null; then
    echo "Cài gói còn thiếu: ${missing[*]} (cần mật khẩu sudo)"
    sudo apt-get update -qq < /dev/tty
    sudo apt-get install -y "${missing[@]}" < /dev/tty
  else
    echo "Thiếu: ${missing[*]}. Cài các gói này rồi chạy lại."
    exit 1
  fi
fi

# --- mã nguồn ---
self="${BASH_SOURCE[0]:-}"
if [ -z "$self" ] || [ "$(cd "$(dirname "$self")/.." 2>/dev/null && pwd -P)" != "$(cd "$DIR" 2>/dev/null && pwd -P)" ]; then
  # chạy qua curl | bash (hoặc từ chỗ khác): tải / cập nhật mã ở ~/lpr-server rồi chạy bản trong đó
  if [ -d "$DIR/.git" ]; then
    echo "Cập nhật mã nguồn ở $DIR"
    git -C "$DIR" pull --ff-only
  elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    echo "$DIR đã có và không phải bản tải từ git. Đổi tên thư mục đó rồi chạy lại."
    exit 1
  else
    echo "Tải mã nguồn về $DIR"
    git clone --depth 1 "$REPO_URL" "$DIR"
  fi
  exec bash "$DIR/deploy/install.sh" < /dev/null
fi

cd "$DIR"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

GO2RTC_VERSION=v1.9.14
if [ ! -x go2rtc/go2rtc ]; then
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;   # Raspberry Pi 4/5 (hệ điều hành 64-bit), mini PC ARM
    *) echo "Chưa hỗ trợ CPU $(uname -m)"; exit 1 ;;
  esac
  curl -fsSL -o go2rtc/go2rtc "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${arch}"
  chmod +x go2rtc/go2rtc
fi

[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
# rapidocr kéo theo opencv-python bản có giao diện, xung đột với bản headless: cài riêng, không kèm phụ thuộc
uv pip install --python .venv/bin/python --no-deps rapidocr_onnxruntime==1.4.4

bash scripts/download_models.sh

if [ ! -f .env ]; then
  sed -e "s|/home/USER/|$HOME/|" .env.example > .env
  chmod 600 .env
fi
if [ ! -f go2rtc/go2rtc.yaml ]; then
  cp go2rtc/go2rtc.example.yaml go2rtc/go2rtc.yaml
fi
chmod 600 go2rtc/go2rtc.yaml

UNITS="go2rtc lpr-worker lpr-web lpr-ai lpr-search lpr-recorder"
mkdir -p "$HOME/.config/systemd/user"
cp deploy/*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable $UNITS
systemctl --user restart $UNITS
systemctl --user --no-pager status $UNITS | grep -E "●|Active:" || true

# chạy cả khi đăng xuất và tự chạy lại sau khi khởi động máy
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "Bật tự chạy sau khi khởi động máy (cần mật khẩu sudo):"
  if ! sudo loginctl enable-linger "$USER" < /dev/tty; then
    echo "  Chưa bật được. Tự chạy một lần: sudo loginctl enable-linger $USER"
  fi
fi

port=$(grep -E '^WEB_PORT=' .env | cut -d= -f2)
ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Xong. Mở web: http://${ip:-<IP máy chủ>}:${port:-8080}"
if grep -qE '^WEB_PASSWORD=123456Aa@' .env; then
  echo "Tài khoản mặc định: admin / 123456Aa@"
  echo "!! Đăng nhập rồi đổi mật khẩu ngay: menu Thêm → Đổi mật khẩu."
fi
if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "Tường lửa ufw đang bật: mở cổng web cho mạng trong nhà, ví dụ"
  echo "  sudo ufw allow from 192.168.1.0/24 to any port ${port:-8080} proto tcp"
fi
