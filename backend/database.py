"""SQLite 연결 및 스키마 정의.

테이블 구조 (3정규형 기준)
- users           : 회원 정보 + 가상 포인트 잔액
- sessions        : 쿠키 기반 로그인 세션
- movies          : 영화 메타데이터 (TMDB / KOBIS / 샘플)
- theaters        : 상영관 (좌석 행·열 수)
- screenings      : 상영 회차 (영화 × 상영관 × 시작시각)
- bookings        : 예매 내역 (한 번의 결제 단위)
- booking_seats   : 예매된 좌석. UNIQUE(screening_id, seat_code) 로 중복 예매를 DB 레벨에서 차단
- point_history   : 포인트 충전/결제/환불 이력 (가상 결제 로그)
"""
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "cinechat.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    nickname      TEXT    NOT NULL,
    points        INTEGER NOT NULL DEFAULT 0 CHECK (points >= 0),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS movies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL DEFAULT 'sample',     -- sample | tmdb | kobis
    external_id  TEXT,
    title        TEXT NOT NULL,
    genres       TEXT NOT NULL DEFAULT '',            -- 쉼표 구분
    overview     TEXT NOT NULL DEFAULT '',
    release_year INTEGER,
    runtime      INTEGER,
    rating       REAL,
    poster_url   TEXT,
    box_rank     INTEGER,                             -- 박스오피스 순위 (없으면 NULL)
    audience_acc INTEGER,                             -- 누적 관객수 (KOBIS)
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS theaters (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    seat_rows  INTEGER NOT NULL,
    seat_cols  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS screenings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id   INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    theater_id INTEGER NOT NULL REFERENCES theaters(id),
    starts_at  TEXT    NOT NULL,                      -- 'YYYY-MM-DD HH:MM'
    price      INTEGER NOT NULL,
    UNIQUE (theater_id, starts_at)
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    screening_id INTEGER NOT NULL REFERENCES screenings(id),
    total_price  INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'PAID' CHECK (status IN ('PAID','CANCELLED')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS booking_seats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    screening_id INTEGER NOT NULL REFERENCES screenings(id),
    seat_code    TEXT    NOT NULL,
    UNIQUE (screening_id, seat_code)
);

CREATE TABLE IF NOT EXISTS point_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,                      -- +충전/환불, -결제
    reason     TEXT    NOT NULL,
    booking_id INTEGER REFERENCES bookings(id),
    created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_screenings_movie ON screenings(movie_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_bookings_user    ON bookings(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_seats_screening  ON booking_seats(screening_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
"""


def connect() -> sqlite3.Connection:
    # isolation_level=None → 트랜잭션을 BEGIN/COMMIT 으로 직접 제어
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # 읽기와 쓰기를 동시에 허용
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_db():
    """FastAPI 의존성 주입용"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
