"""The one place ClipForge talks to a hosted model.

PRD section 14: the laptop never runs a model locally. Brief parsing uses the
larger model; per-clip scoring and copy use a small fast one, because there is
one of those calls per candidate moment.

Two providers, chosen with LLM_PROVIDER:

- `anthropic` - the native SDK, with schema-validated structured output.
- `groq` - any OpenAI-compatible endpoint. Groq's free tier serves open-weight
  models, so this route costs nothing.
- `gemini` - Google's OpenAI-compatible endpoint. Free tier, but Google may use
  free-tier prompts to improve its products; see PROGRESS.md before using it on
  campaign material.

Both return a validated Pydantic instance, so a malformed answer fails here
rather than halfway down the pipeline. Open-weight models are less reliable at
holding to a schema, so the OpenAI-compatible path states the schema in the
prompt, validates, and retries once with the validation error attached.

The key is read from Settings and never logged.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import budget as budget_mod
from .config import Settings, get_settings

log = logging.getLogger("clipforge.llm")

T = TypeVar("T", bound=BaseModel)

# Adaptive thinking and `effort` are not accepted by every Anthropic model:
# Haiku 4.5 rejects `effort` and takes the older budget_tokens form of thinking.
ADAPTIVE_THINKING = frozenset({
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-fable-5-1",
})

# Defaults per provider. The Groq ids were checked against the live catalogue
# on 12 Nov 2026; Groq retires models often, so run
# `python -m clipforge llm-check` if a call starts returning 404.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "anthropic": {"brief": "claude-opus-5", "scoring": "claude-haiku-4-5"},
    "groq": {"brief": "openai/gpt-oss-120b", "scoring": "openai/gpt-oss-20b"},
    # 2.5 models answer 404 to new keys (13 Nov 2026) though the catalogue lists
    # them; 3.8-flash timed out in a probe. This is the one seen to answer.
    "gemini": {"brief": "gemini-3.5-flash-lite", "scoring": "gemini-3.5-flash-lite"},
}

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

JSON_INSTRUCTION = (
    "Answer with a single JSON object and nothing else: no prose before or after it, "
    "no markdown code fence. It must validate against this JSON Schema:\n\n{schema}\n\n"
    "Every property in the schema must be present. Where the input does not provide a "
    "value, use the empty form the schema allows for that property: \"\" for a string, "
    "[] for an array, false for a boolean, and null only where the schema lists null as "
    "a permitted type. Never invent a value to fill a field."
)


class LLMUnavailable(RuntimeError):
    """No API key, or the client is not installed. The caller should hold the job."""


class LLMBadAnswer(RuntimeError):
    """The model answered, but not in the shape the schema demands."""


class LLMQuotaExhausted(RuntimeError):
    """The provider's DAILY allowance is gone. Waiting tonight will not help.

    Groq's free tier is 200,000 tokens a day, and it says so only in the body of
    the 429 -- the x-ratelimit-* headers report the per-minute bucket, which can
    read completely full while the daily cap is what is actually refusing the
    request. Measured 13 Nov 2026: "tokens per day (TPD): Limit 200000, Used
    195830". So this is held for a human rather than retried; six 90-second
    waits against a daily cap are just noise in the log.
    """

    def __init__(self, message: str, *, limit: int | None = None,
                 used: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.limit = limit
        self.used = used
        self.retry_after = retry_after


class LLMRequestTooLarge(RuntimeError):
    """One request exceeded the provider's per-minute token limit.

    Distinct from a transient rate limit: waiting cannot help, because a single
    request can never fit inside a limit smaller than itself. The caller has to
    send less. Groq's free tier allows 8,000 tokens a minute on
    openai/gpt-oss-120b, which a five-hour transcript passes several times over.
    """

    def __init__(self, message: str, *, limit: int | None = None,
                 requested: int | None = None):
        super().__init__(message)
        self.limit = limit
        self.requested = requested


def model_for(settings: Settings, role: str) -> str:
    """The model for 'brief' or 'scoring', falling back to the provider default."""
    configured = settings.llm_model_brief if role == "brief" else settings.llm_model_scoring
    if configured:
        return configured
    defaults = DEFAULT_MODELS.get(settings.llm_provider)
    if not defaults:
        raise LLMUnavailable(
            f"LLM_PROVIDER={settings.llm_provider!r} is not one of "
            f"{sorted(DEFAULT_MODELS)}; set LLM_MODEL_BRIEF and LLM_MODEL_SCORING "
            f"explicitly if you are using a custom endpoint"
        )
    return defaults[role]


def base_url(settings: Settings) -> str:
    if settings.llm_base_url:
        return settings.llm_base_url
    try:
        return BASE_URLS[settings.llm_provider]
    except KeyError:
        raise LLMUnavailable(
            f"no base URL known for LLM_PROVIDER={settings.llm_provider!r}; set LLM_BASE_URL"
        ) from None


def _require_key(settings: Settings) -> str:
    key = settings.llm_api_key
    if not key:
        raise LLMUnavailable(
            f"{settings.llm_key_name} is not set in .env; ClipForge cannot parse briefs "
            f"or score moments without it"
        )
    return key


# --------------------------------------------------------------------------
# schema handling
# --------------------------------------------------------------------------

def schema_text(output_model: type[BaseModel]) -> str:
    return json.dumps(output_model.model_json_schema(), indent=1, sort_keys=True)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a reply, tolerating a stray fence or preamble."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost braces, for a model that adds commentary.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise LLMBadAnswer(f"no JSON object in the reply: {cleaned[:300]!r}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMBadAnswer(f"unparseable JSON in the reply: {exc}") from exc


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

def _parse_anthropic(
    output_model: type[T], system: str, user: str, *, model: str, max_tokens: int,
    settings: Settings,
) -> T:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - requirements install it
        raise LLMUnavailable("the anthropic package is not installed") from exc
    client = anthropic.Anthropic(api_key=_require_key(settings), max_retries=3)
    kwargs: dict[str, Any] = {}
    if model in ADAPTIVE_THINKING:
        kwargs["thinking"] = {"type": "adaptive"}
    try:
        response = client.messages.parse(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_format=output_model, **kwargs,
        )
    except Exception as exc:
        # Never let an SDK error carry request content (and the key) into a log.
        raise LLMBadAnswer(f"{model}: {type(exc).__name__}: {exc}") from exc
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None) or "no explanation given"
        raise LLMBadAnswer(f"{model} declined the request: {detail}")
    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        raise LLMBadAnswer(f"{model} returned no structured output")
    usage = response.usage
    log.info("llm %s: in=%s out=%s", model, usage.input_tokens, usage.output_tokens)
    return parsed


_RETRY_AFTER = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.I)
_TOKEN_LIMIT = re.compile(r"Limit (\d+), Requested (\d+)", re.I)
_DAILY_LIMIT = re.compile(
    r"tokens per day \(TPD\): Limit (\d+), Used (\d+)", re.I)
# Gemini reports quota by id and does not give the numbers in a fixed place.
_GEMINI_DAILY = re.compile(r"PerDay", re.I)

#: A transient rate limit is worth waiting out; this many times.
RATE_LIMIT_RETRIES = 6
SERVER_RETRIES = 4
MAX_RATE_LIMIT_WAIT = 90.0


def _log_limits(headers) -> None:
    """What the provider says is left, so a slow run explains itself."""
    try:
        found = {k.lower(): v for k, v in headers.items()
                 if k.lower().startswith("x-ratelimit")}
    except Exception:
        return
    if found:
        log.debug("limits: %s", ", ".join(
            f"{k.removeprefix('x-ratelimit-')}={v}" for k, v in sorted(found.items())))


def _rate_limit_delay(body: str) -> float | None:
    """How long the provider asked us to wait, if it said."""
    match = _RETRY_AFTER.search(body)
    if match is None:
        return None
    minutes = float(match.group(1) or 0)
    return minutes * 60 + float(match.group(2))


def _token_limit(body: str) -> tuple[int, int] | None:
    match = _TOKEN_LIMIT.search(body)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _chat_completion(
    messages: list[dict[str, str]], *, model: str, max_tokens: int, settings: Settings,
    timeout: float = 180.0,
) -> str:
    """One OpenAI-compatible chat completion. Returns the reply text.

    A transient rate limit is waited out, because on a free tier that is the
    normal cost of a long job rather than an error. A request bigger than the
    whole per-minute allowance is not: that raises LLMRequestTooLarge so the
    caller sends less instead of sleeping forever.
    """
    import httpx

    url = f"{base_url(settings)}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,                      # extraction, not creativity
        "response_format": {"type": "json_object"},
    }
    if settings.llm_reasoning_effort and "gpt-oss" in model:
        # These models write their reasoning into the completion, which on a
        # per-minute token budget is the difference between one request a minute
        # and several. Measured 13 Nov 2026: the same tiny answer cost 71 output
        # tokens by default and 34 at "low". Extraction does not need deep
        # reasoning, and the schema is doing the shaping.
        body["reasoning_effort"] = settings.llm_reasoning_effort
    allowance = budget_mod.for_model(model)
    cost = budget_mod.estimate_cost(messages, max_tokens)
    for attempt in range(1, RATE_LIMIT_RETRIES + 2):
        allowance.wait_for(cost)
        allowance.spend(cost)
        try:
            response = httpx.post(
                url, json=body, timeout=timeout,
                headers={"Authorization": f"Bearer {_require_key(settings)}"},
            )
        except httpx.HTTPError as exc:
            raise LLMBadAnswer(f"{model}: could not reach {url}: {exc}") from exc
        if response.status_code == 401:
            raise LLMUnavailable(
                f"{settings.llm_key_name} was rejected by {url} (401). "
                f"Check the key in .env."
            )
        if response.status_code == 404:
            raise LLMBadAnswer(
                f"{model!r} is not available on this endpoint (404). Run "
                f"`python -m clipforge llm-check` to list the models the key can reach."
            )
        allowance.update(response.headers)
        if response.status_code in (413, 429):
            text = response.text or ""
            daily = _DAILY_LIMIT.search(text)
            if daily is None and settings.llm_provider == "gemini"                     and _GEMINI_DAILY.search(text):
                raise LLMQuotaExhausted(
                    f"{model}: Gemini's daily quota is spent. Nothing will run "
                    f"until it resets; a paid tier or a different LLM_PROVIDER "
                    f"removes this.",
                    retry_after=_rate_limit_delay(text),
                )
            if daily is not None:
                limit, used = int(daily.group(1)), int(daily.group(2))
                raise LLMQuotaExhausted(
                    f"{model}: the provider's daily token allowance is spent "
                    f"({used:,} of {limit:,}). Nothing will run until it resets; "
                    f"a paid tier or a different LLM_PROVIDER removes this.",
                    limit=limit, used=used,
                    retry_after=_rate_limit_delay(text),
                )
            pair = _token_limit(text)
            if pair is not None and pair[1] > pair[0]:
                limit, requested = pair
                raise LLMRequestTooLarge(
                    f"{model}: this one request is {requested} tokens against a "
                    f"{limit}-token-per-minute limit, so waiting cannot help; "
                    f"send less at a time",
                    limit=limit, requested=requested,
                )
            if attempt > RATE_LIMIT_RETRIES:
                raise LLMBadAnswer(
                    f"{model}: still rate limited after {RATE_LIMIT_RETRIES} waits"
                )
            delay = min(_rate_limit_delay(text) or (2.0 ** attempt), MAX_RATE_LIMIT_WAIT)
            log.info("llm %s: rate limited, waiting %.1fs (attempt %d)",
                     model, delay, attempt)
            time.sleep(delay)
            continue
        if response.status_code in (500, 502, 503, 504):
            # Seen from Gemini on 13 Nov 2026: "high demand ... try again later".
            if attempt > SERVER_RETRIES:
                raise LLMBadAnswer(
                    f"{model}: HTTP {response.status_code} on {SERVER_RETRIES + 1} tries: "
                    f"{response.text[:200]}")
            delay = min(2.0 ** attempt * 2, MAX_RATE_LIMIT_WAIT)
            log.info("llm %s: server busy (%d), waiting %.0fs (attempt %d)",
                     model, response.status_code, delay, attempt)
            time.sleep(delay)
            continue
        if response.status_code >= 400:
            raise LLMBadAnswer(f"{model}: HTTP {response.status_code}: {response.text[:400]}")
        break
    try:
        data = response.json()
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise LLMBadAnswer(f"{model}: unexpected response shape: {exc}") from exc
    _log_limits(response.headers)
    usage = data.get("usage") or {}
    log.info("llm %s: in=%s out=%s", model,
             usage.get("prompt_tokens"), usage.get("completion_tokens"))
    if choice.get("finish_reason") == "length":
        raise LLMBadAnswer(
            f"{model} hit the {max_tokens}-token ceiling before finishing the JSON; "
            f"the answer would be truncated"
        )
    return text


def _parse_openai_compatible(
    output_model: type[T], system: str, user: str, *, model: str, max_tokens: int,
    settings: Settings,
) -> T:
    """Schema in the prompt, validate, and retry once with the error attached."""
    schema = JSON_INSTRUCTION.format(schema=schema_text(output_model))
    messages = [
        {"role": "system", "content": f"{system}\n\n{schema}"},
        {"role": "user", "content": user},
    ]
    last: Exception | None = None
    for attempt in (1, 2):
        text = _chat_completion(
            messages, model=model, max_tokens=max_tokens, settings=settings
        )
        try:
            return output_model.model_validate(extract_json(text))
        except (ValidationError, LLMBadAnswer) as exc:
            last = exc
            log.warning("llm %s: answer did not validate on attempt %d: %s",
                        model, attempt, str(exc)[:400])
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    f"That did not validate against the schema:\n\n{str(exc)[:1500]}\n\n"
                    f"Answer again with the corrected JSON object only."},
            ]
    raise LLMBadAnswer(f"{model} could not produce a valid answer in 2 attempts: {last}")


def parse(
    output_model: type[T],
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int = 16000,
    settings: Settings | None = None,
) -> T:
    """Ask for one structured answer and return it validated.

    Raises LLMUnavailable if there is no key, LLMBadAnswer if the reply does not
    fit `output_model`.
    """
    settings = settings or get_settings()
    model = model or model_for(settings, "brief")
    if settings.llm_provider == "anthropic":
        return _parse_anthropic(
            output_model, system, user, model=model, max_tokens=max_tokens, settings=settings
        )
    return _parse_openai_compatible(
        output_model, system, user, model=model, max_tokens=max_tokens, settings=settings
    )


def available_models(settings: Settings | None = None) -> list[str]:
    """Model ids the key can reach. OpenAI-compatible providers only."""
    import httpx

    settings = settings or get_settings()
    if settings.llm_provider == "anthropic":
        return sorted(DEFAULT_MODELS["anthropic"].values())
    url = f"{base_url(settings)}/models"
    response = httpx.get(
        url, timeout=60.0, headers={"Authorization": f"Bearer {_require_key(settings)}"}
    )
    if response.status_code == 401:
        raise LLMUnavailable(f"{settings.llm_key_name} was rejected by {url} (401)")
    if response.status_code >= 400:
        raise LLMBadAnswer(f"HTTP {response.status_code} from {url}: {response.text[:300]}")
    data = response.json()
    ids = (m["id"].removeprefix("models/") for m in data.get("data", []) if "id" in m)
    return sorted(ids)


# A `Caller` is what the pipeline modules depend on, so tests can hand them a
# fake instead of a network call.
Caller = Callable[..., BaseModel]


def default_caller() -> Caller:
    return parse
