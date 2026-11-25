"""메신저(텔레그램) 알림 테스트 — 실제 텔레그램 대신 가짜 Bot API 로 검증.

1. 연결: 1회용 코드 링크 생성 → '/start <코드>' 메시지로 대화방 연결
2. 발송: 예매 완료 · 취소 · 대기 신청 · 취소표 자동 예매 알림이 연결된 사용자에게만 전송
3. 장애 대응: 텔레그램 실패 시 예매는 정상 처리되고, 알림은 남아 있다가 다음에 재발송
4. 기존 DB 마이그레이션: 예전 테이블에 새 컬럼이 자동 추가
"""
import os
import sqlite3
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "tg.db"))
os.environ["SKIP_EXTERNAL_SYNC"] = "1"

import pytest
from fastapi.testclient import TestClient

from backend import auth, database, notifier, services
from backend.database import get_conn
from backend.main import app

auth.PBKDF2_ROUNDS = 1_000
_n = iter(range(1, 10**6))


class FakeTelegram:
    def __init__(self):
        self.sent, self.updates, self.fail = [], [], False

    def __call__(self, method, **p):
        if method == "getMe":
            return {"username": "cinechat_test_bot"}
        if method == "getUpdates":
            return self.updates
        if method == "sendMessage":
            if self.fail:
                raise RuntimeError("telegram down")
            self.sent.append((str(p["chat_id"]), p["text"]))
            return {"message_id": len(self.sent)}
        raise AssertionError(method)


@pytest.fixture()
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setattr(notifier, "_api", fake)
    monkeypatch.setattr(notifier, "_bot_username", None)
    # 테스트에서는 백그라운드 스레드 대신 즉시 발송해 결과를 바로 확인
    monkeypatch.setattr(notifier, "dispatch_async", notifier.dispatch_pending)
    return fake


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def user():
    c = TestClient(app)
    name = f"tg_{next(_n)}"
    r = c.post("/api/auth/signup", json={"username": name, "password": "pass1234", "nickname": name})
    c.uid = r.json()["id"]
    return c


def link(c, tg, chat_id):
    r = c.post("/api/telegram/link").json()
    assert r["url"].startswith("https://t.me/cinechat_test_bot?start=")
    assert c.post("/api/telegram/confirm").json() == {"linked": False}       # 아직 '시작'을 안 누름
    tg.updates.append({"update_id": len(tg.updates) + 1,
                       "message": {"text": f"/start {r['code']}", "chat": {"id": chat_id}}})
    assert c.post("/api/telegram/confirm").json() == {"linked": True}
    assert c.get("/api/auth/me").json()["telegram_linked"] == 1


def big_screening(c):
    m = c.get("/api/movies").json()[0]
    return [s for s in c.get(f"/api/movies/{m['id']}/screenings").json() if s["total_seats"] >= 96][0]


def test_link_and_booking_messages(client, tg):
    u = user()
    link(u, tg, 1001)
    assert "연결되었어요" in tg.sent[-1][1]
    scr = big_screening(u)
    b = u.post("/api/bookings", json={"screening_id": scr["id"], "seats": ["H11"]}).json()
    assert tg.sent[-1][0] == "1001" and "예매 완료" in tg.sent[-1][1] and "H11" in tg.sent[-1][1]
    u.delete(f"/api/bookings/{b['id']}")
    assert "취소" in tg.sent[-1][1] and "환불" in tg.sent[-1][1]
    # 본인 행동 알림은 사이트 팝업(안 읽은 알림)으로는 띄우지 않음
    assert u.get("/api/notifications", params={"unread": "true"}).json() == []


def test_unlinked_user_gets_no_message(client, tg):
    u = user()
    scr = big_screening(u)
    u.post("/api/bookings", json={"screening_id": scr["id"], "seats": ["H10"]})
    assert tg.sent == []


def test_waitlist_auto_booking_message(client, tg):
    with get_conn() as conn:
        scr = next(s for s in services.list_screenings(conn) if s["total_seats"] == 10 and s["remaining_seats"] == 10)
    holders = []
    for seat in [f"{r}{c}" for r in "AB" for c in range(1, 6)]:
        h = user()
        holders.append(h.post("/api/bookings", json={"screening_id": scr["id"], "seats": [seat]}).json())
        holders[-1]["client"] = h
    w = user()
    link(w, tg, 2002)
    w.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})
    assert "대기 신청 완료" in tg.sent[-1][1] and "1번째" in tg.sent[-1][1]
    holders[0]["client"].delete(f"/api/bookings/{holders[0]['id']}")
    msgs = [t for cid, t in tg.sent if cid == "2002"]
    assert "취소표 자동 예매 완료" in msgs[-1]


def test_telegram_failure_does_not_break_booking(client, tg):
    u = user()
    link(u, tg, 3003)
    tg.fail = True
    scr = big_screening(u)
    r = u.post("/api/bookings", json={"screening_id": scr["id"], "seats": ["H9"]})
    assert r.status_code == 201                                     # 예매는 정상
    with get_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND sent_at IS NULL",
                               (u.uid,)).fetchone()[0]
    assert pending == 1                                             # 못 보낸 알림은 남아 있음
    tg.fail = False
    assert notifier.dispatch_pending() == 1                         # 다음 기회에 재발송
    assert "H9" in tg.sent[-1][1]


def test_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    u = user()
    assert u.post("/api/telegram/link").status_code == 503
    assert client.get("/api/health").json()["telegram"] is False


def test_migration_adds_columns(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, salt TEXT,
                         nickname TEXT, points INTEGER, created_at TEXT);
                         CREATE TABLE notifications (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT,
                         is_read INTEGER DEFAULT 0, created_at TEXT);""")
    old.close()
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    conn = sqlite3.connect(path)
    assert {"telegram_chat_id", "telegram_link_code"} <= {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    assert "sent_at" in {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
