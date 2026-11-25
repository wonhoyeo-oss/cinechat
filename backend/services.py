"""비즈니스 로직: 영화 조회, 상영 회차, 좌석 맵, 예매(트랜잭션), 포인트.

[동시성 제어 전략]
1) BEGIN IMMEDIATE : 트랜잭션 시작 시점에 쓰기 잠금(RESERVED lock)을 획득 →
   같은 시점에 들어온 다른 예매 요청은 busy_timeout 동안 대기했다가 순서대로 처리된다.
2) booking_seats 의 UNIQUE(screening_id, seat_code) 제약 :
   애플리케이션 검사를 우회하더라도 DB 가 동일 좌석의 두 번째 INSERT 를 거부한다 (최후 방어선).
3) users.points CHECK(points >= 0) + 'WHERE points >= ?' 조건부 UPDATE : 잔액 음수 방지.
좌석 확인 → 포인트 차감 → 예매/좌석/결제이력 기록이 하나의 트랜잭션이므로
중간에 실패하면 ROLLBACK 되어 '돈만 빠지고 좌석은 없는' 상태가 생기지 않는다.

[매진 대기열 · 취소표 자동 예매]
- 매진(또는 원하는 좌석 수보다 잔여석이 적은) 회차에 대기 신청을 받는다 (waitlist 테이블).
- 예매가 취소되면 '좌석 반환 → 대기자에게 재배정 → 자동 결제'를 취소와 같은 트랜잭션에서 처리한다.
  → 취소와 재배정 사이에 틈이 없으므로, 대기하지 않은 사용자가 반환된 좌석을 가로챌 수 없다(끼어들기 방지).
- 배정 순서는 신청 순서(FIFO). 단, 맨 앞 대기자가 원하는 좌석 수만큼 자리가 나지 않았으면
  그 사람은 순서를 유지한 채 다음 대기자 중 조건이 맞는 사람에게 먼저 배정한다(좌석 수 적합 우선).
- 대기자의 포인트가 부족하면 그 신청은 실패 처리하고 알림을 남긴 뒤 다음 대기자로 넘어간다.
"""
import re
import sqlite3
from datetime import datetime

MAX_SEATS_PER_BOOKING = 8
MAX_WAITLIST_SEATS = 4
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
        "waiting": waitlist_count(conn, screening_id),     # 이 회차의 대기자 수
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
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _book_in_tx(conn: sqlite3.Connection, user_id: int, scr: dict, seats: list[str], reason: str) -> int:
    """이미 열린 트랜잭션 안에서 좌석 확인 → 포인트 차감 → 예매 기록을 수행하고 예매 번호를 돌려준다.
    일반 예매(create_booking)와 대기열 자동 예매(_process_waitlist)가 같은 규칙을 공유한다."""
    screening_id = scr["id"]
    total = scr["price"] * len(seats)
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
        (user_id, -total, f"{reason} ({scr['title']} {len(seats)}석)", booking_id),
    )
    return booking_id


def create_booking(conn: sqlite3.Connection, user_id: int, screening_id: int, seats: list[str]) -> dict:
    scr = get_screening(conn, screening_id)
    if not scr:
        raise BookingError("존재하지 않는 상영 회차입니다.", 404)
    if scr["starts_at"] <= _now_str():
        raise BookingError("이미 시작된 상영 회차는 예매할 수 없습니다.")
    seats = _validate_seats(scr, seats)

    conn.execute("BEGIN IMMEDIATE")          # ← 쓰기 잠금 선점
    try:
        booking_id = _book_in_tx(conn, user_id, scr, seats, "예매 결제")
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
        # 반환된 좌석을 같은 트랜잭션 안에서 대기자에게 즉시 재배정 (끼어들기 방지)
        assigned = _process_waitlist(conn, b["screening_id"])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    result = get_booking(conn, booking_id, user_id)
    result["reassigned_to_waitlist"] = len(assigned)
    return result


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


# ───────────────────────── 좌석 추천 ─────────────────────────
def suggest_seats(seatmap: dict, count: int) -> list[str]:
    """빈 좌석 중 스크린 중앙·약간 뒤쪽의 연속 좌석을 고른다. 연석이 없으면 가운데에 가까운 빈자리를 모은다."""
    rows, cols, taken = seatmap["rows"], seatmap["cols"], set(seatmap["taken"])
    best_rows = sorted(rows, key=lambda r: abs(rows.index(r) - (len(rows) * 0.6)))
    center = (cols + 1) / 2
    for r in best_rows:
        starts = sorted(range(1, cols - count + 2), key=lambda c: abs(c + (count - 1) / 2 - center))
        for c in starts:
            block = [f"{r}{c + i}" for i in range(count)]
            if not taken.intersection(block):
                return block
    free = [f"{r}{c}" for r in best_rows for c in sorted(range(1, cols + 1), key=lambda c: abs(c - center))
            if f"{r}{c}" not in taken]
    return free[:count]


# ───────────────────────── 매진 대기열 ─────────────────────────
def _notify(conn, user_id: int, message: str):
    conn.execute("INSERT INTO notifications(user_id, message) VALUES (?,?)", (user_id, message))


def _expire_started(conn):
    """상영이 시작된 회차의 대기 신청은 만료 처리한다. (트랜잭션 안에서 호출)"""
    rows = conn.execute(
        """SELECT w.id, w.user_id, m.title, s.starts_at FROM waitlist w
             JOIN screenings s ON s.id = w.screening_id JOIN movies m ON m.id = s.movie_id
            WHERE w.status = 'WAITING' AND s.starts_at <= ?""", (_now_str(),)
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE waitlist SET status='EXPIRED', closed_at=datetime('now','localtime') WHERE id=?",
                     (r["id"],))
        _notify(conn, r["user_id"], f"'{r['title']}' {r['starts_at']} 회차가 시작되어 대기 신청이 만료되었어요.")


def _process_waitlist(conn, screening_id: int) -> list[dict]:
    """반환된 좌석을 대기자에게 신청 순서(FIFO)대로 배정하고 자동 결제한다. 반드시 트랜잭션 안에서 호출한다.
    원하는 좌석 수만큼 자리가 없는 대기자는 순서를 유지한 채 건너뛰고(좌석 수 적합 우선),
    포인트가 부족한 대기자는 실패 처리 후 다음 대기자로 넘어간다."""
    scr = get_screening(conn, screening_id)
    if not scr or scr["starts_at"] <= _now_str():
        return []
    assigned = []
    waiting = conn.execute(
        "SELECT * FROM waitlist WHERE screening_id = ? AND status = 'WAITING' ORDER BY id", (screening_id,)
    ).fetchall()
    for w in waiting:
        sm = seat_map(conn, screening_id)
        if sm["remaining"] == 0:
            break
        if sm["remaining"] < w["seat_count"]:
            continue                        # 순서는 유지한 채, 더 적은 좌석을 원하는 다음 대기자 확인
        seats = suggest_seats(sm, w["seat_count"])
        conn.execute("SAVEPOINT wl")        # 이 대기자 한 명의 결제만 되돌릴 수 있게 저장점 설정
        try:
            booking_id = _book_in_tx(conn, w["user_id"], scr, seats, "취소표 자동 예매")
        except BookingError as e:
            conn.execute("ROLLBACK TO wl")
            conn.execute("RELEASE wl")
            conn.execute(
                "UPDATE waitlist SET status='FAILED', fail_reason=?, closed_at=datetime('now','localtime') "
                "WHERE id=?", (e.message, w["id"]),
            )
            _notify(conn, w["user_id"],
                    f"'{scr['title']}' {scr['starts_at']} 취소표가 나왔지만 자동 예매에 실패했어요: {e.message}")
            continue
        conn.execute("RELEASE wl")
        conn.execute(
            "UPDATE waitlist SET status='FULFILLED', booking_id=?, closed_at=datetime('now','localtime') "
            "WHERE id=?", (booking_id, w["id"]),
        )
        _notify(conn, w["user_id"],
                f"🎉 취소표 자동 예매 완료! '{scr['title']}' {scr['starts_at']} {scr['theater']} "
                f"좌석 {', '.join(seats)} ({scr['price'] * len(seats):,}P 결제)")
        assigned.append({"waitlist_id": w["id"], "user_id": w["user_id"], "booking_id": booking_id, "seats": seats})
    return assigned


def join_waitlist(conn, user_id: int, screening_id: int, seat_count: int) -> dict:
    if not 1 <= seat_count <= MAX_WAITLIST_SEATS:
        raise BookingError(f"대기 신청은 1~{MAX_WAITLIST_SEATS}석까지 가능합니다.")
    scr = get_screening(conn, screening_id)
    if not scr:
        raise BookingError("존재하지 않는 상영 회차입니다.", 404)
    if scr["starts_at"] <= _now_str():
        raise BookingError("이미 시작된 상영 회차에는 대기 신청을 할 수 없습니다.")
    need = scr["price"] * seat_count
    conn.execute("BEGIN IMMEDIATE")
    try:
        if seat_map(conn, screening_id)["remaining"] >= seat_count:
            raise BookingError("아직 남은 좌석이 있어요. 대기 없이 바로 예매할 수 있습니다.", 409)
        if conn.execute("SELECT 1 FROM waitlist WHERE user_id=? AND screening_id=? AND status='WAITING'",
                        (user_id, screening_id)).fetchone():
            raise BookingError("이미 이 회차에 대기 신청을 하셨어요.", 409)
        points = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()[0]
        if points < need:
            raise BookingError(f"자동 결제를 위해 {need:,}P 이상이 필요해요. (현재 {points:,}P)", 402)
        wid = conn.execute(
            "INSERT INTO waitlist(user_id, screening_id, seat_count) VALUES (?,?,?)",
            (user_id, screening_id, seat_count),
        ).lastrowid
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise BookingError("이미 이 회차에 대기 신청을 하셨어요.", 409)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_waitlist_entry(conn, wid, user_id)


def cancel_waitlist(conn, user_id: int, waitlist_id: int) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE waitlist SET status='CANCELLED', closed_at=datetime('now','localtime') "
            "WHERE id=? AND user_id=? AND status='WAITING'", (waitlist_id, user_id))
        if cur.rowcount == 0:
            raise BookingError("취소할 수 있는 대기 신청이 없습니다.", 404)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_waitlist_entry(conn, waitlist_id, user_id)


def get_waitlist_entry(conn, waitlist_id: int, user_id: int) -> dict:
    row = conn.execute(
        """SELECT w.*, s.starts_at, s.price, m.title, t.name AS theater,
                  (SELECT COUNT(*) FROM waitlist w2 WHERE w2.screening_id = w.screening_id
                       AND w2.status = 'WAITING' AND w2.id <= w.id) AS position
             FROM waitlist w JOIN screenings s ON s.id = w.screening_id
             JOIN movies m ON m.id = s.movie_id JOIN theaters t ON t.id = s.theater_id
            WHERE w.id = ? AND w.user_id = ?""", (waitlist_id, user_id)
    ).fetchone()
    if not row:
        raise BookingError("대기 신청을 찾을 수 없습니다.", 404)
    d = dict(row)
    if d["status"] != "WAITING":
        d["position"] = None
    return d


def my_waitlist(conn, user_id: int) -> list[dict]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _expire_started(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    ids = conn.execute("SELECT id FROM waitlist WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
    return [get_waitlist_entry(conn, r[0], user_id) for r in ids]


def waitlist_count(conn, screening_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM waitlist WHERE screening_id=? AND status='WAITING'",
                        (screening_id,)).fetchone()[0]


# ───────────────────────── 알림 ─────────────────────────
def my_notifications(conn, user_id: int, only_unread: bool = False, limit: int = 20) -> list[dict]:
    sql = "SELECT id, message, is_read, created_at FROM notifications WHERE user_id=?"
    if only_unread:
        sql += " AND is_read=0"
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def mark_notifications_read(conn, user_id: int) -> int:
    return conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (user_id,)).rowcount
