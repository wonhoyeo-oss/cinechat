# 🎬 CineChat — LLM 기반 대화형 AI 챗봇을 결합한 영화 추천 및 모의 예매 플랫폼

2026학년도 졸업논문(실기) · 동서대학교 컴퓨터공학과 20191527 여원호

자연어 대화만으로 **영화 추천 → 상영 회차 검색 → 좌석 선택 → 포인트 모의 결제**까지 한 번에 처리하는 웹 플랫폼입니다.

## 1. 실행 방법

### 가장 쉬운 방법 (더블클릭)
- **윈도우:** `start_windows.bat` 더블클릭
- **맥:** `start_mac.command` 더블클릭 (처음엔 "확인되지 않은 개발자" 경고가 뜨면 우클릭 → 열기)

필요한 프로그램 설치 → 백엔드 서버 → 화면 실행까지 자동으로 하고 브라우저(http://localhost:8501)가 열립니다.
(사전에 [Python 3.10 이상](https://www.python.org/downloads/) 설치 필요, 윈도우는 설치 시 "Add python.exe to PATH" 체크)

### 직접 실행하는 방법

```bash
pip install -r requirements.txt
cp .env.example .env            # (선택) API 키 입력

# 터미널 1 - 백엔드 REST API 서버
uvicorn backend.main:app --reload --port 8000
#   → http://localhost:8000/docs 에서 Swagger API 문서 확인

# 터미널 2 - 프론트엔드
streamlit run frontend/app.py
#   → http://localhost:8501

# 테스트
pytest -q
```

> API 키가 하나도 없어도 **샘플 영화 15편 + 키워드 기반 챗봇**으로 전체 기능이 동작합니다.
> - `GEMINI_API_KEY`(무료, https://aistudio.google.com 에서 발급)를 넣으면 **Google Gemini 함수 호출(Function Calling) 챗봇**으로 전환됩니다. 무료 호출 한도에 걸리면 키워드 모드로 잠시 대신 답합니다.
> - `TMDB_API_KEY`/`KOBIS_API_KEY`를 넣으면 실제 상영작·박스오피스 데이터로 자동 전환됩니다.

### 반응형 화면
| 화면 폭 | 배치 |
|---|---|
| 1025px 이상 (PC) | 왼쪽 챗봇 · 오른쪽 예매 패널 2단 |
| 641~1024px (태블릿) | 챗봇 위, 예매 패널 아래 1단 + "예매 창으로 이동" 바로가기 |
| 640px 이하 (모바일) | 좌석표를 화면 폭에 맞춰 축소, 영화 카드·예시 질문 2열, 채팅창 높이를 화면 높이에 맞춤 |

## 1-1. 인터넷에 배포하기 (Streamlit Community Cloud, 무료)
화면(Streamlit)이 켜질 때 백엔드(FastAPI)가 없으면 **같은 프로세스 안에서 백엔드를 자동으로 함께 실행**하므로, 서버 하나만 띄워주는 Streamlit Cloud에서도 그대로 동작합니다.

1. GitHub에 이 폴더를 올립니다. (`.env`, `cinechat.db` 는 올리지 않음)
2. https://share.streamlit.io 에서 GitHub 계정으로 로그인 → **Create app** → 저장소 선택
3. **Main file path**: `frontend/app.py`
4. **Advanced settings → Secrets** 에 아래 형식으로 키 입력 → **Deploy**
   ```toml
   GEMINI_API_KEY = "..."
   GEMINI_MODEL = "gemini-4-hydrogen"
   TMDB_API_KEY = "..."
   KOBIS_API_KEY = "..."
   ```
5. 발급된 `https://○○○.streamlit.app` 주소를 공유합니다.

> 무료 서버는 한동안 접속이 없으면 잠들었다가 첫 접속 시 깨어나며(최대 1분), 재시작 시 SQLite DB(회원·예매 내역)가 초기화될 수 있습니다.

## 2. 시스템 구조

```
 [Streamlit 프론트엔드]  ── HTTP(REST, 세션 쿠키) ──▶  [FastAPI 백엔드]
   · 챗봇 채팅창                                       ├─ auth.py      쿠키·세션 인증, PBKDF2 비밀번호 해시
   · 예매 대시보드                                     ├─ services.py  영화/회차/좌석/예매 트랜잭션
   · 좌석 선택 / 결제                                  ├─ chatbot.py   Gemini Function Calling (+ 키워드 폴백)
                                                       ├─ external_api.py  TMDB · KOBIS 연동
                                                       └─ database.py  SQLite (WAL 모드)
```

| 폴더/파일 | 역할 |
|---|---|
| `backend/main.py` | REST API 라우팅, 요청 검증(Pydantic), 예외 처리 |
| `backend/database.py` | 8개 테이블 스키마, 인덱스, DB 연결 |
| `backend/auth.py` | 회원 인증 (HttpOnly 세션 쿠키) |
| `backend/services.py` | 예매·취소·환불·포인트 비즈니스 로직 (동시성 제어) |
| `backend/chatbot.py` | LLM 도구 정의, 도구 실행기, 규칙 기반 폴백 엔진 |
| `backend/external_api.py` | TMDB 상영작/포스터, KOBIS 일별 박스오피스 |
| `backend/seed.py` | 상영관·샘플 영화·상영 회차 자동 생성, 외부 데이터 동기화 |
| `frontend/app.py` | Streamlit UI |
| `tests/test_api.py` | API 통합 테스트 (동시 예매 경쟁 테스트 포함) |

## 3. 데이터베이스 (ERD 요약)

```
users ─┬─< sessions
       ├─< bookings >── screenings >── movies
       │      └─< booking_seats (UNIQUE screening_id+seat_code)
       └─< point_history                  screenings >── theaters
```

## 4. 핵심 구현 포인트

### ① 좌석 동시성 제어 (계획서: "피크 타임 동일 좌석 동시 접근 제어")
1. **`BEGIN IMMEDIATE`** — 예매 트랜잭션 시작 시 쓰기 잠금을 먼저 잡아 요청을 직렬화
2. **`UNIQUE(screening_id, seat_code)`** — 애플리케이션 검사를 우회하더라도 DB가 중복 좌석을 거부 (최후 방어선)
3. **조건부 차감 `UPDATE ... WHERE points >= ?`** + `CHECK(points >= 0)` — 잔액 음수 방지
4. 좌석 확인 → 포인트 차감 → 예매/좌석/결제이력 기록을 **하나의 트랜잭션**으로 묶어 실패 시 전체 ROLLBACK

`tests/test_api.py::test_concurrent_same_seat_only_one_wins` 에서 **20명이 동시에 같은 좌석을 예매하면 정확히 1명만 성공**하는 것을 검증합니다.

### ② LLM Function Calling 챗봇 (Google Gemini)
Google 공식 `google-genai` SDK로 연동합니다. 모델 응답을 대화 기록에 그대로 다시 넣어 Gemini 3의 thought signature(도구 호출 사이 추론 맥락)를 보존하므로, 검색 → 회차 조회 → 결제처럼 여러 번 이어지는 도구 호출도 안정적으로 동작합니다.

LLM이 DB에 직접 접근하지 않고 정의된 7개 도구만 호출합니다 → 없는 영화를 지어내는 환각 방지 + 예매는 검증된 트랜잭션 로직 사용.

| 도구 | 기능 |
|---|---|
| `search_movies` | 제목/장르 검색, 취향 기반 추천 |
| `get_box_office` | 박스오피스 순위 |
| `get_showtimes` | 영화별 상영 회차·잔여석 |
| `get_seat_map` | 좌석 배치 + 추천 연석 |
| `open_booking_screen` | 화면의 좌석 선택 창 열기 (좌석 미리 선택) |
| `book_seats` | 사용자가 동의하면 실제 모의 결제 |
| `get_my_bookings` | 예매 내역·포인트 조회 |

예시 대화: *"비 오는 날 혼자 보기 좋은 영화"*, *"인셉션 내일 저녁 7시 두 명 예매해줘"*, *"응 그걸로 결제해줘"*

### ③ 쿠키·세션 인증
- 비밀번호: PBKDF2-HMAC-SHA256, 사용자별 salt, 200,000회 반복
- 로그인 시 `secrets.token_urlsafe(32)` 토큰을 `sessions` 테이블에 저장, `HttpOnly; SameSite=Lax` 쿠키로 전달 (12시간 만료)

### ④ 외부 데이터 연동
- 서버 시작 시(또는 `POST /api/admin/sync`) TMDB 현재 상영작 + KOBIS 전일 박스오피스를 받아와 DB에 upsert
- KOBIS 영화는 TMDB 제목 검색으로 포스터·줄거리를 보강
- 외부 API 장애 시 기존 데이터로 계속 서비스 (장애 격리)

## 5. 주요 API

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/auth/signup` · `/login` · `/logout` | 회원가입(50,000P 지급) / 로그인 / 로그아웃 |
| GET | `/api/auth/me` | 내 정보 |
| GET | `/api/movies?q=&genre=` · `/api/movies/boxoffice` | 영화 검색 / 박스오피스 |
| GET | `/api/movies/{id}/screenings?date=` | 상영 회차 |
| GET | `/api/screenings/{id}/seats` | 좌석 맵 |
| POST · GET · DELETE | `/api/bookings` · `/api/bookings/{id}` | 예매 / 내역 / 취소(환불) |
| POST · GET | `/api/points/charge` · `/api/points/history` | 포인트 충전 / 이력 |
| POST | `/api/chat` | 챗봇 대화 |

## 6. 참고문헌
- 영화진흥위원회, 영화관입장권통합전산망 오픈 API 서비스 이용 가이드 및 명세서
- The Movie Database (TMDB) API Documentation
- Google, Gemini API Documentation — Function Calling
- OpenAI, GPT-4 Technical Report & API Function Calling Guide
- Streamlit Documentation
