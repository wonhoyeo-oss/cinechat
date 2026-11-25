"""메신저(텔레그램) 알림 발송.

구조
- 사이트 안 알림(notifications 테이블)은 예매 트랜잭션 안에서 기록한다 → 예매와 알림 기록이 함께 성공/실패.
- 실제 메신저 발송은 트랜잭션이 커밋된 '뒤'에 별도 스레드에서 한다.
  → 텔레그램 서버가 느리거나 장애가 나도 예매 처리는 기다리지 않고, DB 잠금을 오래 잡지 않는다.
- 아직 보내지 않은 알림(sent_at IS NULL)을 골라 보내므로, 발송이 실패해도 다음 기회에 다시 보낸다.

사용자 연결 절차
1) 사이트에서 '연결 링크 만들기' → 1회용 코드가 담긴 링크(https://t.me/<봇>?start=<코드>) 생성
2) 사용자가 링크를 눌러 텔레그램에서 '시작' → 봇이 '/start <코드>' 메시지를 받음
3) 사이트에서 '연결 확인' → getUpdates 로 그 코드를 보낸 대화(chat_id)를 찾아 사용자와 연결
   (웹훅 서버 없이도 동작하므로 무료 서버가 잠들어 있어도 연결 가능)

환경변수: TELEGRAM_BOT_TOKEN (@BotFather 에서 발급)
"""
import logging
import os
import secrets
import threading

import requests

from .database import connect

log = logging.getLogger("cinechat.notifier")
API_BASE = "https://api.telegram.org"
TIMEOUT = 8
_bot_username: str | None = None
_dispatch_lock = threading.Lock()


def token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def enabled() -> bool:
    return bool(token())


def _api(method: str, **params) -> dict:
    """텔레그램 Bot API 호출. 테스트에서는 이 함수만 바꿔 끼운다."""
    r = requests.post(f"{API_BASE}/bot{token()}/{method}", json=params, timeout=TIMEOUT)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "telegram error"))
    return data["result"]


def bot_username() -> str | None:
    global _bot_username
    if _bot_username is None and enabled():
        try:
            _bot_username = _api("getMe")["username"]
        except Exception as e:
            log.warning("텔레그램 봇 정보 조회 실패: %s", e)
    return _bot_username


def create_link(conn, user_id: int) -> dict:
    if not enabled():
        raise RuntimeError("텔레그램 알림이 설정되지 않았습니다 (TELEGRAM_BOT_TOKEN 없음).")
    name = bot_username()
    if not name:
        raise RuntimeError("텔레그램 봇에 연결할 수 없습니다. 봇 토큰을 확인해 주세요.")
    code = secrets.token_urlsafe(9)
    conn.execute("UPDATE users SET telegram_link_code=? WHERE id=?", (code, user_id))
    return {"url": f"https://t.me/{name}?start={code}", "code": code}


def confirm_link(conn, user_id: int) -> bool:
    """봇이 받은 메시지 중 이 사용자의 1회용 코드가 담긴 '/start <코드>'를 찾아 대화방을 연결한다."""
    row = conn.execute("SELECT telegram_link_code, nickname FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["telegram_link_code"]:
        return False
    code = row["telegram_link_code"]
    for upd in reversed(_api("getUpdates", limit=100, allowed_updates=["message"])):
        msg = upd.get("message") or {}
        if msg.get("text", "").strip() == f"/start {code}":
            chat_id = str(msg["chat"]["id"])
            conn.execute("UPDATE users SET telegram_chat_id=?, telegram_link_code=NULL WHERE id=?", (chat_id, user_id))
            try:
                _api("sendMessage", chat_id=chat_id,
                     text=f"🎬 CineChat 알림이 연결되었어요, {row['nickname']}님!\n"
                          "예매 완료·취소, 취소표 자동 예매 소식을 여기로 보내드릴게요.")
            except Exception as e:
                log.warning("연결 환영 메시지 실패: %s", e)
            return True
    return False


def unlink(conn, user_id: int):
    conn.execute("UPDATE users SET telegram_chat_id=NULL, telegram_link_code=NULL WHERE id=?", (user_id,))


def dispatch_pending() -> int:
    """아직 메신저로 보내지 않은 알림을 발송한다. 같은 알림이 두 번 가지 않도록 '선점' 후 보낸다."""
    if not enabled():
        return 0
    sent = 0
    with _dispatch_lock:
        conn = connect()
        try:
            rows = conn.execute(
                """SELECT n.id, n.message, u.telegram_chat_id FROM notifications n JOIN users u ON u.id = n.user_id
                    WHERE n.sent_at IS NULL AND u.telegram_chat_id IS NOT NULL ORDER BY n.id LIMIT 50"""
            ).fetchall()
            for r in rows:
                claimed = conn.execute(
                    "UPDATE notifications SET sent_at='sending' WHERE id=? AND sent_at IS NULL", (r["id"],)
                ).rowcount
                if not claimed:
                    continue
                try:
                    _api("sendMessage", chat_id=r["telegram_chat_id"], text=f"🎬 CineChat\n{r['message']}")
                    conn.execute("UPDATE notifications SET sent_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                    sent += 1
                except Exception as e:
                    log.warning("텔레그램 발송 실패(알림 %s): %s", r["id"], e)
                    conn.execute("UPDATE notifications SET sent_at=NULL WHERE id=?", (r["id"],))  # 다음에 재시도
                    break
        finally:
            conn.close()
    return sent


def dispatch_async():
    """요청 처리를 기다리게 하지 않도록 백그라운드 스레드에서 발송."""
    if enabled():
        threading.Thread(target=dispatch_pending, daemon=True, name="telegram-dispatch").start()
