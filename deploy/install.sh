#!/usr/bin/env bash
# Cài đặt / cập nhật (chạy bằng user thường, không cần sudo). Project phải nằm ở ~/lpr-server
# vì các service systemd trỏ tới đó. Chạy lại bất cứ lúc nào để cập nhật, cấu hình và dữ liệu được giữ nguyên.
set -euo pipefail
cd "$HOME/lpr-server"
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

UNITS="go2rtc lpr-worker lpr-web lpr-ai lpr-search"
mkdir -p "$HOME/.config/systemd/user"
cp deploy/*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable $UNITS
systemctl --user restart $UNITS
systemctl --user --no-pager status $UNITS | grep -E "●|Active:"

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "Lưu ý: để hệ thống chạy cả khi bạn đăng xuất / sau khi khởi động lại máy, chạy một lần:"
  echo "  sudo loginctl enable-linger $USER"
fi

port=$(grep -E '^WEB_PORT=' .env | cut -d= -f2)
echo
echo "Mở web: http://$(hostname -I | awk '{print $1}'):${port:-8080}"
if grep -qE '^WEB_PASSWORD=123456Aa@' .env; then
  echo "Đăng nhập: admin / 123456Aa@"
  echo "!! Đang dùng mật khẩu mặc định. Đổi dòng WEB_PASSWORD trong ~/lpr-server/.env rồi chạy:"
  echo "   systemctl --user restart lpr-web"
fi
