"""[논문 실험] 취소표 자동 예매: 제안 방식 vs 비교 방식

제안 방식 (atomic)  : 예매 취소 트랜잭션 안에서 반환 좌석을 대기자에게 바로 재배정한다.
비교 방식 (polling) : 취소는 먼저 커밋하고, 별도 작업자가 일정 주기(기본 100ms)마다 대기열을 확인해 배정한다.
                      (일반적인 '배치/폴링' 구현을 흉내 낸 것)

시나리오 (1회 시행)
  - 10석 상영관을 매진시킨 뒤, 대기자 N명이 1석씩 순서대로 대기 신청
  - 예매자 5명이 동시에 취소하고, 같은 순간 대기하지 않은 사용자 20명이 반환 좌석을 노려 반복 예매 시도
측정 지표
  - 끼어들기 성공 수 : 대기자가 아닌 사용자가 반환 좌석을 가져간 수 (0이어야 공정)
  - 순서 위반 수     : 늦게 신청한 대기자가 먼저 신청한 대기자보다 먼저 배정된 쌍의 수 (0이어야 FIFO)
  - 중복 배정 수     : 같은 좌석이 두 번 팔린 수 (0이어야 무결성)
  - 대기자 배정 수   : 반환 좌석 5석 중 대기자에게 간 좌석 수
  - 취소 평균 응답시간(ms)

실행:  python experiments/waitlist_experiment.py            (기본: N = 10, 50, 100, 200 / 각 5회)
       python experiments/waitlist_experiment.py --quick    (빠른 확인용)
결과:  화면에 마크다운 표 출력 + experiments/results/waitlist_results.csv 저장
"""
import argparse
import csv
import os
import random
import statistics
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["SKIP_EXTERNAL_SYNC"] = "1"

from backend import database, seed, services  # noqa: E402

CANCELS = 5
SNIPERS = 20


def new_db():
    database.DB_PATH = os.path.join(tempfile.mkdtemp(), "exp.db")
    database.init_db()
    with database.get_conn() as conn:
        seed.bootstrap(conn, use_external=False)
        scr = next(s for s in services.list_screenings(conn) if s["total_seats"] == 10)
    return scr


def make_users(conn, n, prefix, points=1_000_000):
    ids = []
    for i in range(n):
        ids.append(conn.execute(
            "INSERT INTO users(username, password_hash, salt, nickname, points) VALUES (?,?,?,?,?)",
            (f"{prefix}{i}_{random.random():.6f}", "x", "x", f"{prefix}{i}", points)).lastrowid)
    return ids


def naive_cancel(conn, user_id, booking_id):
    """비교 방식: 대기열 처리 없이 취소만 커밋."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        b = conn.execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (booking_id, user_id)).fetchone()
        conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=?", (booking_id,))
        conn.execute("DELETE FROM booking_seats WHERE booking_id=?", (booking_id,))
        conn.execute("UPDATE users SET points = points + ? WHERE id=?", (b["total_price"], user_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def run_trial(method: str, n_waiters: int, poll_ms: int = 100) -> dict:
    scr = new_db()
    sid = scr["id"]
    with database.get_conn() as conn:
        conn.execute("BEGIN")
        holders = make_users(conn, 10, "h")
        waiters = make_users(conn, n_waiters, "w")
        snipers = make_users(conn, SNIPERS, "s")
        conn.execute("COMMIT")
        free = services.suggest_seats(services.seat_map(conn, sid), 10)
        bookings = [services.create_booking(conn, uid, sid, [seat])["id"] for uid, seat in zip(holders, free)]
        for uid in waiters:                                   # 신청 순서 = waiters 순서
            services.join_waitlist(conn, uid, sid, 1)

    stop = threading.Event()
    barrier = threading.Barrier(CANCELS + SNIPERS)
    latencies, sniped = [], []
    lock = threading.Lock()

    def canceller(uid, bid):
        conn = database.connect()
        barrier.wait()
        t0 = time.perf_counter()
        if method == "atomic":
            services.cancel_booking(conn, uid, bid)
        else:
            naive_cancel(conn, uid, bid)
        with lock:
            latencies.append((time.perf_counter() - t0) * 1000)
        conn.close()

    def sniper(uid):
        conn = database.connect()
        barrier.wait()
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline and not stop.is_set():
            seat = random.choice(free)
            try:
                services.create_booking(conn, uid, sid, [seat])
                with lock:
                    sniped.append(seat)
            except services.BookingError:
                pass
            time.sleep(0.002)
        conn.close()

    def worker():
        """비교 방식의 대기열 처리 작업자 (poll_ms 마다 실행)"""
        conn = database.connect()
        while not stop.is_set():
            time.sleep(poll_ms / 1000)
            conn.execute("BEGIN IMMEDIATE")
            services._process_waitlist(conn, sid)
            conn.execute("COMMIT")
        conn.close()

    threads = [threading.Thread(target=canceller, args=(u, b)) for u, b in zip(holders[:CANCELS], bookings[:CANCELS])]
    threads += [threading.Thread(target=sniper, args=(u,)) for u in snipers]
    w = threading.Thread(target=worker) if method == "polling" else None
    if w:
        w.start()
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    if w:
        w.join()

    with database.get_conn() as conn:
        status = [conn.execute("SELECT status FROM waitlist WHERE user_id=? AND screening_id=?", (u, sid)).fetchone()[0]
                  for u in waiters]
        rows = conn.execute("SELECT seat_code FROM booking_seats WHERE screening_id=?", (sid,)).fetchall()
    fulfilled = [i for i, s in enumerate(status) if s == "FULFILLED"]
    waiting = [i for i, s in enumerate(status) if s == "WAITING"]
    inversions = sum(1 for f in fulfilled for wtg in waiting if wtg < f)   # 먼저 온 사람이 아직 대기 중인데 뒷사람이 배정된 쌍
    return {
        "method": method, "waiters": n_waiters,
        "sniped": len(sniped),
        "fifo_violations": inversions,
        "duplicates": len(rows) - len({r[0] for r in rows}),
        "to_waitlist": len(fulfilled),
        "cancel_ms": statistics.mean(latencies),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--poll-ms", type=int, default=100)
    args = ap.parse_args()
    sizes = [10, 50] if args.quick else [10, 50, 100, 200]
    repeat = 2 if args.quick else args.repeat

    raw = []
    for n in sizes:
        for method in ("atomic", "polling"):
            for r in range(repeat):
                res = run_trial(method, n, args.poll_ms)
                raw.append(res)
                print(f"  {method:8s} N={n:<4d} #{r + 1}: 끼어들기 {res['sniped']}, 순서위반 {res['fifo_violations']}, "
                      f"중복 {res['duplicates']}, 대기자배정 {res['to_waitlist']}/5, 취소 {res['cancel_ms']:.1f}ms")

    out_dir = os.path.join(ROOT, "experiments", "results")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "waitlist_results.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(raw[0]))
        wr.writeheader()
        wr.writerows(raw)

    name = {"atomic": "제안(원자적 재배정)", "polling": f"비교(폴링 {args.poll_ms}ms)"}
    print(f"\n### 취소표 자동 예매 실험 결과 (취소 {CANCELS}건 · 끼어들기 시도자 {SNIPERS}명 · 각 {repeat}회 평균)\n")
    print("| 방식 | 대기자 수 | 끼어들기 성공(석) | 순서 위반(쌍) | 중복 배정 | 대기자 배정(석/5) | 취소 응답(ms) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for n in sizes:
        for method in ("atomic", "polling"):
            rs = [r for r in raw if r["method"] == method and r["waiters"] == n]
            avg = lambda k: statistics.mean(r[k] for r in rs)  # noqa: E731
            print(f"| {name[method]} | {n} | {avg('sniped'):.1f} | {avg('fifo_violations'):.1f} | "
                  f"{avg('duplicates'):.0f} | {avg('to_waitlist'):.1f} | {avg('cancel_ms'):.1f} |")
    print(f"\nCSV 저장: {os.path.join(out_dir, 'waitlist_results.csv')}")


if __name__ == "__main__":
    main()
