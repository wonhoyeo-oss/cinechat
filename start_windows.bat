@echo off
chcp 65001 > nul
cd /d "%~dp0"
title CineChat 실행기

echo ==========================================
echo   CineChat - AI 영화 예매 플랫폼 실행
echo ==========================================
echo.

where python > nul 2> nul
if errorlevel 1 (
    echo [오류] 파이썬이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 설치하세요.
    echo 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
    pause
    exit /b
)

if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo [안내] .env 파일을 새로 만들었습니다. API 키는 나중에 메모장으로 열어 넣으면 됩니다.
    echo.
)

echo [1/3] 필요한 프로그램 설치 확인 중... (처음 한 번은 몇 분 걸릴 수 있어요)
python -m pip install -q --only-binary=:all: -r requirements.txt
if errorlevel 1 (
    echo [오류] 설치에 실패했습니다. 인터넷 연결을 확인하세요.
    pause
    exit /b
)

echo [2/3] 백엔드 서버 시작 (새 창이 열립니다. 닫지 마세요)
start "CineChat 백엔드 서버 - 닫지 마세요" cmd /k python -m uvicorn backend.main:app --port 8000

timeout /t 4 /nobreak > nul

echo [3/3] 화면 시작... 잠시 후 브라우저가 열립니다.
start "" "http://localhost:8501"
echo.
echo  종료하려면 이 창과 백엔드 서버 창을 모두 닫으세요.
echo.
python -m streamlit run frontend/app.py --server.headless true --browser.gatherUsageStats false
pause
