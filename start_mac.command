#!/bin/bash
# CineChat 실행기 (macOS) - 더블클릭으로 실행
cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "  CineChat - AI 영화 예매 플랫폼 실행"
echo "=========================================="
echo

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "[오류] 파이썬이 설치되어 있지 않습니다. https://www.python.org/downloads/ 에서 설치하세요."
  read -r -p "엔터를 누르면 닫힙니다..."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[안내] .env 파일을 새로 만들었습니다. API 키는 나중에 텍스트 편집기로 열어 넣으면 됩니다."
  echo
fi

echo "[1/3] 필요한 프로그램 설치 확인 중... (처음 한 번은 몇 분 걸릴 수 있어요)"
"$PY" -m pip install -q -r requirements.txt 2>/dev/null || "$PY" -m pip install -q --user -r requirements.txt || {
  echo "[오류] 설치에 실패했습니다. 인터넷 연결을 확인하세요."
  read -r -p "엔터를 누르면 닫힙니다..."
  exit 1
}

echo "[2/3] 백엔드 서버 시작"
"$PY" -m uvicorn backend.main:app --port 8000 > backend.log 2>&1 &
BACKEND_PID=$!
trap 'kill $BACKEND_PID 2>/dev/null; echo "CineChat 종료"; exit' INT TERM EXIT

for _ in $(seq 1 20); do
  curl -s http://localhost:8000/api/health > /dev/null 2>&1 && break
  sleep 1
done

echo "[3/3] 화면 시작... 잠시 후 브라우저가 열립니다."
echo
echo "  종료하려면 이 창을 닫거나 Ctrl + C 를 누르세요."
echo
(sleep 3; open "http://localhost:8501" 2>/dev/null) &
"$PY" -m streamlit run frontend/app.py --server.headless true --browser.gatherUsageStats false
