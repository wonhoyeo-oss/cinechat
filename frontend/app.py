"""CineChat 웹 프론트엔드 (Streamlit).

실행: streamlit run frontend/app.py
- 왼쪽: 대화형 AI 챗봇 창 (추천 영화 카드 → 클릭 시 바로 예매 패널로 연결)
- 오른쪽: 예매 대시보드 (상영 회차 → 좌석 선택 → 포인트 모의 결제)
- 사이드바: 로그인/회원가입, 포인트 충전, 예매 내역
백엔드와는 requests.Session 으로 통신하며, 서버가 내려준 세션 쿠키를 그대로 유지한다.
"""
import os
import sys
import threading
import time

import requests
import streamlit as st
import streamlit.components.v1 as components

API = os.getenv("API_URL", "http://localhost:8000")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

st.set_page_config(page_title="CineChat · AI 영화 예매", page_icon="🎬", layout="wide")


# ───────────────────────── 배포 지원: 비밀키 로드 + 백엔드 내장 실행 ─────────────────────────
def load_secrets_to_env():
    """Streamlit Cloud 의 Secrets(st.secrets)를 환경변수로 옮긴다. 로컬에서는 .env 를 사용하므로 없어도 무방."""
    try:
        for k, v in st.secrets.items():
            if isinstance(v, (str, int, float)) and not os.getenv(k):
                os.environ[k] = str(v)
    except Exception:
        pass


def _backend_alive() -> bool:
    try:
        return requests.get(f"{API}/api/health", timeout=1.5).ok
    except requests.RequestException:
        return False


@st.cache_resource(show_spinner=False)
def ensure_backend() -> str:
    """백엔드(FastAPI)가 따로 켜져 있으면 그대로 쓰고, 없으면 같은 프로세스 안에서 백그라운드로 실행한다.
    → Streamlit Community Cloud 처럼 서버를 하나만 띄울 수 있는 환경에서도 동작."""
    if _backend_alive():
        return "external"
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    import uvicorn
    from backend.main import app as api_app

    server = uvicorn.Server(uvicorn.Config(api_app, host="127.0.0.1", port=8000, log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="cinechat-backend").start()
    for _ in range(120):  # 외부 API 동기화 포함 최대 60초 대기
        if _backend_alive():
            return "embedded"
        time.sleep(0.5)
    return "failed"


load_secrets_to_env()
with st.spinner("서버를 준비하고 있어요… (처음 접속 시 최대 1분)"):
    BACKEND_MODE = ensure_backend()


# ───────────────────────── Streamlit 기본 메뉴·안내 문구 한국어화 ─────────────────────────
KO_SCRIPT = """
<script>
(function () {
  const doc = window.parent.document;
  doc.documentElement.lang = "ko";
  const MAP = {
    "System": "시스템", "Light": "라이트", "Dark": "다크",
    "Rerun": "다시 실행", "Auto rerun": "자동 다시 실행", "Always rerun": "항상 다시 실행",
    "Clear cache": "캐시 비우기", "Print": "인쇄", "Record screen": "화면 녹화",
    "Stop recording": "녹화 중지", "Deploy": "배포", "Settings": "설정", "About": "정보",
    "Developer options": "개발자 옵션", "Report a bug": "버그 신고", "Get help": "도움말",
    "Running...": "실행 중...", "Stop": "중지", "Connecting": "연결 중", "Please wait...": "잠시만 기다려 주세요...",
    "Press Enter to submit form": "Enter 키를 누르면 제출됩니다",
    "Press Enter to apply": "Enter 키를 누르면 적용됩니다",
    "Choose an option": "선택하세요", "No results": "결과 없음",
    "Deploy this app using...": "이 앱을 배포할 방법을 선택하세요",
    "Deploy now": "지금 배포", "Learn more": "자세히 보기", "Start trial": "체험 시작",
    "Other platforms": "다른 플랫폼", "For community, always free": "커뮤니티용, 항상 무료",
    "For enterprise": "기업용", "For custom deployment": "직접 구성하는 배포",
    "Choose app theme, colors and fonts": "앱 테마, 색상, 글꼴 선택",
    "Wide mode": "넓은 화면", "Edit active theme": "현재 테마 편집",
    "Close": "닫기", "Clear caches": "캐시 비우기", "Cancel": "취소",
    "Are you sure you want to clear the app's function caches?": "앱의 캐시를 모두 비울까요?",
    "This will remove all cached entries from functions using @st.cache_data and @st.cache_resource.":
      "@st.cache_data, @st.cache_resource 로 저장된 캐시가 모두 삭제됩니다.",
    "Main menu": "메인 메뉴", "Collapse sidebar": "사이드바 접기", "Expand sidebar": "사이드바 펼치기",
    "Show password text": "비밀번호 보기", "Hide password text": "비밀번호 숨기기",
    "Fullscreen": "전체 화면", "Close fullscreen": "전체 화면 닫기"
  };
  const MADE = /^Made with Streamlit/;
  function translate(root) {
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const t = n.nodeValue.trim();
      if (!t) continue;
      if (MAP[t]) n.nodeValue = n.nodeValue.replace(t, MAP[t]);
      else if (MADE.test(t)) n.nodeValue = n.nodeValue.replace("Made with Streamlit", "Streamlit으로 제작");
    }
    root.querySelectorAll && root.querySelectorAll("[title],[aria-label],[placeholder]").forEach(el => {
      for (const attr of ["title", "aria-label", "placeholder"]) {
        const v = el.getAttribute(attr);
        if (v && MAP[v]) el.setAttribute(attr, MAP[v]);
      }
    });
  }
  if (window.parent.__cinechatKoObserver) window.parent.__cinechatKoObserver.disconnect();
  let pending = false;
  const obs = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; translate(doc.body); });
  });
  obs.observe(doc.body, { childList: true, subtree: true, characterData: true });
  window.parent.__cinechatKoObserver = obs;
  translate(doc.body);
})();
</script>
"""

st.markdown("""
<style>
.block-container {padding-top: 1.6rem;}
.movie-card {border:1px solid rgba(128,128,128,.25); border-radius:12px; padding:10px 12px; margin-bottom:6px;}
.movie-card h4 {margin:0 0 2px 0; font-size:1rem;}
.movie-meta {opacity:.7; font-size:.8rem;}
.poster-ph {height:150px; border-radius:8px; display:flex; align-items:center; justify-content:center;
            font-size:2.4rem; background:linear-gradient(135deg,#3a2f5b,#b4475f);}
.screen {text-align:center; padding:4px; margin:6px 0 12px; border-radius:0 0 40px 40px;
         background:linear-gradient(#ddd,#888); color:#222; font-size:.75rem; letter-spacing:.4em;}
div[data-testid="stHorizontalBlock"] button[kind] {padding:0 !important; min-height:30px;}
.legend span {display:inline-block; margin-right:12px; font-size:.8rem;}
.jump {display:block; text-align:center; padding:8px; border-radius:10px; margin:4px 0;
       background:rgba(180,71,95,.12); color:inherit !important; text-decoration:none !important; font-weight:600;}
.jump.mobile-only, .mobile-only {display:none;}
.st-key-ko-script {display:none !important;}
.st-key-summary [data-testid="stMetricValue"] {font-size: 1.45rem !important; white-space: normal !important;}
.st-key-summary [data-testid="stMetricValue"] > div {white-space: normal !important; overflow: visible !important;}

/* ───── 반응형: 태블릿 (≤1024px) → 챗봇과 예매 패널을 위아래로 배치 ───── */
@media (max-width: 1024px) {
  .st-key-main > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] {flex-wrap: wrap !important;}
  .st-key-main > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
      min-width: 100% !important; flex: 1 1 100% !important;}
  .jump.mobile-only, .mobile-only {display:block;}
}

/* 가로 배치를 유지해야 하는 줄 (좌석 행, 제목줄, 결제 요약) : 모바일에서도 쌓이지 않게 */
.st-key-seatgrid [data-testid="stHorizontalBlock"],
.st-key-headrow [data-testid="stHorizontalBlock"],
.st-key-legendrow [data-testid="stHorizontalBlock"],
.st-key-summary [data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important;}
.st-key-seatgrid [data-testid="stColumn"],
.st-key-summary [data-testid="stColumn"] {min-width: 0 !important; width: auto !important; flex: 1 1 0 !important;}
.st-key-headrow [data-testid="stColumn"]:first-child,
.st-key-legendrow [data-testid="stColumn"]:first-child {min-width:0 !important; flex: 1 1 auto !important;}
.st-key-headrow [data-testid="stColumn"]:last-child,
.st-key-legendrow [data-testid="stColumn"]:last-child {min-width:0 !important; flex: 0 0 auto !important; width:auto !important;}

/* ───── 반응형: 모바일 (≤640px) ───── */
@media (max-width: 640px) {
  .block-container {padding: 2.6rem .7rem 5rem !important;}
  h3 {font-size: 1.15rem !important;}
  h4 {font-size: 1.05rem !important;}
  /* 좌석: 화면 폭에 맞춰 촘촘하게 */
  .st-key-seatgrid [data-testid="stHorizontalBlock"] {gap: 2px !important;}
  .st-key-seatgrid button {min-height: 26px !important; height: 26px; font-size: .62rem !important; border-radius: 5px;}
  .st-key-seatgrid button p {font-size: .62rem !important;}
  .st-key-seatgrid [data-testid="stMarkdownContainer"] p {font-size: .7rem; margin-top: 4px;}
  /* 영화 카드·예시 질문: 2개씩 */
  [class*="st-key-cards-"] [data-testid="stHorizontalBlock"],
  .st-key-examples [data-testid="stHorizontalBlock"] {flex-wrap: wrap !important; gap: .5rem !important;}
  [class*="st-key-cards-"] [data-testid="stColumn"],
  .st-key-examples [data-testid="stColumn"] {min-width: calc(50% - .25rem) !important; flex: 1 1 calc(50% - .25rem) !important;}
  .st-key-examples button p {font-size: .78rem;}
  .poster-ph {height: 84px; font-size: 1.8rem;}
  .movie-card {padding: 6px 8px;}
  .movie-card h4 {font-size: .88rem;}
  /* 결제 요약 숫자 축소 */
  .st-key-summary [data-testid="stMetricValue"] {font-size: 1.05rem !important;}
  .st-key-summary [data-testid="stMetricLabel"] p {font-size: .72rem !important;}
  /* 채팅창 높이를 화면 높이에 맞춤 */
  .st-key-chatbox {height: 58vh !important; max-height: 58vh !important;}
  .legend span {margin-right: 6px; font-size: .72rem;}
  [data-testid="stChatMessage"] {padding: .5rem .25rem;}
}
</style>
""", unsafe_allow_html=True)


# ───────────────────────── 상태 / API 헬퍼 ─────────────────────────
def init_state():
    ss = st.session_state
    ss.setdefault("http", requests.Session())
    ss.setdefault("user", None)
    ss.setdefault("messages", [])          # [{role, content, movies?, screenings?}]
    ss.setdefault("movie_id", None)
    ss.setdefault("screening_id", None)
    ss.setdefault("selected", set())
    ss.setdefault("flash", None)


init_state()
ss = st.session_state

with st.container(key="ko-script"):
    components.html(KO_SCRIPT, height=0)


def api(method: str, path: str, **kw):
    try:
        r = ss.http.request(method, f"{API}{path}", timeout=60, **kw)
    except requests.ConnectionError:
        st.error("서버에 연결할 수 없습니다. 잠시 후 새로고침해 주세요. (계속되면 실행 창의 에러 메시지를 확인하세요)")
        st.stop()
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = r.text
        if isinstance(detail, list):  # pydantic 검증 오류
            detail = " / ".join(d.get("msg", "") for d in detail)
        return None, detail
    return r.json(), None


def refresh_user():
    data, _ = api("GET", "/api/auth/me")
    ss.user = data


def open_booking(movie_id=None, screening_id=None, seats=None):
    if screening_id:
        scr, _ = api("GET", f"/api/screenings/{screening_id}/seats")
        if scr:
            movie_id = scr["screening"]["movie_id"]
    ss.movie_id = movie_id
    ss.screening_id = screening_id
    ss.selected = set(seats or [])


def won(n):
    return f"{n:,}원"


# ───────────────────────── 사이드바: 회원 ─────────────────────────
with st.sidebar:
    st.markdown("## 🎬 CineChat")
    health, _ = api("GET", "/api/health")
    if health:
        llm_name = {"openai": "OpenAI", "gemini": "Gemini (무료)"}.get(health["llm"], "키워드 모드")
        st.caption(f"AI 엔진: **{llm_name}** · "
                   f"TMDB {'✅' if health['tmdb'] else '—'} · KOBIS {'✅' if health['kobis'] else '—'}")
    if ss.user is None:
        refresh_user()

    if ss.user:
        u = ss.user
        st.success(f"**{u['nickname']}**님 환영합니다")
        st.metric("보유 포인트", f"{u['points']:,} P")
        with st.expander("💳 포인트 충전 (가상)"):
            amt = st.select_slider("충전 금액", options=[10_000, 30_000, 50_000, 100_000], value=30_000,
                                   format_func=won)
            if st.button("충전하기", use_container_width=True):
                data, err = api("POST", "/api/points/charge", json={"amount": amt})
                if err:
                    st.error(err)
                else:
                    ss.flash = f"{won(amt)} 충전 완료!"
                    refresh_user()
                    st.rerun()
        with st.expander("🎟️ 내 예매 내역", expanded=False):
            bookings, _ = api("GET", "/api/bookings")
            if not bookings:
                st.caption("예매 내역이 없습니다.")
            for b in bookings or []:
                cancelled = b["status"] == "CANCELLED"
                st.markdown(f"{'~~' if cancelled else ''}**{b['title']}**{'~~' if cancelled else ''}  \n"
                            f"{b['starts_at']} · {b['theater']}  \n"
                            f"{', '.join(b['seats']) if b['seats'] else '취소됨'} · {won(b['total_price'])}")
                if not cancelled and st.button("예매 취소", key=f"cancel{b['id']}"):
                    _, err = api("DELETE", f"/api/bookings/{b['id']}")
                    ss.flash = err or "예매가 취소되고 포인트가 환불되었습니다."
                    refresh_user()
                    st.rerun()
                st.divider()
        if st.button("로그아웃", use_container_width=True):
            api("POST", "/api/auth/logout")
            ss.user = None
            st.rerun()
    else:
        tab_login, tab_signup = st.tabs(["로그인", "회원가입"])
        with tab_login:
            with st.form("login"):
                uid = st.text_input("아이디")
                pw = st.text_input("비밀번호", type="password")
                if st.form_submit_button("로그인", use_container_width=True):
                    data, err = api("POST", "/api/auth/login", json={"username": uid, "password": pw})
                    if err:
                        st.error(err)
                    else:
                        ss.user = data
                        st.rerun()
        with tab_signup:
            with st.form("signup"):
                uid = st.text_input("아이디 (영문/숫자 3~20자)")
                nick = st.text_input("닉네임")
                pw = st.text_input("비밀번호 (4자 이상)", type="password")
                if st.form_submit_button("가입하고 50,000P 받기", use_container_width=True):
                    data, err = api("POST", "/api/auth/signup",
                                    json={"username": uid, "password": pw, "nickname": nick or uid})
                    if err:
                        st.error(err)
                    else:
                        ss.user = data
                        ss.flash = "가입 완료! 축하 포인트 50,000P가 지급되었어요."
                        st.rerun()
        st.info("로그인 없이도 챗봇 추천과 상영 정보 조회는 가능해요. 결제는 로그인 후 이용할 수 있습니다.")

if ss.flash:
    st.toast(ss.flash)
    ss.flash = None


# ───────────────────────── 공통 컴포넌트 ─────────────────────────
def movie_card(m: dict, key: str):
    if m.get("poster_url"):
        st.image(m["poster_url"], use_container_width=True)
    else:
        st.markdown(f"<div class='poster-ph'>🎞️</div>", unsafe_allow_html=True)
    rank = f"🏆 {m['box_rank']}위 · " if m.get("box_rank") else ""
    meta = " · ".join(x for x in [", ".join(m["genres"][:2]), str(m.get("release_year") or ""),
                                  f"{m['runtime']}분" if m.get("runtime") else "",
                                  f"★{m['rating']:.1f}" if m.get("rating") else ""] if x)
    st.markdown(f"<div class='movie-card'><h4>{m['title']}</h4><div class='movie-meta'>{rank}{meta}</div></div>",
                unsafe_allow_html=True)
    if st.button("예매하기", key=key, use_container_width=True):
        open_booking(movie_id=m["id"])
        st.rerun()


def card_grid(movies: list[dict], prefix: str, per_row: int = 4):
    # 모바일에서는 CSS 로 2열 배치 (st-key-cards-* 클래스)
    with st.container(key=f"cards-{prefix}"):
        for i in range(0, len(movies), per_row):
            cols = st.columns(per_row)
            for col, m in zip(cols, movies[i:i + per_row]):
                with col:
                    movie_card(m, f"{prefix}-{m['id']}")


# ───────────────────────── 메인 레이아웃 ─────────────────────────
st.markdown("### 말로 찾고, 말로 예매하는 영화관")
main = st.container(key="main")
chat_col, book_col = main.columns([1.05, 1], gap="large")

# ── 왼쪽: 챗봇 ──
with chat_col:
    st.markdown("#### 💬 AI 예매 도우미")
    box = st.container(height=560, border=True, key="chatbox")
    with box:
        if not ss.messages:
            with st.chat_message("assistant", avatar="🎬"):
                st.markdown("안녕하세요! 오늘 어떤 영화를 보고 싶으세요?  \n"
                            "기분이나 상황만 말씀해 주셔도 골라드리고, 예매까지 대화로 도와드릴게요.")
        for idx, msg in enumerate(ss.messages):
            with st.chat_message(msg["role"], avatar="🎬" if msg["role"] == "assistant" else "🙂"):
                st.markdown(msg["content"])
                if msg.get("meta"):
                    st.caption(msg["meta"])
                if msg.get("movies"):
                    card_grid(msg["movies"][:4], f"chat{idx}", per_row=4)

    _top, _ = api("GET", "/api/movies/boxoffice")
    _title = _top[0]["title"] if _top else "인셉션"   # 실제 상영 1위 영화로 예시 문장 생성
    examples = ["요즘 인기 영화 뭐야?", "비 오는 날 혼자 보기 좋은 영화", f"{_title} 내일 저녁 7시 두 명 예매해줘",
                "내 예매 내역 보여줘"]
    ex_cols = st.container(key="examples").columns(len(examples))
    clicked = None
    for c, ex in zip(ex_cols, examples):
        if c.button(ex, key=f"ex-{ex}", use_container_width=True):
            clicked = ex
    if ss.movie_id is not None:  # 태블릿·모바일에서는 예매 창이 아래에 있으므로 바로가기 제공
        st.markdown("<a class='jump mobile-only' href='#booking-panel'>👇 예매 창으로 이동</a>",
                    unsafe_allow_html=True)
    prompt = st.chat_input("예) 여자친구랑 볼 로맨스 영화 추천해줘") or clicked

    if prompt:
        ss.messages.append({"role": "user", "content": prompt})
        history = [{"role": m["role"], "content": m["content"]} for m in ss.messages]
        with st.spinner("생각 중…"):
            data, err = api("POST", "/api/chat", json={"messages": history[-20:]})
        if err:
            ss.messages.append({"role": "assistant", "content": f"⚠️ {err}"})
        else:
            ss.messages.append({"role": "assistant", "content": data["reply"], "movies": data.get("movies"),
                                "meta": f"⏱ {data.get('elapsed', 0):.1f}초 · {data.get('engine', '')}"})
            action = data.get("action") or {}
            if action.get("type") == "open_booking":
                open_booking(screening_id=action["screening_id"], seats=action.get("seats"))
            elif action.get("type") == "booked":
                ss.flash = "🎉 챗봇을 통해 예매가 완료되었습니다!"
                ss.screening_id = data["booking"]["screening_id"]
                ss.selected = set()
                refresh_user()
            elif data.get("movies") and len(data["movies"]) == 1:
                open_booking(movie_id=data["movies"][0]["id"])
        st.rerun()

# ── 오른쪽: 예매 대시보드 ──
with book_col:
    st.markdown("<div id='booking-panel'></div>", unsafe_allow_html=True)
    st.markdown("#### 🎟️ 예매")
    if ss.movie_id is None:
        st.caption("챗봇에게 물어보거나 아래 박스오피스에서 영화를 골라 주세요.")
        top, _ = api("GET", "/api/movies/boxoffice")
        card_grid((top or [])[:6], "bo", per_row=3)
    else:
        movie, _ = api("GET", f"/api/movies/{ss.movie_id}")
        head_l, head_r = st.container(key="headrow").columns([4, 1])
        head_l.markdown(f"**{movie['title']}** · {', '.join(movie['genres'])}")
        if head_r.button("✕ 닫기"):
            ss.movie_id = ss.screening_id = None
            ss.selected = set()
            st.rerun()
        st.caption(movie["overview"])

        shows, _ = api("GET", f"/api/movies/{ss.movie_id}/screenings")
        if not shows:
            st.warning("예매 가능한 상영 회차가 없습니다.")
        else:
            dates = sorted({s["starts_at"][:10] for s in shows})
            cur_date = next((s["starts_at"][:10] for s in shows if s["id"] == ss.screening_id), dates[0])
            date = st.radio("날짜", dates, index=dates.index(cur_date), horizontal=True,
                            format_func=lambda d: f"{int(d[5:7])}/{int(d[8:10])}")
            day_shows = [s for s in shows if s["starts_at"].startswith(date)]
            labels = {s["id"]: f"{s['starts_at'][11:]} {s['theater']} · 잔여 {s['remaining_seats']}석" for s in day_shows}
            ids = list(labels)
            idx = ids.index(ss.screening_id) if ss.screening_id in ids else 0
            sid = st.selectbox("상영 회차", ids, index=idx, format_func=labels.get)
            if sid != ss.screening_id:
                ss.screening_id = sid
                ss.selected = set()
                st.rerun()

            seatmap, err = api("GET", f"/api/screenings/{sid}/seats")
            scr = seatmap["screening"]
            taken = set(seatmap["taken"])
            ss.selected -= taken   # 그 사이 다른 사람이 예매한 좌석은 선택 해제 (실시간 동기화)

            top_l, top_r = st.container(key="legendrow").columns([3, 1])
            top_l.markdown("<div class='legend'><span>⬜ 선택 가능</span><span>🟥 선택됨</span>"
                           "<span>⬛ 예매 완료</span></div>", unsafe_allow_html=True)
            if top_r.button("🔄 새로고침"):
                st.rerun()
            st.markdown("<div class='screen'>SCREEN</div>", unsafe_allow_html=True)
            seatgrid = st.container(key="seatgrid")
            for row in seatmap["rows"]:
                cols = seatgrid.columns(seatmap["cols"] + 1, gap="small")
                cols[0].markdown(f"**{row}**")
                for c in range(1, seatmap["cols"] + 1):
                    code = f"{row}{c}"
                    is_taken = code in taken
                    is_sel = code in ss.selected
                    if cols[c].button("✕" if is_taken else str(c), key=f"seat-{sid}-{code}", disabled=is_taken,
                                      type="primary" if is_sel else "secondary", use_container_width=True):
                        ss.selected.symmetric_difference_update({code})
                        st.rerun()

            sel = sorted(ss.selected)
            total = scr["price"] * len(sel)
            st.divider()
            m1, m2, m3 = st.container(key="summary").columns(3)
            m1.metric("선택 좌석", ", ".join(sel) if sel else "-")
            m2.metric("결제 금액", won(total))
            m3.metric("내 포인트", f"{ss.user['points']:,}P" if ss.user else "로그인 필요")
            pay = st.button(f"💳 {won(total)} 포인트로 결제하기", type="primary", use_container_width=True,
                            disabled=not sel or not ss.user)
            if not ss.user:
                st.caption("결제하려면 로그인이 필요해요. (모바일은 왼쪽 위 **»** 메뉴에서 로그인)")
            if pay:
                data, err = api("POST", "/api/bookings", json={"screening_id": sid, "seats": sel})
                if err:
                    st.error(err)
                else:
                    ss.selected = set()
                    refresh_user()
                    ss.flash = f"🎉 예매 완료! {data['title']} {data['starts_at']} {', '.join(data['seats'])}"
                    ss.messages.append({"role": "assistant", "content":
                        f"예매가 완료되었어요! 🎉  \n**{data['title']}** · {data['starts_at']} · {data['theater']}  \n"
                        f"좌석 {', '.join(data['seats'])} · 결제 {won(data['total_price'])}"})
                    st.rerun()

    with st.expander("🔎 전체 상영작 둘러보기"):
        genres, _ = api("GET", "/api/genres")
        g = st.selectbox("장르", ["전체"] + (genres or []))
        lst, _ = api("GET", "/api/movies", params={} if g == "전체" else {"genre": g})
        card_grid(lst or [], "all", per_row=3)
