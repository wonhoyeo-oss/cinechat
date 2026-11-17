"""LLM 기반 대화형 챗봇 (Google Gemini Function Calling).

동작 흐름
  사용자 자연어 → LLM 이 의도 파악 → 필요한 '도구(tool)' 호출 결정 → 백엔드가 DB 조회/예매 실행
  → 결과(JSON)를 다시 LLM 에 전달 → LLM 이 자연스러운 한국어 답변 생성
LLM 은 DB 에 직접 접근하지 않고 아래 TOOLS 로 정의된 함수만 호출할 수 있으므로
없는 영화·회차를 지어내는 환각(hallucination)을 줄이고, 예매 같은 쓰기 작업도 검증된 로직을 거친다.

GEMINI_API_KEY 가 없으면 규칙 기반(키워드) 엔진으로 자동 전환되어 데모가 가능하다.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

from . import services

log = logging.getLogger("cinechat.chatbot")

SYSTEM_PROMPT = """너는 영화관 'CineChat'의 친절한 예매 도우미야. 오늘 날짜는 {today}({weekday})이고 현재 시각은 {now}야.
규칙:
- 영화·상영시간·좌석 정보는 반드시 도구(tool)를 호출해서 얻은 데이터만 사용해. 목록에 없는 영화나 회차를 지어내지 마.
- 사용자가 기분, 상황, 취향을 모호하게 말하면(예: "비 오는 날 볼 만한 거", "울고 싶어") 어울리는 장르를 추론해 search_movies 로 찾아 2~4편을 이유와 함께 추천해.
- 예매 요청이 오면 get_showtimes 로 회차를 찾고, 날짜/시간/인원이 불분명하면 짧게 되물어.
- 실제 결제(book_seats)는 사용자가 영화·회차·좌석·금액을 확인하고 "예매해줘/결제해줘"처럼 명확히 동의했을 때만 호출해. 좌석을 지정하지 않았으면 get_seat_map 으로 빈 좌석 중 가운데 쪽 연석을 골라 제안해.
- 사용자가 화면에서 직접 좌석을 고르고 싶어 하면 open_booking_screen 을 호출해.
- 로그인하지 않은 상태({login})라면 결제 전에 로그인이 필요하다고 안내해.
- 답변은 한국어로, 3~6문장 정도로 간결하게. 금액은 '12,000원'처럼 표기해."""

TOOLS = [
    {"type": "function", "function": {
        "name": "search_movies",
        "description": "제목/키워드 또는 장르로 상영 중인 영화를 검색한다. 추천할 때도 사용.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "제목 또는 줄거리 키워드"},
            "genre": {"type": "string", "description": "장르명 (예: 액션, 로맨스, 코미디, 공포, SF, 애니메이션, 드라마, 스릴러, 판타지, 범죄, 가족, 음악)"},
            "limit": {"type": "integer", "default": 5},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_box_office",
        "description": "현재 박스오피스 순위(인기 영화)를 가져온다.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 5}}},
    }},
    {"type": "function", "function": {
        "name": "get_showtimes",
        "description": "특정 영화의 상영 회차(시간, 상영관, 가격, 잔여석)를 조회한다.",
        "parameters": {"type": "object", "required": ["movie_id"], "properties": {
            "movie_id": {"type": "integer"},
            "date": {"type": "string", "description": "YYYY-MM-DD. 생략 시 전체 일정"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_seat_map",
        "description": "상영 회차의 좌석 배치와 이미 예매된 좌석을 조회한다.",
        "parameters": {"type": "object", "required": ["screening_id"], "properties": {
            "screening_id": {"type": "integer"}}},
    }},
    {"type": "function", "function": {
        "name": "open_booking_screen",
        "description": "사용자 화면에 해당 회차의 좌석 선택/결제 창을 연다. 좌석을 미리 선택해 둘 수 있다.",
        "parameters": {"type": "object", "required": ["screening_id"], "properties": {
            "screening_id": {"type": "integer"},
            "seats": {"type": "array", "items": {"type": "string"}, "description": "미리 선택할 좌석 예: ['E5','E6']"},
        }},
    }},
    {"type": "function", "function": {
        "name": "book_seats",
        "description": "포인트로 실제 모의 결제·예매를 진행한다. 사용자가 명확히 동의한 뒤에만 호출.",
        "parameters": {"type": "object", "required": ["screening_id", "seats"], "properties": {
            "screening_id": {"type": "integer"},
            "seats": {"type": "array", "items": {"type": "string"}},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_my_bookings",
        "description": "로그인한 사용자의 예매 내역과 포인트 잔액을 조회한다.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

WEEKDAYS = "월화수목금토일"


def _slim_movie(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "title", "genres", "overview", "release_year", "runtime", "rating", "box_rank")}


def suggest_seats(seatmap: dict, count: int) -> list[str]:
    """빈 좌석 중 스크린 중앙·약간 뒤쪽의 연속 좌석을 고른다."""
    rows, cols, taken = seatmap["rows"], seatmap["cols"], set(seatmap["taken"])
    best_rows = sorted(rows, key=lambda r: abs(rows.index(r) - (len(rows) * 0.6)))
    center = (cols + 1) / 2
    for r in best_rows:
        starts = sorted(range(1, cols - count + 2), key=lambda c: abs(c + (count - 1) / 2 - center))
        for c in starts:
            block = [f"{r}{c + i}" for i in range(count)]
            if not taken.intersection(block):
                return block
    free = [f"{r}{c}" for r in rows for c in range(1, cols + 1) if f"{r}{c}" not in taken]
    return free[:count]


class ToolRunner:
    """LLM/규칙 엔진이 호출하는 도구 실행기. UI 에 보여줄 카드/액션을 함께 수집한다."""

    def __init__(self, conn, user: dict | None):
        self.conn = conn
        self.user = user
        self.movies: dict[int, dict] = {}
        self.screenings: list[dict] = []
        self.action: dict | None = None
        self.booking: dict | None = None

    def run(self, name: str, args: dict) -> dict:
        try:
            fn = getattr(self, f"t_{name}")
        except AttributeError:
            return {"error": f"알 수 없는 도구: {name}"}
        try:
            return fn(**args)
        except services.BookingError as e:
            return {"error": e.message}
        except TypeError as e:
            return {"error": f"잘못된 인자: {e}"}

    def _remember(self, movies):
        for m in movies:
            self.movies[m["id"]] = m

    def t_search_movies(self, query=None, genre=None, limit=5):
        res = services.list_movies(self.conn, query=query, genre=genre, limit=limit or 5)
        if not res and query:  # 띄어쓰기 차이 등으로 못 찾으면 공백 제거 후 재검색
            res = [m for m in services.list_movies(self.conn, limit=200)
                   if query.replace(" ", "") in m["title"].replace(" ", "")][:limit]
        self._remember(res)
        return {"movies": [_slim_movie(m) for m in res]}

    def t_get_box_office(self, limit=5):
        res = services.box_office(self.conn, limit=limit or 5)
        self._remember(res)
        return {"box_office": [_slim_movie(m) for m in res]}

    def t_get_showtimes(self, movie_id, date=None):
        movie = services.get_movie(self.conn, int(movie_id))
        if not movie:
            return {"error": "해당 영화를 찾을 수 없습니다."}
        self._remember([movie])
        shows = services.list_screenings(self.conn, movie_id=int(movie_id), date=date)
        self.screenings = shows
        return {"movie": movie["title"], "showtimes": [
            {k: s[k] for k in ("id", "starts_at", "theater", "price", "remaining_seats")} for s in shows[:20]
        ]}

    def t_get_seat_map(self, screening_id):
        sm = services.seat_map(self.conn, int(screening_id))
        return {"rows": sm["rows"], "cols": sm["cols"], "taken": sm["taken"], "remaining": sm["remaining"],
                "recommended_pair": suggest_seats(sm, 2), "price": sm["screening"]["price"],
                "note": "A열이 스크린에 가장 가깝다."}

    def t_open_booking_screen(self, screening_id, seats=None):
        scr = services.get_screening(self.conn, int(screening_id))
        if not scr:
            return {"error": "존재하지 않는 회차입니다."}
        self.action = {"type": "open_booking", "screening_id": int(screening_id), "seats": seats or []}
        return {"ok": True, "message": "좌석 선택 화면을 열었습니다."}

    def t_book_seats(self, screening_id, seats):
        if not self.user:
            return {"error": "로그인이 필요합니다. 왼쪽 사이드바에서 로그인해 주세요."}
        b = services.create_booking(self.conn, self.user["id"], int(screening_id), seats)
        self.booking = b
        self.action = {"type": "booked", "booking_id": b["id"]}
        left = self.conn.execute("SELECT points FROM users WHERE id=?", (self.user["id"],)).fetchone()[0]
        return {"ok": True, "booking": b, "points_left": left}

    def t_get_my_bookings(self):
        if not self.user:
            return {"error": "로그인이 필요합니다."}
        bs = services.my_bookings(self.conn, self.user["id"])
        left = self.conn.execute("SELECT points FROM users WHERE id=?", (self.user["id"],)).fetchone()[0]
        return {"points": left, "bookings": bs[:10]}

    def result(self, reply: str, engine: str) -> dict:
        return {
            "reply": reply,
            "engine": engine,
            "movies": list(self.movies.values())[:6],
            "screenings": self.screenings[:12],
            "action": self.action,
            "booking": self.booking,
        }


# ───────────────────────── LLM 엔진 (기본: Google Gemini) ─────────────────────────
def llm_config() -> dict | None:
    """사용할 LLM 결정. 기본은 Google Gemini(무료 티어). GEMINI_API_KEY 가 없고 OPENAI_API_KEY 만 있으면 OpenAI."""
    if os.getenv("GEMINI_API_KEY", "").strip():
        return {"provider": "gemini", "api_key": os.getenv("GEMINI_API_KEY").strip(),
                "model": os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")}
    if os.getenv("OPENAI_API_KEY", "").strip():
        return {"provider": "openai", "api_key": os.getenv("OPENAI_API_KEY").strip(),
                "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini")}
    return None


def _system_prompt(user: dict | None) -> str:
    now = datetime.now()
    return SYSTEM_PROMPT.format(
        today=now.strftime("%Y-%m-%d"), weekday=WEEKDAYS[now.weekday()] + "요일", now=now.strftime("%H:%M"),
        login=f"로그인: {user['nickname']}님, 포인트 {user['points']:,}P" if user else "로그인 안 함",
    )


class RateLimitError(Exception):
    """무료 티어 호출 한도 초과"""


def _gemini_chat(conn, messages: list[dict], user: dict | None, cfg: dict) -> dict:
    """Google 공식 google-genai SDK 로 Function Calling.
    모델 응답(Content)을 그대로 대화 기록에 다시 넣기 때문에, Gemini 3 의 'thought signature'
    (함수 호출 사이의 추론 맥락)가 보존되어 여러 번 이어지는 도구 호출도 안정적으로 동작한다."""
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=cfg["api_key"], http_options=types.HttpOptions(
        timeout=40_000,
        retry_options=types.HttpRetryOptions(attempts=3, initial_delay=1.5, http_status_codes=[500, 502, 503, 504]),
    ))
    model = cfg["model"]
    engine = f"gemini:{model}"

    declarations = [types.FunctionDeclaration(
        name=t["function"]["name"],
        description=t["function"]["description"],
        parameters_json_schema=t["function"]["parameters"],
    ) for t in TOOLS]
    config = types.GenerateContentConfig(
        system_instruction=_system_prompt(user),
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # 도구 실행은 직접 제어
        temperature=0.6,
    )
    # Gemini 는 assistant 대신 'model' 역할을 쓰고, 대화는 user 로 시작해야 한다
    contents = []
    for m in messages[-12:]:
        role = "model" if m["role"] == "assistant" else "user"
        if not contents and role == "model":
            continue
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

    runner = ToolRunner(conn, user)
    for _ in range(6):  # 도구 호출 루프 (무한 루프 방지)
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
        except errors.ClientError as e:
            if e.code == 429:
                raise RateLimitError() from e
            raise
        calls = resp.function_calls or []
        if not calls:
            return runner.result((resp.text or "").strip() or "죄송해요, 다시 한 번 말씀해 주시겠어요?", engine)
        contents.append(resp.candidates[0].content)          # 모델의 함수 호출(서명 포함) 그대로 보존
        parts = []
        for fc in calls:
            args = dict(fc.args or {})
            out = runner.run(fc.name, args)
            log.info("tool %s(%s) -> %s", fc.name, args, str(out)[:200])
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=fc.id, name=fc.name, response=json.loads(json.dumps(out, ensure_ascii=False, default=str)))))
        contents.append(types.Content(role="user", parts=parts))
    return runner.result("요청을 처리하는 데 시간이 너무 오래 걸렸어요. 조금 더 구체적으로 말씀해 주시겠어요?", engine)


def _openai_chat(conn, messages: list[dict], user: dict | None, cfg: dict) -> dict:
    """(선택) OpenAI 로 실행할 때 사용하는 엔진"""
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"], timeout=40)
    model = cfg["model"]
    engine = f"openai:{model}"
    system = _system_prompt(user)
    convo = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in messages[-12:] if m["role"] in ("user", "assistant")
    ]
    runner = ToolRunner(conn, user)
    for _ in range(6):  # 도구 호출 루프 (무한 루프 방지)
        resp = client.chat.completions.create(model=model, messages=convo, tools=TOOLS, temperature=0.6)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return runner.result(msg.content or "", engine)
        convo.append({"role": "assistant", "content": msg.content or "",
                      "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            out = runner.run(tc.function.name, args)
            log.info("tool %s(%s) -> %s", tc.function.name, args, str(out)[:200])
            convo.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(out, ensure_ascii=False)})
    return runner.result("요청을 처리하는 데 시간이 너무 오래 걸렸어요. 조금 더 구체적으로 말씀해 주시겠어요?", engine)


# ───────────────────────── 규칙 기반 폴백 엔진 ─────────────────────────
GENRE_WORDS = {
    "액션": ["액션", "때려", "시원한", "화끈", "스트레스"],
    "로맨스": ["로맨스", "멜로", "사랑", "연애", "설레", "데이트", "커플"],
    "코미디": ["코미디", "웃긴", "웃고", "웃음", "유쾌", "가볍게", "우울", "기분 전환"],
    "공포": ["공포", "호러", "무서운", "무섭", "오싹"],
    "SF": ["sf", "에스에프", "우주", "공상과학", "미래"],
    "애니메이션": ["애니", "만화", "아이랑", "아이와", "애들", "가족이랑"],
    "드라마": ["드라마", "감동", "울고", "눈물", "여운"],
    "스릴러": ["스릴러", "긴장", "반전", "추리"],
    "범죄": ["범죄", "형사", "느와르"],
    "판타지": ["판타지", "마법"],
    "음악": ["뮤지컬", "음악"],
}


def _parse_date(text: str) -> str | None:
    today = datetime.now().date()
    if "모레" in text:
        return (today + timedelta(days=2)).isoformat()
    if "내일" in text:
        return (today + timedelta(days=1)).isoformat()
    if "오늘" in text:
        return today.isoformat()
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        return f"{today.year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _parse_hour(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*시", text)
    if m:
        h = int(m.group(1))
        if ("저녁" in text or "오후" in text or "밤" in text) and h < 12:
            h += 12
        elif h < 9:  # '7시' 처럼 말하면 보통 저녁
            h += 12
        return h
    for word, h in (("아침", 10), ("오전", 10), ("점심", 13), ("오후", 16), ("저녁", 19), ("밤", 22), ("심야", 22)):
        if word in text:
            return h
    return None


def _parse_count(text: str) -> int | None:
    m = re.search(r"(\d)\s*(명|자리|석|장)", text)
    if m:
        return int(m.group(1))
    for word, n in (("혼자", 1), ("한 명", 1), ("둘이", 2), ("두 명", 2), ("두 자리", 2), ("2인", 2),
                    ("세 명", 3), ("셋이", 3), ("네 명", 4), ("넷이", 4)):
        if word in text:
            return n
    return None


def _rule_chat(conn, messages: list[dict], user: dict | None) -> dict:
    runner = ToolRunner(conn, user)
    text = messages[-1]["content"].strip() if messages else ""
    low = text.lower()

    # 직전 대화에서 언급된 영화를 이어서 처리 (예: "그거 내일 7시로 예매해줘")
    all_movies = services.list_movies(conn, limit=500)
    def find_title(s):
        squeezed = s.replace(" ", "")
        return next((m for m in sorted(all_movies, key=lambda x: -len(x["title"]))
                     if m["title"].replace(" ", "") in squeezed), None)
    movie = find_title(text)
    if not movie and re.search(r"그거|그 영화|이거|첫 ?번째|1번", text):
        for prev in reversed(messages[:-1]):
            movie = find_title(prev["content"])
            if movie:
                break

    if re.search(r"내 ?예매|예매 ?내역|포인트 ?잔액|내 ?포인트", text):
        out = runner.t_get_my_bookings()
        if "error" in out:
            return runner.result(out["error"], "rule")
        lines = [f"- {b['title']} · {b['starts_at']} · {', '.join(b['seats']) or '취소됨'} ({b['status']})"
                 for b in out["bookings"][:5]]
        return runner.result(f"현재 포인트는 {out['points']:,}P 입니다.\n" +
                             ("\n".join(lines) if lines else "아직 예매 내역이 없어요."), "rule")

    if movie and re.search(r"예매|예약|보러|볼래|잡아|표", text):
        date = _parse_date(text)
        hour = _parse_hour(text)
        count = _parse_count(text) or 2
        runner.t_get_showtimes(movie["id"], date)
        shows = runner.screenings
        if not shows:
            return runner.result(f"'{movie['title']}'의 {'해당 날짜 ' if date else ''}남은 상영 회차가 없어요. 다른 날짜를 말씀해 주세요.", "rule")
        pick = min(shows, key=lambda s: abs(int(s["starts_at"][11:13]) - hour)) if hour is not None else shows[0]
        sm = services.seat_map(conn, pick["id"])
        seats = suggest_seats(sm, count)
        runner.t_open_booking_screen(pick["id"], seats)
        total = pick["price"] * len(seats)
        return runner.result(
            f"'{movie['title']}' {pick['starts_at']} {pick['theater']} 회차를 찾았어요. "
            f"추천 좌석은 {', '.join(seats)} ({len(seats)}석, 총 {total:,}원)입니다. "
            "예매 창에서 좌석을 확인하고 결제 버튼을 눌러 주세요!", "rule")

    if movie:
        runner.t_get_showtimes(movie["id"], _parse_date(text))
        g = ", ".join(movie["genres"])
        n = len(runner.screenings)
        return runner.result(
            f"'{movie['title']}' ({movie.get('release_year') or ''}, {g})\n{movie['overview']}\n\n"
            f"예매 가능한 회차가 {n}개 있어요. \"내일 저녁 7시 두 명 예매해줘\"처럼 말씀해 주세요.", "rule")

    if re.search(r"박스오피스|인기|순위|요즘|흥행|뭐 ?하", text):
        out = runner.t_get_box_office(5)
        lines = [f"{m['box_rank']}위 {m['title']} ({', '.join(m['genres'][:2])})" for m in out["box_office"]]
        return runner.result("지금 가장 인기 있는 영화예요!\n" + "\n".join(lines), "rule")

    genre = next((g for g, words in GENRE_WORDS.items() if any(w in low for w in words)), None)
    if genre or re.search(r"추천|볼 ?만한|뭐 ?볼", text):
        out = runner.t_search_movies(genre=genre, limit=4) if genre else runner.t_get_box_office(4)
        items = out.get("movies") or out.get("box_office") or []
        if not items:
            return runner.result(f"'{genre}' 장르 상영작이 지금은 없어요. 다른 장르는 어떠세요?", "rule")
        lines = [f"• {m['title']} — {m['overview'][:45]}…" for m in items]
        head = f"{genre} 장르로 골라봤어요." if genre else "고민되신다면 요즘 인기작부터 추천드려요."
        return runner.result(head + "\n" + "\n".join(lines) + "\n\n마음에 드는 영화 제목을 말씀해 주시면 상영 시간을 알려드릴게요.", "rule")

    return runner.result(
        "안녕하세요! CineChat 예매 도우미예요 🎬\n"
        "• \"요즘 인기 영화 뭐야?\"\n• \"우울한데 웃긴 영화 추천해줘\"\n• \"인셉션 내일 저녁 7시 2명 예매해줘\"\n"
        "• \"내 예매 내역 보여줘\" 처럼 편하게 말씀해 주세요.\n"
        "(현재 GEMINI_API_KEY 가 없어 키워드 기반 모드로 동작 중입니다)", "rule")


def chat(conn, messages: list[dict], user: dict | None) -> dict:
    cfg = llm_config()
    if cfg:
        engine = _gemini_chat if cfg["provider"] == "gemini" else _openai_chat
        try:
            return engine(conn, messages, user, cfg)
        except RateLimitError:
            out = _rule_chat(conn, messages, user)
            out["reply"] += "\n\n(⏳ 무료 AI 호출 한도에 잠시 걸려서 키워드 모드로 답했어요. 1분쯤 뒤에 다시 시도해 주세요.)"
            return out
        except Exception as e:
            log.exception("LLM 호출 실패 → 규칙 기반 엔진으로 대체")
            out = _rule_chat(conn, messages, user)
            if getattr(e, "code", None) in (400, 401, 403):
                out["reply"] += "\n\n(⚠️ AI API 키가 올바르지 않거나 권한이 없어 키워드 모드로 답했어요. .env 의 키를 확인해 주세요.)"
            elif getattr(e, "code", None) == 404:
                out["reply"] += "\n\n(⚠️ 모델 이름을 찾을 수 없어요. .env 의 GEMINI_MODEL 을 AI Studio 에 표시된 모델명으로 바꿔 주세요.)"
            else:
                out["reply"] += f"\n\n(⚠️ AI 서버 연결 실패로 키워드 모드로 응답했어요: {type(e).__name__})"
            return out
    return _rule_chat(conn, messages, user)
