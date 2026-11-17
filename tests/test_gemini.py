"""Gemini 연동 테스트 (실제 API 호출 없이 가짜 HTTP 서버로 검증).

google-genai SDK 가 실제로 보내는 HTTP 요청을 가로채서
① 도구 정의가 전달되는지 ② 함수 호출 → 백엔드 실행 → 결과 전달 루프가 도는지
③ Gemini 3 의 thoughtSignature 가 다음 요청에 그대로 보존되는지 ④ 429(무료 한도 초과) 처리
를 확인한다.
"""
import json
import os
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "gemini.db"))

import httpx
import pytest
from google import genai
from google.genai import types

from backend import chatbot, seed
from backend.database import get_conn, init_db

SIGNATURE = "c2lnbmF0dXJlLWZyb20tZ2VtaW5p"


@pytest.fixture()
def fake_gemini(monkeypatch):
    requests_seen = []
    state = {"mode": "ok"}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        requests_seen.append(body)
        if state["mode"] == "429":
            return httpx.Response(429, json={"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        if len(requests_seen) == 1:  # 1차: 모델이 함수 호출을 요청
            return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"id": "call-1", "name": "search_movies", "args": {"genre": "로맨스"}},
                 "thoughtSignature": SIGNATURE}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"text": "설레는 로맨스 영화로 어바웃 타임과 라라랜드를 추천해요!"}]}, "finishReason": "STOP"}]})

    real_client = genai.Client

    def client_factory(*, api_key, http_options):
        opts = http_options.model_copy(update={"httpx_client": httpx.Client(transport=httpx.MockTransport(handler)),
                                               "retry_options": None})
        return real_client(api_key=api_key, http_options=opts)

    monkeypatch.setattr(genai, "Client", client_factory)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return requests_seen, state


@pytest.fixture()
def conn():
    init_db()
    with get_conn() as c:
        seed.bootstrap(c, use_external=False)
        yield c


def test_gemini_function_calling_loop(fake_gemini, conn):
    seen, _ = fake_gemini
    out = chatbot.chat(conn, [{"role": "user", "content": "여자친구랑 볼 설레는 영화"}], None)

    assert out["engine"].startswith("gemini:")
    assert "추천" in out["reply"]
    assert {m["title"] for m in out["movies"]} >= {"어바웃 타임", "라라랜드"}   # 도구가 실제 DB 를 조회함

    first, second = seen
    names = {f["name"] for f in first["tools"][0]["functionDeclarations"]}
    assert {"search_movies", "book_seats", "get_showtimes"} <= names
    assert "systemInstruction" in first

    model_turn, tool_turn = second["contents"][-2], second["contents"][-1]
    assert model_turn["parts"][0]["thoughtSignature"] == SIGNATURE            # 서명 보존
    fr = tool_turn["parts"][0]["functionResponse"]
    assert fr["name"] == "search_movies" and fr["id"] == "call-1"
    assert any(m["title"] == "라라랜드" for m in fr["response"]["movies"])


def test_gemini_rate_limit_falls_back(fake_gemini, conn):
    _, state = fake_gemini
    state["mode"] = "429"
    out = chatbot.chat(conn, [{"role": "user", "content": "요즘 인기 영화 뭐야?"}], None)
    assert out["engine"] == "rule" and "한도" in out["reply"] and "1위" in out["reply"]


def test_history_starts_with_user(fake_gemini, conn):
    seen, _ = fake_gemini
    chatbot.chat(conn, [{"role": "assistant", "content": "안녕하세요"},
                        {"role": "user", "content": "로맨스 추천"}], None)
    assert seen[0]["contents"][0]["role"] == "user"
