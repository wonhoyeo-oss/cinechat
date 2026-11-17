"""API 통합 테스트 (외부 API·OpenAI 없이 실행됨).

실행: pytest -q
"""
import os
import tempfile
import threading

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SKIP_EXTERNAL_SYNC"] = "1"
os.environ.pop("OPENAI_API_KEY", None)

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def new_user(client, name):
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={"username": name, "password": "pass1234", "nickname": name})
    assert r.status_code == 201, r.text
    return c


def future_screening(client):
    movie = client.get("/api/movies").json()[0]
    shows = client.get(f"/api/movies/{movie['id']}/screenings").json()
    assert shows, "예매 가능한 회차가 있어야 함"
    return shows[0]


def test_movies_and_boxoffice(client):
    movies = client.get("/api/movies").json()
    assert len(movies) >= 10
    assert client.get("/api/movies/boxoffice").json()[0]["box_rank"] == 1
    assert all("로맨스" in m["genres"] for m in client.get("/api/movies", params={"genre": "로맨스"}).json())


def test_auth_cookie_flow(client):
    c = new_user(client, "alice")
    assert "cinechat_session" in c.cookies
    assert c.get("/api/auth/me").json()["points"] == 50_000
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "alice", "password": "pass1234"}).status_code == 200
    assert c.get("/api/auth/me").status_code == 200


def test_booking_requires_login(client):
    s = future_screening(client)
    r = TestClient(app).post("/api/bookings", json={"screening_id": s["id"], "seats": ["A1"]})
    assert r.status_code == 401


def test_book_cancel_refund(client):
    c = new_user(client, "bob")
    s = future_screening(client)
    r = c.post("/api/bookings", json={"screening_id": s["id"], "seats": ["c3", "C4"]})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["seats"] == ["C3", "C4"] and b["total_price"] == s["price"] * 2
    assert c.get("/api/auth/me").json()["points"] == 50_000 - s["price"] * 2
    assert "C3" in c.get(f"/api/screenings/{s['id']}/seats").json()["taken"]

    # 같은 좌석 재예매 → 409
    other = new_user(client, "carol")
    assert other.post("/api/bookings", json={"screening_id": s["id"], "seats": ["C4"]}).status_code == 409

    # 취소 → 환불 + 좌석 반환
    assert c.delete(f"/api/bookings/{b['id']}").json()["status"] == "CANCELLED"
    assert c.get("/api/auth/me").json()["points"] == 50_000
    assert "C3" not in c.get(f"/api/screenings/{s['id']}/seats").json()["taken"]


def test_invalid_seats_and_insufficient_points(client):
    c = new_user(client, "dave")
    s = future_screening(client)
    assert c.post("/api/bookings", json={"screening_id": s["id"], "seats": ["Z99"]}).status_code == 400
    seats = [f"H{i}" for i in range(1, 9)]  # 8석
    c.post("/api/bookings", json={"screening_id": s["id"], "seats": seats})       # 96,000원 > 50,000P
    r = c.post("/api/bookings", json={"screening_id": s["id"], "seats": seats})
    assert r.status_code == 402
    assert c.get("/api/auth/me").json()["points"] == 50_000  # 실패 시 포인트 그대로 (롤백)


def test_concurrent_same_seat_only_one_wins(client):
    """20명이 동시에 같은 좌석을 예매 → 정확히 1명만 성공해야 한다."""
    s = future_screening(client)
    users = [new_user(client, f"race{i}") for i in range(20)]
    results = []
    barrier = threading.Barrier(len(users))

    def attempt(c):
        barrier.wait()
        results.append(c.post("/api/bookings", json={"screening_id": s["id"], "seats": ["E6", "E7"]}).status_code)

    threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 1, results
    assert results.count(409) == 19, results
    seats = client.get(f"/api/screenings/{s['id']}/seats").json()["taken"]
    assert seats.count("E6") == 1


def test_chatbot_rule_engine(client):
    c = new_user(client, "erin")
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "우울한데 웃긴 영화 추천해줘"}]}).json()
    assert r["engine"] == "rule" and any("코미디" in m["genres"] for m in r["movies"])

    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "요즘 인기 영화 뭐야?"}]}).json()
    assert "1위" in r["reply"]

    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "인셉션 내일 저녁 7시 두 명 예매해줘"}]}).json()
    assert r["action"]["type"] == "open_booking" and len(r["action"]["seats"]) == 2

    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "내 예매 내역 보여줘"}]}).json()
    assert "50,000P" in r["reply"]


def test_tool_runner_book_seats(client):
    """LLM 이 호출하는 book_seats 도구가 실제 트랜잭션 예매 로직을 사용하는지 확인"""
    from backend import chatbot
    from backend.database import get_conn
    s = future_screening(client)
    with get_conn() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username='erin'").fetchone()[0]
        runner = chatbot.ToolRunner(conn, {"id": uid, "nickname": "erin", "points": 50_000})
        out = runner.run("book_seats", {"screening_id": s["id"], "seats": ["F1"]})
        assert out["ok"] and out["booking"]["seats"] == ["F1"]
        assert "error" in runner.run("book_seats", {"screening_id": s["id"], "seats": ["F1"]})
        assert "error" in chatbot.ToolRunner(conn, None).run("book_seats", {"screening_id": s["id"], "seats": ["F2"]})
