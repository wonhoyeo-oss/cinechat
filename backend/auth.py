"""쿠키 + 서버 세션 기반 인증.

- 비밀번호는 PBKDF2-HMAC-SHA256 (사용자별 salt, 200,000회 반복) 으로 해시해서 저장
- 로그인 성공 시 추측 불가능한 토큰(secrets.token_urlsafe)을 sessions 테이블에 저장하고
  HttpOnly / SameSite=Lax 쿠키로 내려준다 → JS 에서 쿠키 탈취(XSS) 방지
- 요청마다 쿠키의 토큰으로 세션을 조회하고 만료 여부를 검사한다
"""
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, HTTPException

from .database import get_db

SESSION_COOKIE = "cinechat_session"
SESSION_HOURS = 12
PBKDF2_ROUNDS = 200_000
SIGNUP_BONUS = 50_000  # 가입 시 지급하는 가상 포인트


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now','localtime')")
    conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)", (token, user_id, expires))
    return token


def delete_session(conn: sqlite3.Connection, token: str):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def current_user_optional(
    cinechat_session: str | None = Cookie(default=None),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not cinechat_session:
        return None
    row = conn.execute(
        """SELECT u.id, u.username, u.nickname, u.points, (u.telegram_chat_id IS NOT NULL) AS telegram_linked
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > datetime('now','localtime')""",
        (cinechat_session,),
    ).fetchone()
    return dict(row) if row else None


def current_user(user=Depends(current_user_optional)):
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user
