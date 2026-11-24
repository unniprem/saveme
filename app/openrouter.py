"""Asynchronous OpenRouter client for AI Highlights (runs in the API process, never the browser).

- `model()`: the configured model's entry in OpenRouter's public catalogue (GET /models,
  cached for an hour) as a ModelInfo: modalities, context, prices, and which of
  structured_outputs / response_format / reasoning it supports. `check_model` refuses a model
  SaveMe can't use (no text in/out, no fixed price, too little context).
- `complete()`: one chat completion. Only parameters the model lists are sent, because
  `provider.require_parameters` (always on, so a JSON schema is never silently ignored) would
  otherwise rule out every provider. A JSON schema goes as a strict `json_schema` response format
  when supported, else as `json_object`, else not at all (the prompt still asks for JSON, and the
  caller validates). Reasoning is requested at the lowest effort offered.
- `key_info()`: the key's usage and limit (GET /key), for "Test connection".

Errors become AppErrors: openrouter_auth (401), openrouter_credits (402), openrouter_refused
(other 4xx), openrouter_rate_limited (429 that persists), openrouter_unavailable (network,
timeouts, 5xx, garbled replies). Network errors, timeouts, 408, 429, 5xx and errors reported
inside a 200 are retried MAX_RETRIES times with exponential backoff (1 s, 2 s, 4 s, plus jitter),
or after Retry-After when OpenRouter sends one (up to MAX_RETRY_AFTER seconds; longer means
"rate limited" at once). Cancellation is never caught: an abandoned request may still be billed.
For the spending limit, complete() reports through `on_lost` every failed attempt that may have
been billed (one that reached the model: a timeout or broken connection after sending, or an error
reported inside a 200, with its usage if any), and asks `before_retry` before each retry.

The API key is only ever placed in the Authorization header. It is never logged, and messages
from OpenRouter are cleaned and scrubbed of it before they reach a user. Logs carry status
codes, model ids and token counts, never prompts or answers.
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import math
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import AiSettings
from app.errors import AppError
from app.transcript import clean_text

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_SECONDS = 1.0
JITTER_SECONDS = 0.5
MAX_RETRY_AFTER = 60.0
CATALOGUE_TTL_SECONDS = 3600.0
MAX_CATALOGUE_BYTES = 32 * 1024 * 1024
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


# --- models and prices ------------------------------------------------------------------------

def _price(value: object) -> float | None:
    """A per-token USD price from the catalogue's decimal string; None when it isn't a fixed,
    non-negative number (dynamic routers say "-1")."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


@dataclass(frozen=True)
class Pricing:
    """USD per token; `tiers` are (min_prompt_tokens, prompt, completion) for long prompts."""

    prompt: float
    completion: float
    tiers: tuple[tuple[int, float, float], ...] = ()

    def rates(self, prompt_tokens: int) -> tuple[float, float]:
        rates = (self.prompt, self.completion)
        for floor, prompt, completion in self.tiers:
            if prompt_tokens >= floor:
                rates = (prompt, completion)
        return rates

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        prompt, completion = self.rates(prompt_tokens)
        return prompt_tokens * prompt + completion_tokens * completion


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    context_length: int
    max_completion_tokens: int | None
    pricing: Pricing | None
    parameters: frozenset[str]
    input_modalities: frozenset[str]
    output_modalities: frozenset[str]
    reasoning_mandatory: bool
    reasoning_efforts: tuple[str, ...]

    @property
    def structured_outputs(self) -> bool:
        return "structured_outputs" in self.parameters

    @property
    def json_mode(self) -> bool:
        return "response_format" in self.parameters

    @property
    def reasoning(self) -> bool:
        return "reasoning" in self.parameters

    @property
    def lowest_effort(self) -> str | None:
        return next((effort for effort in _EFFORTS if effort in self.reasoning_efforts), None)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _strings(value: object) -> frozenset[str]:
    return frozenset(v for v in value if isinstance(v, str)) if isinstance(value, list) else frozenset()


def model_info(entry: dict) -> ModelInfo:
    """A catalogue entry (GET /models) as a ModelInfo; missing or odd fields read as absent."""
    pricing_data = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
    prompt, completion = _price(pricing_data.get("prompt")), _price(pricing_data.get("completion"))
    pricing = None
    if prompt is not None and completion is not None:
        tiers = []
        for tier in pricing_data.get("overrides") or []:
            if not isinstance(tier, dict):
                continue
            floor = _int(tier.get("min_prompt_tokens"))
            t_prompt, t_completion = _price(tier.get("prompt")), _price(tier.get("completion"))
            if floor and t_prompt is not None and t_completion is not None:
                tiers.append((floor, t_prompt, t_completion))
        pricing = Pricing(prompt, completion, tuple(sorted(tiers)))
    top = entry.get("top_provider") if isinstance(entry.get("top_provider"), dict) else {}
    arch = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
    efforts = reasoning.get("supported_efforts")
    return ModelInfo(
        id=str(entry.get("id", "")),
        name=clean_text(str(entry.get("name") or entry.get("id", "")), 200),
        context_length=_int(entry.get("context_length")) or _int(top.get("context_length")) or 0,
        max_completion_tokens=_int(top.get("max_completion_tokens")),
        pricing=pricing,
        parameters=_strings(entry.get("supported_parameters")),
        input_modalities=_strings(arch.get("input_modalities")),
        output_modalities=_strings(arch.get("output_modalities")),
        reasoning_mandatory=reasoning.get("mandatory") is True,
        reasoning_efforts=tuple(e for e in efforts if isinstance(e, str)) if isinstance(efforts, list) else (),
    )


def check_model(info: ModelInfo, *, needed_tokens: int) -> None:
    """Raise model_unsupported, naming the reason, unless SaveMe can use the model for a call
    of `needed_tokens` (prompt plus answer budget)."""
    def unsupported(reason: str) -> AppError:
        return AppError("model_unsupported", f"The model {info.id} can't be used for AI Highlights: "
                        f"{reason} Choose another OPENROUTER_MODEL.")

    if "text" not in info.input_modalities or "text" not in info.output_modalities:
        raise unsupported("it doesn't take and return text.")
    if info.pricing is None:
        raise unsupported("its price isn't fixed, so SaveMe can't keep to the spending limit.")
    if info.parameters and not {"max_tokens", "max_completion_tokens"} & info.parameters:
        raise unsupported("it can't be given a maximum answer length, so SaveMe can't keep to the "
                          "spending limit.")
    if info.context_length < needed_tokens:
        raise unsupported(f"its context window ({info.context_length:,} tokens) is too small.")


# --- answers ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cost: float | None  # USD (OpenRouter credits) as reported; None when not reported


@dataclass(frozen=True)
class Completion:
    content: str
    usage: Usage
    finish_reason: str | None


class UsageMeter:
    """Running totals for one analysis. The cost is OpenRouter's own figure for every call that
    reported one, plus tokens × catalogue price for any call that didn't; `cost_reported` says
    whether every call reported it (then the total is the actual cost)."""

    def __init__(self, pricing: Pricing | None):
        self._pricing = pricing
        self.calls = self.prompt_tokens = self.completion_tokens = 0
        self._reported = 0.0
        self._estimated = 0.0
        self._unreported = 0

    def add(self, usage: Usage, *, bound: float | None = None) -> None:
        """Count one call. A call that reported nothing about its usage counts as `bound`, the
        most it could have cost."""
        if usage.cost is None and not usage.prompt_tokens and not usage.completion_tokens and bound is not None:
            self.calls += 1
            self._unreported += 1
            self._estimated += bound
            return
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        if usage.cost is not None:
            self._reported += usage.cost
        else:
            self._unreported += 1
            if self._pricing is not None:
                self._estimated += self._pricing.cost(usage.prompt_tokens, usage.completion_tokens)

    def add_lost(self, usage: Usage | None, *, bound: float) -> None:
        """Count a failed attempt that may have been billed, at its usage if known, else `bound`."""
        self.add(usage if usage is not None else Usage(0, 0, None), bound=bound)

    @property
    def spent(self) -> float:
        return self._reported + self._estimated

    def view(self) -> dict:
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": round(self.spent, 6),
                "cost_reported": self.calls > 0 and self._unreported == 0}


def parse_json_object(text: str) -> dict:
    """The JSON object in a model's answer, which may be fenced (```json) or wrapped in prose."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    candidates = [stripped]
    first, last = stripped.find("{"), stripped.rfind("}")
    if 0 <= first < last:
        candidates.append(stripped[first:last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise AppError("ai_invalid_response", "The AI model's answer wasn't valid JSON.")


def _usage(data: dict) -> Usage:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def count(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    cost = usage.get("cost")
    valid = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
    return Usage(count("prompt_tokens"), count("completion_tokens"), float(cost) if valid else None)


usage_of = _usage  # the usage of any reply body, for callers counting failed attempts


def _content(message: object) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in content
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


# --- the client -------------------------------------------------------------------------------

class _Failure(Exception):
    """A failed attempt. `billed`: it reached the model, so it may cost money; `data`: its body."""

    def __init__(self, error: AppError, *, retry: bool, after: float | None = None,
                 billed: bool = False, data: dict | None = None):
        self.error, self.retry, self.after = error, retry, after
        self.billed, self.data = billed, data


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class OpenRouterClient:
    def __init__(self, settings: AiSettings, *, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], object] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random,
                 clock: Callable[[], float] = time.monotonic, max_retries: int = MAX_RETRIES):
        self._settings = settings
        self._sleep, self._jitter, self._clock = sleep, jitter, clock
        self._max_retries = max_retries
        self._catalogue: dict[str, dict] | None = None
        self._catalogue_at = 0.0
        self._catalogue_lock = asyncio.Lock()
        headers = {"Accept": "application/json"}
        if settings.configured:
            headers["Authorization"] = f"Bearer {settings.openrouter_api_key}"
        # trust_env=False: no proxy or netrc from the environment; redirects are never followed.
        self._http = httpx.AsyncClient(base_url=settings.openrouter_base_url + "/", headers=headers,
                                       transport=transport, timeout=_TIMEOUT, trust_env=False,
                                       follow_redirects=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- requests ---

    def _scrub(self, text: str) -> str:
        """An OpenRouter message fit to show: the key removed first (cutting first could leave
        part of it), then cleaned and capped."""
        key = self._settings.openrouter_api_key
        if key:
            text = text.replace(key, "[key]")
        return clean_text(text, 300)

    def _error_body_message(self, data: object) -> str:
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        return self._scrub(message) if isinstance(message, str) else ""

    def _failure(self, status: int, data: object, after: float | None) -> _Failure:
        if status == 401:
            return _Failure(AppError("openrouter_auth"), retry=False)
        if status == 402:
            return _Failure(AppError("openrouter_credits"), retry=False)
        if status == 429:
            return _Failure(AppError("openrouter_rate_limited"), retry=True, after=after)
        if status == 408 or status >= 500:
            return _Failure(AppError("openrouter_unavailable"), retry=True, after=after)
        detail = self._error_body_message(data) or "no reason given"
        return _Failure(AppError("openrouter_refused", f"OpenRouter refused the request ({status}): {detail}"),
                        retry=False)

    def _classify(self, response: httpx.Response) -> dict:
        after = _retry_after(response)
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200:
            raise self._failure(response.status_code, data, after)
        if not isinstance(data, dict):
            raise _Failure(AppError("openrouter_unavailable"), retry=True)
        error = data.get("error")
        if isinstance(error, dict):  # an error reported inside a 200: the model may have run
            code = error.get("code")
            status = code if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600 else 502
            failure = self._failure(status, data, after)
            failure.billed, failure.data = True, data
            raise failure
        return data

    async def _send(self, method: str, path: str, *, body: dict | None = None,
                    check: Callable[[dict], None] | None = None,
                    before_retry: Callable[[], bool] | None = None,
                    on_lost: Callable[[dict | None], None] | None = None) -> dict:
        if not self._settings.configured:
            raise AppError("ai_not_configured")
        attempt = 0
        while True:
            try:
                try:
                    response = await self._http.request(method, path, json=body)
                except httpx.TimeoutException as exc:
                    sent = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
                    raise _Failure(AppError("openrouter_unavailable", "OpenRouter took too long to answer. "
                                            "Try again later."), retry=True, billed=sent) from None
                except httpx.TransportError as exc:
                    logger.warning("openrouter %s %s: %s", method, path, type(exc).__name__)
                    sent = not isinstance(exc, httpx.ConnectError)
                    raise _Failure(AppError("openrouter_unavailable"), retry=True, billed=sent) from None
                if len(response.content) > MAX_CATALOGUE_BYTES:
                    raise _Failure(AppError("openrouter_unavailable"), retry=False)
                data = self._classify(response)
                if check is not None:
                    check(data)
                return data
            except _Failure as failure:
                logger.warning("openrouter %s %s failed: %s (attempt %d)", method, path,
                               failure.error.code, attempt + 1)
                if failure.billed and on_lost is not None:
                    on_lost(failure.data)
                if not failure.retry or attempt >= self._max_retries:
                    raise failure.error from None
                if failure.after is not None and failure.after > MAX_RETRY_AFTER:
                    raise failure.error from None
                if before_retry is not None and not before_retry():
                    logger.info("openrouter %s %s: not retried (spending limit)", method, path)
                    raise failure.error from None
                delay = (failure.after if failure.after is not None
                         else BACKOFF_SECONDS * 2 ** attempt + self._jitter() * JITTER_SECONDS)
                attempt += 1
                await self._sleep(delay)

    # --- public API ---

    async def model(self, model_id: str | None = None) -> ModelInfo:
        """The catalogue entry for `model_id` (default: OPENROUTER_MODEL)."""
        wanted = model_id or self._settings.openrouter_model
        async with self._catalogue_lock:
            if self._catalogue is None or self._clock() - self._catalogue_at >= CATALOGUE_TTL_SECONDS:
                data = await self._send("GET", "models")
                entries = data.get("data") if isinstance(data.get("data"), list) else []
                self._catalogue = {e["id"]: e for e in entries
                                   if isinstance(e, dict) and isinstance(e.get("id"), str)}
                self._catalogue_at = self._clock()
            entry = self._catalogue.get(wanted)
        if entry is None:
            raise AppError("model_unsupported", f"OpenRouter has no model called {wanted}. "
                           "Check OPENROUTER_MODEL against https://openrouter.ai/models.")
        return model_info(entry)

    async def key_info(self) -> dict:
        """The key's spend so far and its limit, in USD (None when OpenRouter doesn't say)."""
        data = await self._send("GET", "key")
        info = data.get("data") if isinstance(data.get("data"), dict) else {}

        def usd(name: str) -> float | None:
            value = info.get(name)
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            return float(value) if ok else None

        return {"usage_usd": usd("usage"), "limit_usd": usd("limit"),
                "limit_remaining_usd": usd("limit_remaining"), "is_free_tier": info.get("is_free_tier") is True}

    async def complete(self, messages: Sequence[dict], *, model: ModelInfo, schema: dict | None,
                       schema_name: str, max_tokens: int,
                       before_retry: Callable[[], bool] | None = None,
                       on_lost: Callable[[dict | None], None] | None = None) -> Completion:
        body: dict = {"model": model.id, "messages": list(messages)}
        if "max_tokens" in model.parameters or not model.parameters:
            body["max_tokens"] = max_tokens
        elif "max_completion_tokens" in model.parameters:
            body["max_completion_tokens"] = max_tokens
        if schema is not None and model.structured_outputs:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        elif schema is not None and model.json_mode:
            body["response_format"] = {"type": "json_object"}
        if model.reasoning and model.lowest_effort is not None:
            body["reasoning"] = {"effort": model.lowest_effort}
        body["provider"] = {"require_parameters": True,
                            "data_collection": self._settings.openrouter_data_collection}

        def check(data: dict) -> None:
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise _Failure(AppError("openrouter_unavailable"), retry=True)
            if choices[0].get("finish_reason") == "error" or isinstance(choices[0].get("error"), dict):
                raise _Failure(AppError("openrouter_unavailable"), retry=True, billed=True, data=data)

        data = await self._send("POST", "chat/completions", body=body, check=check,
                                before_retry=before_retry, on_lost=on_lost)
        choice = data["choices"][0]
        usage = _usage(data)
        finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
        logger.info("openrouter completion: model %s, %d prompt + %d completion tokens, finish %s",
                    model.id, usage.prompt_tokens, usage.completion_tokens, finish)
        return Completion(content=_content(choice.get("message")), usage=usage, finish_reason=finish)
