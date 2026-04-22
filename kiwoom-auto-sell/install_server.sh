#!/usr/bin/env bash
# Kiwoom Auto Sell - Oracle Cloud Ubuntu 22.04/24.04 installer
# 한 번에 모든 세팅: Python + Tailscale + systemd 서비스
set -e

echo ""
echo "=========================================="
echo "  키움 자동매도 서버 설치 스크립트"
echo "=========================================="
echo ""

# 1. 시스템 업데이트 & 필수 패키지
echo "[1/7] 시스템 업데이트 중..."
sudo apt update -qq
sudo apt install -y python3 python3-pip python3-venv unzip curl git ufw

# 2. 방화벽 열기 (Oracle은 iptables 별도 설정 필요)
echo "[2/7] 방화벽 포트 열기..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8765 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || sudo apt install -y iptables-persistent

# 3. Tailscale 설치 (아이폰에서 안전하게 접속하기 위함)
echo "[3/7] Tailscale 설치 중..."
curl -fsSL https://tailscale.com/install.sh | sh

# 4. 프로젝트 디렉토리 확인
APP_DIR="/home/ubuntu/kiwoom-auto-sell"
if [ ! -d "$APP_DIR" ]; then
  echo ""
  echo "[X] $APP_DIR 폴더가 없습니다."
  echo "    zip 파일을 먼저 업로드/해제해 주세요."
  echo "    예: cd ~ && unzip kiwoom-auto-sell.zip"
  exit 1
fi

# 5. Python 가상환경 & 의존성
echo "[4/7] Python 가상환경 구성 중..."
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip --quiet
./venv/bin/pip install -r requirements.txt --quiet

# 6. systemd 서비스 (자동 시작, 재부팅해도 계속 돔)
echo "[5/7] systemd 서비스 등록 중..."
sudo tee /etc/systemd/system/kiwoom-autosell.service > /dev/null <<EOF
[Unit]
Description=Kiwoom Auto Sell Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kiwoom-autosell
sudo systemctl restart kiwoom-autosell

# 7. Tailscale 연결 (최초 1회 계정 인증 필요)
echo "[6/7] Tailscale 인증 시작..."
echo ""
echo "========================================================"
echo "  아래에 뜨는 URL을 휴대폰으로 열어서 승인하세요!"
echo "========================================================"
sudo tailscale up --ssh

# 상태 요약
echo ""
echo "[7/7] 완료!"
echo ""
TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || echo "(Tailscale 연결 확인 필요)")
echo "=========================================="
echo "  설치 완료!"
echo "=========================================="
echo ""
echo "  서버 상태: sudo systemctl status kiwoom-autosell"
echo "  서버 로그: sudo journalctl -u kiwoom-autosell -f"
echo ""
echo "  아이폰/PC에서 접속 주소:"
echo "    http://$TS_IP:8765"
echo ""
echo "  (아이폰에 Tailscale 앱 설치 후 같은 계정으로 로그인 필수)"
echo ""
