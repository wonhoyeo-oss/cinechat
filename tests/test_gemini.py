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


@pytest.fixture(autouse=True)
def _reset_model_state():
    chatbot._DISCOVERED_MODELS = None
    chatbot._MODEL_COOLDOWN.clear()
    chatbot._LAST_GOOD_MODEL = None
    yield


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


def test_thinking_level_low_by_default(fake_gemini, conn):
    seen, _ = fake_gemini
    out = chatbot.chat(conn, [{"role": "user", "content": "로맨스 추천"}], None)
    tc = seen[0]["generationConfig"]["thinkingConfig"]
    assert (tc.get("thinkingLevel") or tc.get("thinking_level")) == "LOW"
    assert out["llm_calls"] == 2 and out["elapsed"] >= 0


def test_thinking_unsupported_model_retries_without_it(fake_gemini, conn, monkeypatch):
    """thinking 설정을 거부(400)하는 모델이면 설정을 빼고 재시도해야 한다"""
    seen, state = fake_gemini
    real_handler_calls = []
    import httpx as _httpx
    orig_send = _httpx.Client.send

    def send(self, request, **kw):
        body = json.loads(request.content)
        if "thinkingConfig" in body.get("generationConfig", {}):
            real_handler_calls.append("rejected")
            return _httpx.Response(400, json={"error": {"code": 400, "message": "thinking not supported",
                                                        "status": "INVALID_ARGUMENT"}}, request=request)
        return orig_send(self, request, **kw)

    monkeypatch.setattr(_httpx.Client, "send", send)
    out = chatbot.chat(conn, [{"role": "user", "content": "로맨스 추천"}], None)
    assert real_handler_calls == ["rejected"]
    assert out["engine"].startswith("gemini:") and "추천" in out["reply"]


def test_overloaded_model_falls_back_to_other_gemini(monkeypatch, conn):
    """기본 모델이 503(과부하)이면 모델 목록에서 가벼운 모델을 찾아 그 모델로 답해야 한다"""
    chatbot._DISCOVERED_MODELS = None
    hits = []

    def handler(request: httpx.Request):
        path = request.url.path
        hits.append((request.method, path))
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-9-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-9-flash-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]},
            ]})
        if "gemini-4-hydrogen" in path:
            return httpx.Response(503, json={"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"text": "대체 모델이 답했어요"}]}, "finishReason": "STOP"}]})

    real_client = genai.Client

    def client_factory(*, api_key, http_options):
        opts = http_options.model_copy(update={"httpx_client": httpx.Client(transport=httpx.MockTransport(handler)),
                                               "retry_options": None})
        return real_client(api_key=api_key, http_options=opts)

    monkeypatch.setattr(genai, "Client", client_factory)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
    out = chatbot.chat(conn, [{"role": "user", "content": "안녕"}], None)
    assert out["engine"] == "gemini:gemini-9-flash-lite", out
    assert out["reply"] == "대체 모델이 답했어요"

    # 두 번째 질문: 실패한 기본 모델은 건너뛰고 곧바로 대체 모델로 가야 한다 (느린 재시도 없음)
    hits.clear()
    out = chatbot.chat(conn, [{"role": "user", "content": "또 안녕"}], None)
    assert out["engine"] == "gemini:gemini-9-flash-lite"
    assert not any("gemini-4-hydrogen" in p for _, p in hits), hits
    chatbot._DISCOVERED_MODELS = None
    chatbot._MODEL_COOLDOWN.clear()
    chatbot._LAST_GOOD_MODEL = None


def test_rule_greeting_has_no_key_warning_when_key_set(monkeypatch, conn):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    out = chatbot._rule_chat(conn, [{"role": "user", "content": "ㅎㅇ"}], None)
    assert "키가 없어" not in out["reply"] and "예매해줘" in out["reply"]
