"""The provider layer: model choice, JSON extraction, validation and retry.

Nothing here touches the network; the OpenAI-compatible transport is stubbed.
"""
from __future__ import annotations

import json

import pytest

from clipforge.core import llm
from clipforge.core.config import get_settings
from clipforge.ingest.rules import BriefRules


@pytest.fixture()
def groq(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key-not-real")
    monkeypatch.delenv("LLM_MODEL_BRIEF", raising=False)
    monkeypatch.delenv("LLM_MODEL_SCORING", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture()
def anthropic_settings(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("LLM_MODEL_BRIEF", raising=False)
    monkeypatch.delenv("LLM_MODEL_SCORING", raising=False)
    yield get_settings()
    get_settings.cache_clear()


def reply(content: str, *, finish: str = "stop", status: int = 200):
    """One fake OpenAI-compatible response."""
    return status, {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def stub_post(monkeypatch, replies):
    """Serve `replies` in order, recording each request body."""
    import httpx

    sent: list[dict] = []
    queue = list(replies)

    class FakeResponse:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
            # Real responses carry these; the code reads them to report the quota.
            self.headers = {"content-type": "application/json",
                            "x-ratelimit-limit-tokens": "8000",
                            "x-ratelimit-remaining-tokens": "7907"}

        def json(self):
            if not isinstance(self._payload, dict):
                raise ValueError("not json")
            return self._payload

    def fake_post(url, *, json=None, timeout=None, headers=None):
        sent.append({"url": url, "body": json, "headers": headers})
        status, payload = queue.pop(0)
        return FakeResponse(status, payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


VALID = json.dumps({
    "approved_source_urls": ["https://youtu.be/dQw4w9WgXcQ"],
    "approved_source_notes": "",
    "clip_min_seconds": 20, "clip_max_seconds": 60,
    "allowed_platforms": ["youtube_shorts"],
    "required_hashtags": [], "required_mentions": [],
    "disclosure_text": None, "caption_requirements": None,
    "min_creator_frame_share": None,
    "banned_content": [], "banned_edits": [],
    "pre_approval": False,
    "rate_per_1k": None, "clip_cap_usd": None, "min_views": None,
    "country_restrictions": [], "deadline": None, "uncertain": [],
})


# --- provider selection ----------------------------------------------------

def test_groq_defaults_and_endpoint(groq):
    assert groq.llm_api_key == "test-key-not-real"
    assert groq.llm_key_name == "GROQ_API_KEY"
    assert llm.base_url(groq) == "https://api.groq.com/openai/v1"
    assert llm.model_for(groq, "brief") == llm.DEFAULT_MODELS["groq"]["brief"]
    assert llm.model_for(groq, "scoring") == llm.DEFAULT_MODELS["groq"]["scoring"]


def test_anthropic_defaults(anthropic_settings):
    assert anthropic_settings.llm_key_name == "ANTHROPIC_API_KEY"
    assert llm.model_for(anthropic_settings, "brief") == "claude-opus-5"
    assert llm.model_for(anthropic_settings, "scoring") == "claude-haiku-4-5"


def test_env_overrides_the_default_model(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL_SCORING", "something-specific")
    assert llm.model_for(get_settings(), "scoring") == "something-specific"
    get_settings.cache_clear()


def test_unknown_provider_is_a_clear_error(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("LLM_PROVIDER", "wishful")
    monkeypatch.delenv("LLM_MODEL_BRIEF", raising=False)
    with pytest.raises(llm.LLMUnavailable, match="not one of"):
        llm.model_for(get_settings(), "brief")
    get_settings.cache_clear()


def test_missing_key_names_the_right_variable(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    settings = get_settings()
    with pytest.raises(llm.LLMUnavailable, match="GROQ_API_KEY"):
        llm.parse(BriefRules, "s", "u", settings=settings)
    assert "GROQ_API_KEY" in settings.missing_for(1)
    get_settings.cache_clear()


# --- the request ------------------------------------------------------------

def test_sends_key_as_bearer_and_schema_in_the_prompt(groq, monkeypatch):
    sent = stub_post(monkeypatch, [reply(VALID)])
    rules = llm.parse(BriefRules, "SYSTEM TEXT", "USER TEXT", settings=groq)
    assert rules.clip_max_seconds == 60
    body, headers = sent[0]["body"], sent[0]["headers"]
    assert headers["Authorization"] == "Bearer test-key-not-real"
    assert sent[0]["url"].endswith("/chat/completions")
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    system = body["messages"][0]["content"]
    assert "SYSTEM TEXT" in system
    assert "approved_source_urls" in system      # the schema travels with the prompt
    assert body["messages"][1]["content"] == "USER TEXT"


def test_strips_a_markdown_fence(groq, monkeypatch):
    stub_post(monkeypatch, [reply(f"```json\n{VALID}\n```")])
    assert llm.parse(BriefRules, "s", "u", settings=groq).clip_min_seconds == 20


def test_tolerates_commentary_around_the_object(groq, monkeypatch):
    stub_post(monkeypatch, [reply(f"Sure, here you go:\n{VALID}\nHope that helps!")])
    assert llm.parse(BriefRules, "s", "u", settings=groq).clip_min_seconds == 20


def test_retries_once_with_the_validation_error(groq, monkeypatch):
    bad = json.dumps({"clip_min_seconds": "not a number"})
    sent = stub_post(monkeypatch, [reply(bad), reply(VALID)])
    rules = llm.parse(BriefRules, "s", "u", settings=groq)
    assert rules.clip_min_seconds == 20
    # The second attempt must carry the failed answer and the reason back.
    follow_up = sent[1]["body"]["messages"]
    assert follow_up[-2]["role"] == "assistant"
    assert "did not validate" in follow_up[-1]["content"]


def test_gives_up_after_two_attempts(groq, monkeypatch):
    stub_post(monkeypatch, [reply("not json at all"), reply("still not json")])
    with pytest.raises(llm.LLMBadAnswer, match="2 attempts"):
        llm.parse(BriefRules, "s", "u", settings=groq)


def test_truncated_answer_is_not_treated_as_valid(groq, monkeypatch):
    stub_post(monkeypatch, [reply(VALID[:80], finish="length"),
                            reply(VALID[:80], finish="length")])
    with pytest.raises(llm.LLMBadAnswer, match="ceiling"):
        llm.parse(BriefRules, "s", "u", settings=groq)


@pytest.mark.parametrize("status, match", [
    (401, "rejected"),
    (404, "llm-check"),
    (400, "HTTP 400"),
])
def test_http_errors_explain_themselves(groq, monkeypatch, status, match):
    stub_post(monkeypatch, [(status, {"error": "nope"})])
    with pytest.raises((llm.LLMBadAnswer, llm.LLMUnavailable), match=match):
        llm.parse(BriefRules, "s", "u", settings=groq)


def test_a_bad_key_is_unavailable_not_a_bad_answer(groq, monkeypatch):
    """401 must be LLMUnavailable so the job holds for a human instead of retrying."""
    stub_post(monkeypatch, [(401, {"error": "invalid api key"})])
    with pytest.raises(llm.LLMUnavailable):
        llm.parse(BriefRules, "s", "u", settings=groq)


def test_schema_text_is_json(groq):
    assert "approved_source_urls" in json.loads(llm.schema_text(BriefRules))["properties"]


def test_extract_json_rejects_a_reply_with_no_object():
    with pytest.raises(llm.LLMBadAnswer, match="no JSON object"):
        llm.extract_json("I would rather not.")


# --- rate limits ------------------------------------------------------------
# Measured against Groq on 13 Nov 2026: the free tier allows 8,000 tokens a
# minute on openai/gpt-oss-120b. A five-hour transcript asked for 22,371 and was
# refused with HTTP 413. Waiting cannot fix a request bigger than the whole
# per-minute allowance, so the two cases must not be confused.

TPM_REFUSAL = {"error": {
    "message": ("Request too large for model `openai/gpt-oss-120b` in organization `org_x` "
                "service tier `on_demand` on tokens per minute (TPM): Limit 8000, "
                "Requested 22371, please reduce your message size and try again."),
    "type": "tokens", "code": "rate_limit_exceeded"}}

TRANSIENT = {"error": {
    "message": "Rate limit reached for model. Please try again in 1.5s.",
    "type": "tokens", "code": "rate_limit_exceeded"}}


def test_a_request_over_the_whole_allowance_is_not_retried(groq, monkeypatch):
    sent = stub_post(monkeypatch, [(413, TPM_REFUSAL)])
    with pytest.raises(llm.LLMRequestTooLarge) as caught:
        llm.parse(BriefRules, "s", "u", settings=groq)
    assert caught.value.limit == 8000
    assert caught.value.requested == 22371
    assert "send less at a time" in str(caught.value)
    assert len(sent) == 1          # not retried: waiting cannot help


def test_a_transient_rate_limit_is_waited_out(groq, monkeypatch):
    sent = stub_post(monkeypatch, [(429, TRANSIENT), (200, {
        "choices": [{"message": {"content": VALID}, "finish_reason": "stop"}],
        "usage": {}})])
    waited: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", waited.append)
    rules = llm.parse(BriefRules, "s", "u", settings=groq)
    assert rules.clip_max_seconds == 60
    assert len(sent) == 2
    assert waited == [1.5]         # the delay the provider asked for


def test_the_providers_own_delay_is_parsed_with_minutes():
    assert llm._rate_limit_delay("Please try again in 2m59.56s") == pytest.approx(179.56)
    assert llm._rate_limit_delay("try again in 0.5s") == pytest.approx(0.5)
    assert llm._rate_limit_delay("no idea") is None


def test_a_capped_wait_is_used_when_the_provider_asks_for_too_long(groq, monkeypatch):
    long_wait = {"error": {"message": "Please try again in 30m0s.", "code": "x"}}
    stub_post(monkeypatch, [(429, long_wait), (200, {
        "choices": [{"message": {"content": VALID}, "finish_reason": "stop"}],
        "usage": {}})])
    waited: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", waited.append)
    llm.parse(BriefRules, "s", "u", settings=groq)
    assert waited == [llm.MAX_RATE_LIMIT_WAIT]


def test_it_gives_up_after_enough_waits(groq, monkeypatch):
    stub_post(monkeypatch, [(429, TRANSIENT)] * (llm.RATE_LIMIT_RETRIES + 1))
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with pytest.raises(llm.LLMBadAnswer, match="still rate limited"):
        llm.parse(BriefRules, "s", "u", settings=groq)


def test_a_413_that_is_not_a_token_limit_is_still_retried(groq, monkeypatch):
    """Only a stated limit smaller than the request proves waiting is futile."""
    vague = {"error": {"message": "Payload too large"}}
    stub_post(monkeypatch, [(413, vague), (200, {
        "choices": [{"message": {"content": VALID}, "finish_reason": "stop"}],
        "usage": {}})])
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    assert llm.parse(BriefRules, "s", "u", settings=groq).clip_min_seconds == 20


# --- token thrift -----------------------------------------------------------
# On a per-minute budget the answer costs as much as the question. Measured
# 13 Nov 2026: the same small answer cost 71 output tokens by default and 34 at
# reasoning_effort "low", and leaving max_tokens at 16,000 allowed roughly one
# request a minute.

def test_reasoning_effort_is_sent_to_gpt_oss(groq, monkeypatch):
    sent = stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", settings=groq)
    assert sent[0]["body"]["reasoning_effort"] == "low"


def test_reasoning_effort_is_not_sent_to_other_models(groq, monkeypatch):
    """An unknown parameter is a 400 on a model that does not take it."""
    sent = stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", model="llama-3.3-70b", settings=groq)
    assert "reasoning_effort" not in sent[0]["body"]


def test_reasoning_effort_can_be_switched_off(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    sent = stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", settings=get_settings())
    assert "reasoning_effort" not in sent[0]["body"]
    get_settings.cache_clear()


def test_max_tokens_is_passed_through(groq, monkeypatch):
    sent = stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", max_tokens=1234, settings=groq)
    assert sent[0]["body"]["max_tokens"] == 1234


def test_the_answer_ceilings_are_modest():
    """A regression guard: these exist to fit a per-minute token budget."""
    from clipforge.ingest import tagger
    from clipforge.moments import score

    assert tagger.WINDOW_ANSWER_TOKENS <= 2000
    assert tagger.REDUCE_ANSWER_TOKENS <= 2000
    assert score.ANSWER_TOKENS <= 4000


# --- pacing -----------------------------------------------------------------
# Blind retrying was the wrong strategy: a refused request debits the same
# per-minute bucket it is waiting for, so waiting after a refusal is strictly
# worse than not earning one.

def test_a_request_waits_for_room_before_being_sent(groq, monkeypatch):
    from clipforge.core import budget as budget_mod

    budget_mod.reset()
    allowance = budget_mod.for_model(llm.model_for(groq, "brief"))
    allowance.update({"x-ratelimit-limit-tokens": "8000",
                      "x-ratelimit-remaining-tokens": "100",
                      "x-ratelimit-reset-tokens": "60s"})
    slept: list[float] = []
    monkeypatch.setattr(budget_mod.time, "sleep", slept.append)
    stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s" * 3000, "u" * 3000, settings=groq)
    assert slept, "it should have waited for room rather than being refused"
    budget_mod.reset()


def test_the_first_request_does_not_wait_on_a_guess(groq, monkeypatch):
    from clipforge.core import budget as budget_mod

    budget_mod.reset()
    slept: list[float] = []
    monkeypatch.setattr(budget_mod.time, "sleep", slept.append)
    stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", settings=groq)
    assert slept == []
    budget_mod.reset()


def test_the_allowance_is_learned_from_the_response(groq, monkeypatch):
    from clipforge.core import budget as budget_mod

    budget_mod.reset()
    stub_post(monkeypatch, [reply(VALID)])
    llm.parse(BriefRules, "s", "u", settings=groq)
    allowance = budget_mod.for_model(llm.model_for(groq, "brief"))
    assert allowance.limit == 8000          # from the stub's headers
    budget_mod.reset()


# --- the daily allowance ----------------------------------------------------
# The real limit, captured from a live 429 on 13 Nov 2026: 200,000 tokens a DAY.
# Groq says so only in the body; the x-ratelimit-* headers report the
# per-minute bucket and read completely full while the daily cap is what is
# refusing the request. Six 90-second waits against a daily cap are noise.

DAILY = {"error": {"message": (
    "Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` "
    "service tier `on_demand` on tokens per day (TPD): Limit 200000, Used 195830, "
    "Requested 4534. Please try again in 2m37.248s. Need more tokens? Upgrade to "
    "Dev Tier today at https://console.groq.com/settings/billing"),
    "type": "tokens", "code": "rate_limit_exceeded"}}


def test_a_spent_daily_allowance_is_not_retried(groq, monkeypatch):
    sent = stub_post(monkeypatch, [(429, DAILY)])
    waited: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", waited.append)
    with pytest.raises(llm.LLMQuotaExhausted) as caught:
        llm.parse(BriefRules, "s", "u", settings=groq)
    assert caught.value.limit == 200000
    assert caught.value.used == 195830
    assert caught.value.retry_after == pytest.approx(157.248)
    assert len(sent) == 1, "a daily cap must not be retried"
    assert waited == [], "and must not be waited out"


def test_the_message_says_what_to_do_about_it(groq, monkeypatch):
    stub_post(monkeypatch, [(429, DAILY)])
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with pytest.raises(llm.LLMQuotaExhausted, match="195,830 of 200,000"):
        llm.parse(BriefRules, "s", "u", settings=groq)


def test_a_per_minute_limit_is_still_waited_out(groq, monkeypatch):
    """The two must not be confused: one is transient, one is not."""
    sent = stub_post(monkeypatch, [(429, TRANSIENT), reply(VALID)])
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    assert llm.parse(BriefRules, "s", "u", settings=groq).clip_max_seconds == 60
    assert len(sent) == 2


# --- gemini ---------------------------------------------------------------

@pytest.fixture()
def gemini(tmp_path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("LLM_MODEL_BRIEF", raising=False)
    monkeypatch.delenv("LLM_MODEL_SCORING", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    yield get_settings()


def test_gemini_uses_its_own_key_and_endpoint(gemini):
    assert gemini.llm_api_key == "test-key-not-real"
    assert gemini.llm_key_name == "GEMINI_API_KEY"
    assert "generativelanguage.googleapis.com" in llm.base_url(gemini)
    assert llm.model_for(gemini, "scoring") == llm.DEFAULT_MODELS["gemini"]["scoring"]


def test_gemini_does_not_borrow_the_groq_key(gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "groq-only")
    get_settings.cache_clear()
    with pytest.raises(llm.LLMUnavailable, match="GEMINI_API_KEY"):
        llm.parse(BriefRules, "s", "u", settings=get_settings())


GEMINI_DAILY = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
    "Quota exceeded for metric: generate_content_free_tier_requests, "
    "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier. "
    "Please retry in 41.5s."}}


def test_a_spent_gemini_daily_quota_is_held_not_retried(gemini, monkeypatch):
    sent = stub_post(monkeypatch, [(429, GEMINI_DAILY)])
    waited: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", waited.append)
    with pytest.raises(llm.LLMQuotaExhausted, match="Gemini"):
        llm.parse(BriefRules, "s", "u", settings=gemini)
    assert len(sent) == 1 and waited == []


def test_a_gemini_per_minute_limit_is_waited_out(gemini, monkeypatch):
    transient = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message":
        "Quota exceeded for quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}}
    sent = stub_post(monkeypatch, [(429, transient), reply(VALID)])
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    assert llm.parse(BriefRules, "s", "u", settings=gemini).clip_max_seconds == 60
    assert len(sent) == 2


def test_a_busy_server_is_retried_then_succeeds(gemini, monkeypatch):
    busy = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}}
    sent = stub_post(monkeypatch, [(503, busy), (503, busy), reply(VALID)])
    waited: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", waited.append)
    assert llm.parse(BriefRules, "s", "u", settings=gemini).clip_max_seconds == 60
    assert len(sent) == 3 and len(waited) == 2


def test_a_server_that_stays_busy_gives_up(gemini, monkeypatch):
    busy = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "high demand"}}
    sent = stub_post(monkeypatch, [(503, busy)] * 10)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    with pytest.raises(llm.LLMBadAnswer, match="503"):
        llm.parse(BriefRules, "s", "u", settings=gemini)
    assert len(sent) == llm.SERVER_RETRIES + 1
