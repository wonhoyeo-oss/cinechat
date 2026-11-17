"""외부 영화 데이터 연동 (TMDB + 영화진흥위원회 KOBIS Open API).

- TMDB  : 현재 상영작 목록, 줄거리, 장르, 포스터, 평점 (한국어 메타데이터)
- KOBIS : 일별 박스오피스 순위, 누적 관객수
API 키가 없거나 네트워크 오류가 나면 예외 대신 빈 결과를 돌려주고, 서비스는 샘플 데이터로 계속 동작한다.
"""
import logging
import os
from datetime import datetime, timedelta

import requests

log = logging.getLogger("cinechat.external")

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
KOBIS_BASE = "https://www.kobis.or.kr/kobisopenapi/webservice/rest"
TIMEOUT = 8

# TMDB 장르 ID → 한국어 장르명 (TMDB /genre/movie/list 기준)
TMDB_GENRES = {
    28: "액션", 12: "모험", 16: "애니메이션", 35: "코미디", 80: "범죄", 99: "다큐멘터리",
    18: "드라마", 10751: "가족", 14: "판타지", 36: "역사", 27: "공포", 10402: "음악",
    9648: "미스터리", 10749: "로맨스", 878: "SF", 10770: "TV 영화", 53: "스릴러",
    10752: "전쟁", 37: "서부",
}


def _tmdb_request(path: str, **params):
    key = os.getenv("TMDB_API_KEY", "").strip()
    if not key:
        return None
    headers = {"accept": "application/json"}
    # v4 읽기 토큰(긴 JWT)과 v3 API 키 모두 지원
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    params.setdefault("language", "ko-KR")
    r = requests.get(f"{TMDB_BASE}{path}", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_tmdb_now_playing(pages: int = 2) -> list[dict]:
    movies = []
    try:
        for page in range(1, pages + 1):
            data = _tmdb_request("/movie/now_playing", region="KR", page=page)
            if not data:
                return []
            for m in data.get("results", []):
                movies.append({
                    "source": "tmdb",
                    "external_id": str(m["id"]),
                    "title": m.get("title") or m.get("original_title"),
                    "genres": ",".join(TMDB_GENRES.get(g, "") for g in m.get("genre_ids", []) if g in TMDB_GENRES),
                    "overview": m.get("overview") or "",
                    "release_year": int(m["release_date"][:4]) if m.get("release_date") else None,
                    "rating": m.get("vote_average"),
                    "poster_url": f"{TMDB_IMG}{m['poster_path']}" if m.get("poster_path") else None,
                })
    except Exception as e:  # 네트워크/키 오류 → 샘플 데이터로 대체
        log.warning("TMDB 연동 실패: %s", e)
        return []
    return movies


def search_tmdb(title: str) -> dict | None:
    """KOBIS 박스오피스 영화에 포스터·줄거리를 붙이기 위한 제목 검색"""
    try:
        data = _tmdb_request("/search/movie", query=title, region="KR")
        if data and data.get("results"):
            m = data["results"][0]
            return {
                "overview": m.get("overview") or "",
                "genres": ",".join(TMDB_GENRES.get(g, "") for g in m.get("genre_ids", []) if g in TMDB_GENRES),
                "rating": m.get("vote_average"),
                "poster_url": f"{TMDB_IMG}{m['poster_path']}" if m.get("poster_path") else None,
                "release_year": int(m["release_date"][:4]) if m.get("release_date") else None,
            }
    except Exception as e:
        log.warning("TMDB 검색 실패(%s): %s", title, e)
    return None


def fetch_kobis_daily_boxoffice() -> list[dict]:
    key = os.getenv("KOBIS_API_KEY", "").strip()
    if not key:
        return []
    target = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")  # 전일자까지만 집계됨
    try:
        r = requests.get(
            f"{KOBIS_BASE}/boxoffice/searchDailyBoxOfficeList.json",
            params={"key": key, "targetDt": target},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        items = r.json()["boxOfficeResult"]["dailyBoxOfficeList"]
    except Exception as e:
        log.warning("KOBIS 연동 실패: %s", e)
        return []
    return [{
        "source": "kobis",
        "external_id": it["movieCd"],
        "title": it["movieNm"],
        "box_rank": int(it["rank"]),
        "audience_acc": int(it["audiAcc"]),
        "release_year": int(it["openDt"][:4]) if it.get("openDt") else None,
    } for it in items]
