#!/usr/bin/env bash
# 키움 자동매도 실행 스크립트 (macOS / Linux)
set -e

cd "$(dirname "$0")"

# Python 확인
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "❌ Python이 설치되어 있지 않습니다. https://www.python.org 에서 먼저 설치해 주세요."
  exit 1
fi

# 가상환경 없으면 생성
if [ ! -d "venv" ]; then
  echo "📦 가상환경 생성중..."
  $PY -m venv venv
fi

# 가상환경 활성화
# shellcheck disable=SC1091
source venv/bin/activate

# 의존성 설치 (이미 설치되어 있으면 빠르게 통과)
echo "📦 의존성 확인중..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo ""
echo "================================================"
echo "🚀 키움 자동매도 서버 시작"
echo "📍 브라우저에서 http://localhost:8000 접속"
echo "🛑 종료: Ctrl+C"
echo "================================================"
echo ""

# 서버 실행
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
