"""비즈니스 로직: 영화 조회, 상영 회차, 좌석 맵, 예매(트랜잭션), 포인트.

[동시성 제어 전략]
1) BEGIN IMMEDIATE : 트랜잭션 시작 시점에 쓰기 잠금(RESERVED lock)을 획득 →
   같은 시점에 들어온 다른 예매 요청은 busy_timeout 동안 대기했다가 순서대로 처리된다.
2) booking_seats 의 UNIQUE(screening_id, seat_code) 제약 :
   애플리케이션 검사를 우회하더라도 DB 가 동일 좌석의 두 번째 INSERT 를 거부한다 (최후 방어선).
3) users.points CHECK(points >= 0) + 'WHERE points >= ?' 조건부 UPDATE : 잔액 음수 방지.
좌석 확인 → 포인트 차감 → 예매/좌석/결제이력 기록이 하나의 트랜잭션이므로
중간에 실패하면 ROLLBACK 되어 '돈만 빠지고 좌석은 없는' 상태가 생기지 않는다.
"""
import re
import sqlite3
from datetime import datetime

MAX_SEATS_PER_BOOKING = 8
SEAT_RE = re.compile(r"^([A-Z])(\d{1,2})$")


class BookingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _movie_dict(row) -> dict:
    d = dict(row)
    d["genres"] = [g for g in (d.get("genres") or "").split(",") if g]
    return d


# ───────────────────────── 영화 ─────────────────────────
def list_movies(conn, query: str | None = None, genre: str | None = None, limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM movies WHERE 1=1"
    params: list = []
    if query:
        sql += " AND (title LIKE ? OR overview LIKE ?)"
        params += [f"%{query}%", f"%{query}%"]
    if genre:
        sql += " AND (',' || genres || ',') LIKE ?"
        params.append(f"%,{genre},%")
    sql += " ORDER BY box_rank IS NULL, box_rank, rating DESC, id LIMIT ?"
    params.append(limit)
    return [_movie_dict(r) for r in conn.execute(sql, params).fetchall()]


def get_movie(conn, movie_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    return _movie_dict(row) if row else None


def box_office(conn, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM movies WHERE box_rank IS NOT NULL ORDER BY box_rank LIMIT ?", (limit,)
    ).fetchall()
    return [_movie_dict(r) for r in rows]


def all_genres(conn) -> list[str]:
    genres = set()
    for (g,) in conn.execute("SELECT genres FROM movies").fetchall():
        genres.update(x for x in (g or "").split(",") if x)
    return sorted(genres)


# ───────────────────────── 상영 회차 / 좌석 ─────────────────────────
def list_screenings(conn, movie_id: int | None = None, date: str | None = None,
                    only_future: bool = True) -> list[dict]:
    sql = """
        SELECT s.id, s.movie_id, m.title, s.starts_at, s.price, t.name AS theater,
               t.seat_rows * t.seat_cols AS total_seats,
               t.seat_rows * t.seat_cols - (SELECT COUNT(*) FROM booking_seats b WHERE b.screening_id = s.id)
                   AS remaining_seats
          FROM screenings s
          JOIN theaters t ON t.id = s.theater_id
          JOIN movies   m ON m.id = s.movie_id
         WHERE 1=1"""
    params: list = []
    if movie_id:
        sql += " AND s.movie_id = ?"
        params.append(movie_id)
    if date:
        sql += " AND substr(s.starts_at, 1, 10) = ?"
        params.append(date)
    if only_future:
        sql += " AND s.starts_at > ?"
        params.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    sql += " ORDER BY s.starts_at, t.id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_screening(conn, screening_id: int) -> dict | None:
    row = conn.execute(
        """SELECT s.*, t.name AS theater, t.seat_rows, t.seat_cols, m.title
             FROM screenings s JOIN theaters t ON t.id = s.theater_id JOIN movies m ON m.id = s.movie_id
            WHERE s.id = ?""",
        (screening_id,),
    ).fetchone()
    return dict(row) if row else None


def seat_map(conn, screening_id: int) -> dict:
    scr = get_screening(conn, screening_id)
    if not scr:
        raise BookingError("존재하지 않는 상영 회차입니다.", 404)
    taken = [r[0] for r in conn.execute(
        "SELECT seat_code FROM booking_seats WHERE screening_id = ?", (screening_id,)
    ).fetchall()]
    rows = [chr(ord("A") + i) for i in range(scr["seat_rows"])]
    return {
        "screening": scr,
        "rows": rows,
        "cols": scr["seat_cols"],
        "taken": sorted(taken),
        "remaining": scr["seat_rows"] * scr["seat_cols"] - len(taken),
    }


def _validate_seats(scr: dict, seats: list[str]) -> list[str]:
    if not seats:
        raise BookingError("좌석을 1개 이상 선택해 주세요.")
    norm = sorted({s.strip().upper() for s in seats})
    if len(norm) > MAX_SEATS_PER_BOOKING:
        raise BookingError(f"한 번에 최대 {MAX_SEATS_PER_BOOKING}석까지 예매할 수 있습니다.")
    for code in norm:
        m = SEAT_RE.match(code)
        if not m:
            raise BookingError(f"잘못된 좌석 번호입니다: {code}")
        row_idx = ord(m.group(1)) - ord("A")
        col = int(m.group(2))
        if not (0 <= row_idx < scr["seat_rows"] and 1 <= col <= scr["seat_cols"]):
            raise BookingError(f"상영관에 없는 좌석입니다: {code}")
    return norm


# ───────────────────────── 예매 / 결제 ─────────────────────────
def create_booking(conn: sqlite3.Connection, user_id: int, screening_id: int, seats: list[str]) -> dict:
    scr = get_screening(conn, screening_id)
    if not scr:
        raise BookingError("존재하지 않는 상영 회차입니다.", 404)
    if scr["starts_at"] <= datetime.now().strftime("%Y-%m-%d %H:%M"):
        raise BookingError("이미 시작된 상영 회차는 예매할 수 없습니다.")
    seats = _validate_seats(scr, seats)
    total = scr["price"] * len(seats)

    conn.execute("BEGIN IMMEDIATE")          # ← 쓰기 잠금 선점
    try:
        placeholders = ",".join("?" * len(seats))
        taken = [r[0] for r in conn.execute(
            f"SELECT seat_code FROM booking_seats WHERE screening_id = ? AND seat_code IN ({placeholders})",
            (screening_id, *seats),
        ).fetchall()]
        if taken:
            raise BookingError(f"이미 예매된 좌석이 있습니다: {', '.join(sorted(taken))}", 409)

        cur = conn.execute(
            "UPDATE users SET points = points - ? WHERE id = ? AND points >= ?", (total, user_id, total)
        )
        if cur.rowcount == 0:
            raise BookingError("포인트가 부족합니다. 포인트를 충전해 주세요.", 402)

        booking_id = conn.execute(
            "INSERT INTO bookings(user_id, screening_id, total_price) VALUES (?,?,?)",
            (user_id, screening_id, total),
        ).lastrowid
        conn.executemany(
            "INSERT INTO booking_seats(booking_id, screening_id, seat_code) VALUES (?,?,?)",
            [(booking_id, screening_id, s) for s in seats],
        )
        conn.execute(
            "INSERT INTO point_history(user_id, amount, reason, booking_id) VALUES (?,?,?,?)",
            (user_id, -total, f"예매 결제 ({scr['title']} {len(seats)}석)", booking_id),
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise BookingError("다른 사용자가 방금 같은 좌석을 예매했습니다. 좌석을 다시 선택해 주세요.", 409)
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return get_booking(conn, booking_id, user_id)


def cancel_booking(conn: sqlite3.Connection, user_id: int, booking_id: int) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        b = conn.execute(
            """SELECT b.*, s.starts_at FROM bookings b JOIN screenings s ON s.id = b.screening_id
                WHERE b.id = ? AND b.user_id = ?""",
            (booking_id, user_id),
        ).fetchone()
        if not b:
            raise BookingError("예매 내역을 찾을 수 없습니다.", 404)
        if b["status"] != "PAID":
            raise BookingError("이미 취소된 예매입니다.")
        if b["starts_at"] <= datetime.now().strftime("%Y-%m-%d %H:%M"):
            raise BookingError("상영이 시작된 예매는 취소할 수 없습니다.")
        conn.execute("UPDATE bookings SET status = 'CANCELLED' WHERE id = ?", (booking_id,))
        conn.execute("DELETE FROM booking_seats WHERE booking_id = ?", (booking_id,))   # 좌석 반환
        conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (b["total_price"], user_id))
        conn.execute(
            "INSERT INTO point_history(user_id, amount, reason, booking_id) VALUES (?,?,?,?)",
            (user_id, b["total_price"], "예매 취소 환불", booking_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_booking(conn, booking_id, user_id)


def get_booking(conn, booking_id: int, user_id: int) -> dict:
    row = conn.execute(
        """SELECT b.id, b.status, b.total_price, b.created_at, s.id AS screening_id, s.starts_at,
                  m.title, m.poster_url, t.name AS theater
             FROM bookings b
             JOIN screenings s ON s.id = b.screening_id
             JOIN movies m     ON m.id = s.movie_id
             JOIN theaters t   ON t.id = s.theater_id
            WHERE b.id = ? AND b.user_id = ?""",
        (booking_id, user_id),
    ).fetchone()
    if not row:
        raise BookingError("예매 내역을 찾을 수 없습니다.", 404)
    d = dict(row)
    seats = conn.execute(
        "SELECT seat_code FROM booking_seats WHERE booking_id = ? ORDER BY seat_code", (booking_id,)
    ).fetchall()
    d["seats"] = [s[0] for s in seats]  # 취소된 예매는 좌석이 반환되어 빈 목록
    return d


def my_bookings(conn, user_id: int) -> list[dict]:
    ids = conn.execute(
        "SELECT id FROM bookings WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)
    ).fetchall()
    return [get_booking(conn, r[0], user_id) for r in ids]


def charge_points(conn, user_id: int, amount: int) -> int:
    if amount <= 0 or amount > 1_000_000:
        raise BookingError("충전 금액은 1원 이상 100만원 이하만 가능합니다.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE users SET points = points + ? WHERE id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO point_history(user_id, amount, reason) VALUES (?,?,?)", (user_id, amount, "가상 포인트 충전")
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute("SELECT points FROM users WHERE id = ?", (user_id,)).fetchone()[0]


def point_history(conn, user_id: int, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT amount, reason, booking_id, created_at FROM point_history WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?", (user_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]
