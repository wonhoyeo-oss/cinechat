"""초기 데이터: 상영관, 샘플 영화, 상영 회차 생성 + 외부 API 동기화."""
import logging
import sqlite3
from datetime import datetime, timedelta

from . import external_api

log = logging.getLogger("cinechat.seed")

THEATERS = [("1관 (일반)", 8, 12), ("2관 (일반)", 8, 12), ("3관 (프리미엄)", 6, 10)]
SHOW_TIMES = ["10:00", "13:30", "16:40", "19:50", "22:30"]
BASE_PRICE = 12_000
PREMIUM_PRICE = 16_000
SCHEDULE_DAYS = 3

# API 키가 없을 때 사용하는 샘플 영화 (줄거리는 직접 작성한 짧은 소개)
SAMPLE_MOVIES = [
    ("인셉션", "SF,액션,스릴러", 2010, 148, "타인의 꿈속에 들어가 생각을 훔치는 전문가가 '생각을 심는' 마지막 임무에 도전한다."),
    ("인터스텔라", "SF,드라마,모험", 2014, 169, "황폐해진 지구를 떠나 인류가 살 수 있는 새 행성을 찾아 우주로 향하는 탐사대의 이야기."),
    ("기생충", "드라마,스릴러", 2019, 132, "반지하에 사는 가족이 부유한 가족의 집에 하나둘 스며들며 벌어지는 예측 불가의 사건."),
    ("어바웃 타임", "로맨스,판타지,드라마", 2013, 123, "시간을 되돌릴 수 있는 능력을 알게 된 청년이 사랑과 가족의 의미를 깨달아 간다."),
    ("라라랜드", "로맨스,음악,드라마", 2016, 128, "꿈을 좇는 재즈 피아니스트와 배우 지망생이 LA에서 만나 사랑하고 성장하는 뮤지컬."),
    ("극한직업", "코미디,액션,범죄", 2019, 111, "잠복 수사를 위해 치킨집을 차린 마약반 형사들. 그런데 가게가 대박이 나 버렸다."),
    ("부산행", "액션,공포,스릴러", 2016, 118, "정체불명의 바이러스가 퍼진 가운데 부산행 KTX에 올라탄 사람들의 생존기."),
    ("겨울왕국", "애니메이션,가족,판타지,음악", 2013, 102, "얼음 마법을 가진 언니 엘사와 그를 찾아 나선 동생 안나의 모험."),
    ("센과 치히로의 행방불명", "애니메이션,판타지,가족", 2001, 125, "신들의 세계에 들어선 소녀 치히로가 부모를 구하기 위해 온천장에서 일하며 성장한다."),
    ("다크 나이트", "액션,범죄,드라마", 2008, 152, "고담시를 혼돈에 빠뜨리는 조커에 맞서 배트맨이 한계를 시험받는다."),
    ("컨저링", "공포,미스터리,스릴러", 2013, 112, "외딴 농가로 이사 온 가족에게 벌어지는 기이한 현상을 초자연 현상 조사 부부가 파헤친다."),
    ("탑건: 매버릭", "액션,드라마", 2022, 131, "전설의 파일럿 매버릭이 최정예 후배들을 이끌고 불가능에 가까운 임무에 나선다."),
    ("인사이드 아웃", "애니메이션,코미디,가족", 2015, 95, "소녀 라일리의 머릿속 감정들인 기쁨, 슬픔, 버럭, 까칠, 소심의 좌충우돌 대모험."),
    ("엑시트", "코미디,액션", 2019, 103, "도심에 유독가스가 퍼지자 산악부 출신 청년이 클라이밍 실력으로 탈출에 나선다."),
    ("범죄도시", "액션,범죄", 2017, 121, "신흥 범죄조직이 세력을 넓히자 괴물 형사 마석도가 소탕 작전에 뛰어든다."),
]


def seed_theaters(conn: sqlite3.Connection):
    for name, r, c in THEATERS:
        conn.execute("INSERT OR IGNORE INTO theaters(name, seat_rows, seat_cols) VALUES (?,?,?)", (name, r, c))


def seed_sample_movies(conn: sqlite3.Connection):
    if conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0] > 0:
        return
    for i, (title, genres, year, runtime, overview) in enumerate(SAMPLE_MOVIES, start=1):
        conn.execute(
            """INSERT INTO movies(source, external_id, title, genres, overview, release_year, runtime, box_rank)
               VALUES ('sample', ?, ?, ?, ?, ?, ?, ?)""",
            (f"S{i}", title, genres, overview, year, runtime, i if i <= 10 else None),
        )


def upsert_movie(conn: sqlite3.Connection, m: dict):
    cols = ["source", "external_id", "title", "genres", "overview", "release_year",
            "runtime", "rating", "poster_url", "box_rank", "audience_acc"]
    vals = [m.get(c) for c in cols]
    vals[3] = vals[3] or ""
    vals[4] = vals[4] or ""
    updates = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols[2:])
    conn.execute(
        f"INSERT INTO movies({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(source, external_id) DO UPDATE SET {updates}",
        vals,
    )


def sync_external(conn: sqlite3.Connection) -> dict:
    """외부 API 에서 영화 데이터를 받아와 DB 에 반영. 받아온 게 있으면 샘플 영화의 박스오피스 순위는 해제."""
    tmdb = external_api.fetch_tmdb_now_playing()
    kobis = external_api.fetch_kobis_daily_boxoffice()
    if not tmdb and not kobis:
        return {"tmdb": 0, "kobis": 0, "message": "API 키가 없거나 연결 실패 → 샘플 데이터 유지"}

    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE movies SET box_rank = NULL")
        for m in tmdb:
            upsert_movie(conn, m)
        for m in kobis:
            extra = external_api.search_tmdb(m["title"]) or {}
            upsert_movie(conn, {**extra, **m})
        # 실제 데이터가 들어왔으므로 샘플 영화 정리 (이미 예매가 걸린 회차/영화는 보존)
        conn.execute("""DELETE FROM screenings
                         WHERE movie_id IN (SELECT id FROM movies WHERE source = 'sample')
                           AND id NOT IN (SELECT screening_id FROM bookings)""")
        conn.execute("""DELETE FROM movies WHERE source = 'sample'
                           AND id NOT IN (SELECT movie_id FROM screenings)""")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"tmdb": len(tmdb), "kobis": len(kobis), "message": "동기화 완료"}


def ensure_schedule(conn: sqlite3.Connection, days: int = SCHEDULE_DAYS):
    """오늘부터 N일간 상영 회차가 없으면 생성. 박스오피스 상위작에 회차를 더 많이 배정한다."""
    theaters = conn.execute("SELECT id, name FROM theaters ORDER BY id").fetchall()
    movies = conn.execute(
        "SELECT id FROM movies ORDER BY box_rank IS NULL, box_rank, rating DESC, id LIMIT 12"
    ).fetchall()
    if not movies or not theaters:
        return
    movie_ids = [m["id"] for m in movies]
    today = datetime.now().date()
    slot = 0
    for d in range(days):
        day = (today + timedelta(days=d)).isoformat()
        for t in theaters:
            for hm in SHOW_TIMES:
                starts = f"{day} {hm}"
                exists = conn.execute(
                    "SELECT 1 FROM screenings WHERE theater_id=? AND starts_at=?", (t["id"], starts)
                ).fetchone()
                if exists:
                    slot += 1
                    continue
                # 앞 순위 영화일수록 자주 등장하도록 가중 순환
                weighted = movie_ids[:4] * 2 + movie_ids
                mid = weighted[slot % len(weighted)]
                price = PREMIUM_PRICE if "프리미엄" in t["name"] else BASE_PRICE
                conn.execute(
                    "INSERT INTO screenings(movie_id, theater_id, starts_at, price) VALUES (?,?,?,?)",
                    (mid, t["id"], starts, price),
                )
                slot += 1


def bootstrap(conn: sqlite3.Connection, use_external: bool = True):
    seed_theaters(conn)
    seed_sample_movies(conn)
    if use_external:
        try:
            log.info("외부 API 동기화: %s", sync_external(conn))
        except Exception as e:
            log.warning("외부 API 동기화 실패: %s", e)
    ensure_schedule(conn)
