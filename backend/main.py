"""CineChat REST API 서버 (FastAPI).

실행:  uvicorn backend.main:app --reload --port 8000
문서:  http://localhost:8000/docs  (Swagger UI 자동 생성)
"""
import logging
import os
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from . import auth, chatbot, notifier, seed, services
from .database import get_conn, get_db, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with get_conn() as conn:
        seed.bootstrap(conn, use_external=os.getenv("SKIP_EXTERNAL_SYNC") != "1")
    yield


app = FastAPI(title="CineChat API", version="1.0.0",
              description="LLM 기반 대화형 AI 챗봇을 결합한 영화 추천 및 모의 예매 플랫폼", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:8501").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(services.BookingError)
async def booking_error_handler(_: Request, exc: services.BookingError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


# ───────────────────────── 요청 스키마 ─────────────────────────
class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=4, max_length=64)
    nickname: str = Field(min_length=1, max_length=20)


class LoginIn(BaseModel):
    username: str
    password: str


class BookingIn(BaseModel):
    screening_id: int
    seats: list[str] = Field(min_length=1, max_length=services.MAX_SEATS_PER_BOOKING)


class ChargeIn(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)


class WaitlistIn(BaseModel):
    screening_id: int
    seat_count: int = Field(ge=1, le=services.MAX_WAITLIST_SEATS)


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=2000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


# ───────────────────────── 인증 ─────────────────────────
def _set_session_cookie(resp: Response, token: str):
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_HOURS * 3600, secure=os.getenv("COOKIE_SECURE") == "1")


@app.post("/api/auth/signup", status_code=201, tags=["auth"])
def signup(body: SignupIn, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    pw_hash, salt = auth.hash_password(body.password)
    conn.execute("BEGIN")
    try:
        uid = conn.execute(
            "INSERT INTO users(username, password_hash, salt, nickname, points) VALUES (?,?,?,?,?)",
            (body.username, pw_hash, salt, body.nickname, auth.SIGNUP_BONUS),
        ).lastrowid
        conn.execute("INSERT INTO point_history(user_id, amount, reason) VALUES (?,?,?)",
                     (uid, auth.SIGNUP_BONUS, "회원가입 축하 포인트"))
        token = auth.create_session(conn, uid)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        raise HTTPException(409, "이미 사용 중인 아이디입니다.")
    _set_session_cookie(response, token)
    return {"id": uid, "username": body.username, "nickname": body.nickname, "points": auth.SIGNUP_BONUS}


@app.post("/api/auth/login", tags=["auth"])
def login(body: LoginIn, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM users WHERE username = ?", (body.username,)).fetchone()
    if not row or not auth.verify_password(body.password, row["password_hash"], row["salt"]):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    token = auth.create_session(conn, row["id"])
    _set_session_cookie(response, token)
    return {"id": row["id"], "username": row["username"], "nickname": row["nickname"], "points": row["points"]}


@app.post("/api/auth/logout", tags=["auth"])
def logout(request: Request, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.delete_session(conn, token)
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/auth/me", tags=["auth"])
def me(user=Depends(auth.current_user)):
    return user


# ───────────────────────── 영화 / 상영 ─────────────────────────
@app.get("/api/movies", tags=["movies"])
def movies(q: str | None = None, genre: str | None = None, limit: int = 30,
           conn: sqlite3.Connection = Depends(get_db)):
    return services.list_movies(conn, query=q, genre=genre, limit=min(limit, 100))


@app.get("/api/movies/boxoffice", tags=["movies"])
def boxoffice(conn: sqlite3.Connection = Depends(get_db)):
    return services.box_office(conn)


@app.get("/api/genres", tags=["movies"])
def genres(conn: sqlite3.Connection = Depends(get_db)):
    return services.all_genres(conn)


@app.get("/api/movies/{movie_id}", tags=["movies"])
def movie_detail(movie_id: int, conn: sqlite3.Connection = Depends(get_db)):
    m = services.get_movie(conn, movie_id)
    if not m:
        raise HTTPException(404, "영화를 찾을 수 없습니다.")
    return m


@app.get("/api/movies/{movie_id}/screenings", tags=["screenings"])
def movie_screenings(movie_id: int, date: str | None = None, conn: sqlite3.Connection = Depends(get_db)):
    return services.list_screenings(conn, movie_id=movie_id, date=date)


@app.get("/api/screenings/{screening_id}/seats", tags=["screenings"])
def screening_seats(screening_id: int, conn: sqlite3.Connection = Depends(get_db)):
    return services.seat_map(conn, screening_id)


# ───────────────────────── 예매 / 포인트 ─────────────────────────
@app.post("/api/bookings", status_code=201, tags=["bookings"])
def book(body: BookingIn, user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.create_booking(conn, user["id"], body.screening_id, body.seats)


@app.get("/api/bookings", tags=["bookings"])
def bookings(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.my_bookings(conn, user["id"])


@app.delete("/api/bookings/{booking_id}", tags=["bookings"])
def cancel(booking_id: int, user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.cancel_booking(conn, user["id"], booking_id)


@app.post("/api/points/charge", tags=["points"])
def charge(body: ChargeIn, user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return {"points": services.charge_points(conn, user["id"], body.amount)}


@app.get("/api/points/history", tags=["points"])
def history(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.point_history(conn, user["id"])


# ───────────────────────── 취소표 대기열 / 알림 ─────────────────────────
@app.post("/api/waitlist", status_code=201, tags=["waitlist"])
def join_waitlist(body: WaitlistIn, user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.join_waitlist(conn, user["id"], body.screening_id, body.seat_count)


@app.get("/api/waitlist", tags=["waitlist"])
def my_waitlist(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.my_waitlist(conn, user["id"])


@app.delete("/api/waitlist/{waitlist_id}", tags=["waitlist"])
def cancel_waitlist(waitlist_id: int, user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return services.cancel_waitlist(conn, user["id"], waitlist_id)


@app.get("/api/notifications", tags=["notifications"])
def notifications(unread: bool = False, user=Depends(auth.current_user),
                  conn: sqlite3.Connection = Depends(get_db)):
    return services.my_notifications(conn, user["id"], only_unread=unread)


@app.post("/api/notifications/read", tags=["notifications"])
def read_notifications(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    return {"updated": services.mark_notifications_read(conn, user["id"])}


# ───────────────────────── 메신저(텔레그램) 알림 연결 ─────────────────────────
@app.post("/api/telegram/link", tags=["notifications"])
def telegram_link(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        return notifier.create_link(conn, user["id"])
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.post("/api/telegram/confirm", tags=["notifications"])
def telegram_confirm(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    try:
        linked = notifier.confirm_link(conn, user["id"])
    except Exception as e:
        raise HTTPException(503, f"텔레그램 서버에 연결할 수 없습니다: {e}")
    if linked:
        notifier.dispatch_async()
    return {"linked": linked}


@app.delete("/api/telegram", tags=["notifications"])
def telegram_unlink(user=Depends(auth.current_user), conn: sqlite3.Connection = Depends(get_db)):
    notifier.unlink(conn, user["id"])
    return {"linked": False}


# ───────────────────────── 챗봇 ─────────────────────────
@app.post("/api/chat", tags=["chat"])
def chat(body: ChatIn, user=Depends(auth.current_user_optional), conn: sqlite3.Connection = Depends(get_db)):
    return chatbot.chat(conn, [m.model_dump() for m in body.messages], user)


# ───────────────────────── 관리 ─────────────────────────
@app.post("/api/admin/sync", tags=["admin"])
def sync(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    admin_token = os.getenv("ADMIN_TOKEN")
    if not admin_token or request.headers.get("X-Admin-Token") != admin_token:
        raise HTTPException(403, "관리자 토큰이 필요합니다.")
    result = seed.sync_external(conn)
    seed.ensure_schedule(conn)
    return result


@app.get("/api/health", tags=["admin"])
def health():
    return {"ok": True, "llm": (chatbot.llm_config() or {}).get("provider", "rule-based"),
            "tmdb": bool(os.getenv("TMDB_API_KEY")), "kobis": bool(os.getenv("KOBIS_API_KEY")),
            "telegram": notifier.enabled()}
