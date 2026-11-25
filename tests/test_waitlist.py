"""매진 대기열 · 취소표 자동 예매 테스트.

검증 항목
1. 잔여석이 있으면 대기 신청 불가, 같은 회차 중복 대기 불가
2. 취소표는 신청 순서(FIFO)대로 자동 예매 + 자동 결제 + 알림
3. 좌석 수가 맞지 않는 대기자는 순서를 유지한 채 건너뜀 (좌석 수 적합 우선)
4. 포인트가 부족한 대기자는 실패 처리 후 다음 대기자에게 배정
5. 끼어들기 방지: 취소 직후 대기하지 않은 사용자는 반환 좌석을 예매할 수 없음
6. 동시성: 대기자 40명 + 취소 5건 + 끼어들기 시도 20건이 동시에 일어나도 중복 배정 0건, 순서 위반 0건
"""
import os
import tempfile
import threading

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "waitlist.db"))
os.environ["SKIP_EXTERNAL_SYNC"] = "1"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("GEMINI_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from backend import auth, chatbot, services
from backend.database import get_conn
from backend.main import app

auth.PBKDF2_ROUNDS = 1_000      # 테스트 속도를 위해 해시 반복 횟수만 낮춤 (동작은 동일)
_counter = iter(range(1, 1_000_000))


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def user(prefix="u"):
    c = TestClient(app)
    name = f"wl_{prefix}{next(_counter)}"
    r = c.post("/api/auth/signup", json={"username": name, "password": "pass1234", "nickname": name})
    assert r.status_code == 201, r.text
    c.uid = r.json()["id"]
    return c


_used = set()


def private_screening(client):
    """아직 아무도 쓰지 않은 4관(10석) 회차 하나를 고른다."""
    with get_conn() as conn:
        for s in services.list_screenings(conn):
            if s["total_seats"] == 10 and s["id"] not in _used and s["remaining_seats"] == 10:
                _used.add(s["id"])
                return s
    pytest.skip("사용 가능한 10석 회차가 없음")


def fill(client, scr, per_user=1):
    """회차를 매진시키고 (사용자 클라이언트, 예매) 목록을 돌려준다."""
    with get_conn() as conn:
        free = services.suggest_seats(services.seat_map(conn, scr["id"]), 10)
    holders = []
    for i in range(0, 10, per_user):
        c = user("h")
        r = c.post("/api/bookings", json={"screening_id": scr["id"], "seats": free[i:i + per_user]})
        assert r.status_code == 201, r.text
        holders.append((c, r.json()))
    assert client.get(f"/api/screenings/{scr['id']}/seats").json()["remaining"] == 0
    return holders


def points(c):
    return c.get("/api/auth/me").json()["points"]


def test_join_rules(client):
    scr = private_screening(client)
    a = user()
    r = a.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})
    assert r.status_code == 409, "잔여석이 있으면 대기 신청 불가"
    fill(client, scr)
    assert a.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1}).status_code == 201
    assert a.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1}).status_code == 409
    assert a.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 5}).status_code == 422
    assert TestClient(app).post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1}).status_code == 401


def test_fifo_auto_booking_and_notification(client):
    scr = private_screening(client)
    holders = fill(client, scr)
    w1, w2, w3 = user("w"), user("w"), user("w")
    for w in (w1, w2, w3):
        assert w.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1}).status_code == 201
    assert [x["position"] for x in w3.get("/api/waitlist").json()] == [3]

    c, b = holders[0]
    res = c.delete(f"/api/bookings/{b['id']}").json()
    assert res["reassigned_to_waitlist"] == 1

    wl1 = w1.get("/api/waitlist").json()[0]
    assert wl1["status"] == "FULFILLED"
    assert points(w1) == 50_000 - scr["price"]                         # 자동 결제
    assert w1.get("/api/bookings").json()[0]["seats"] == b["seats"]    # 반환된 좌석이 그대로 배정
    assert "자동 예매 완료" in w1.get("/api/notifications").json()[0]["message"]
    assert w2.get("/api/waitlist").json()[0]["position"] == 1           # 순번이 앞당겨짐
    assert w3.get("/api/waitlist").json()[0]["status"] == "WAITING"


def test_seat_count_fit_keeps_order(client):
    scr = private_screening(client)
    holders = fill(client, scr, per_user=1)
    big, small = user("big"), user("small")
    big.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 2})
    small.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})

    holders[0][0].delete(f"/api/bookings/{holders[0][1]['id']}")       # 1석 반환
    assert small.get("/api/waitlist").json()[0]["status"] == "FULFILLED"
    assert big.get("/api/waitlist").json()[0]["status"] == "WAITING"
    assert big.get("/api/waitlist").json()[0]["position"] == 1

    holders[1][0].delete(f"/api/bookings/{holders[1][1]['id']}")       # 1석만 비어 있음 → 2석 대기자는 계속 대기
    assert big.get("/api/waitlist").json()[0]["status"] == "WAITING"
    holders[2][0].delete(f"/api/bookings/{holders[2][1]['id']}")       # 2석 확보 → 배정
    assert big.get("/api/waitlist").json()[0]["status"] == "FULFILLED"
    assert len(big.get("/api/bookings").json()[0]["seats"]) == 2


def test_insufficient_points_fails_and_moves_on(client):
    scr = private_screening(client)
    holders = fill(client, scr)
    poor, rich = user("poor"), user("rich")
    poor.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})
    rich.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})
    # 대기 신청 후 포인트를 다 써버린 상황
    other = private_screening(client)
    poor.post("/api/bookings", json={"screening_id": other["id"], "seats": ["A1", "A2"]})
    assert points(poor) < scr["price"]

    holders[0][0].delete(f"/api/bookings/{holders[0][1]['id']}")
    p = poor.get("/api/waitlist").json()[0]
    assert p["status"] == "FAILED" and "포인트" in p["fail_reason"]
    assert rich.get("/api/waitlist").json()[0]["status"] == "FULFILLED"
    assert "실패" in poor.get("/api/notifications").json()[0]["message"]


def test_no_cutting_in_line(client):
    """취소와 재배정이 한 트랜잭션이라, 취소 직후 다른 사용자가 그 좌석을 예매할 수 없다."""
    scr = private_screening(client)
    holders = fill(client, scr)
    waiter, sniper = user("wait"), user("snipe")
    waiter.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1})
    c, b = holders[0]
    c.delete(f"/api/bookings/{b['id']}")
    r = sniper.post("/api/bookings", json={"screening_id": scr["id"], "seats": b["seats"]})
    assert r.status_code == 409
    assert waiter.get("/api/waitlist").json()[0]["status"] == "FULFILLED"


def test_concurrent_cancels_joins_and_snipers(client):
    """대기자 40명이 순서대로 줄을 선 뒤, 취소 5건과 끼어들기 시도 20건이 동시에 발생."""
    scr = private_screening(client)
    holders = fill(client, scr)
    waiters = [user("cw") for _ in range(40)]
    for w in waiters:                                   # 신청 순서 = 리스트 순서
        assert w.post("/api/waitlist", json={"screening_id": scr["id"], "seat_count": 1}).status_code == 201
    snipers = [user("cs") for _ in range(20)]
    seats_all = [b["seats"][0] for _, b in holders]
    results = {"snipe_ok": 0}
    barrier = threading.Barrier(5 + 20)

    def cancel(c, b):
        barrier.wait()
        assert c.delete(f"/api/bookings/{b['id']}").status_code == 200

    def snipe(c, i):
        barrier.wait()
        for _ in range(3):
            r = c.post("/api/bookings", json={"screening_id": scr["id"], "seats": [seats_all[i % 10]]})
            if r.status_code == 201:
                results["snipe_ok"] += 1

    threads = [threading.Thread(target=cancel, args=h) for h in holders[:5]]
    threads += [threading.Thread(target=snipe, args=(s, i)) for i, s in enumerate(snipers)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    statuses = [w.get("/api/waitlist").json()[0]["status"] for w in waiters]
    assert results["snipe_ok"] == 0, "대기자가 있는 동안 끼어들기 예매 성공 0건"
    assert statuses[:5] == ["FULFILLED"] * 5, "먼저 신청한 5명이 배정"
    assert statuses[5:] == ["WAITING"] * 35, "나머지는 순서 유지"
    with get_conn() as conn:
        seats = conn.execute("SELECT seat_code FROM booking_seats WHERE screening_id=?", (scr["id"],)).fetchall()
    assert len(seats) == 10 and len({s[0] for s in seats}) == 10, "중복 배정 0건, 매진 상태 유지"


def test_chatbot_waitlist_tool(client):
    scr = private_screening(client)
    fill(client, scr)
    u = user("bot")
    with get_conn() as conn:
        runner = chatbot.ToolRunner(conn, {"id": u.uid, "nickname": "bot", "points": 50_000})
        sm = runner.run("get_seat_map", {"screening_id": scr["id"]})
        assert sm["sold_out"] is True
        out = runner.run("join_waitlist", {"screening_id": scr["id"], "seat_count": 1})
        assert out["ok"] and out["waitlist"]["position"] == 1
        assert runner.action["type"] == "waitlisted"
        assert runner.run("get_my_waitlist", {})["waitlist"][0]["status"] == "WAITING"
        assert "error" in chatbot.ToolRunner(conn, None).run("join_waitlist", {"screening_id": scr["id"], "seat_count": 1})
