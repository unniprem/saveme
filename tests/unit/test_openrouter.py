"""The OpenRouter client against a mocked transport: request shape, capability check, pricing,
retries, errors, usage and cost. No test reaches the network."""
import asyncio
import json
import logging

import httpx
import pytest

from app.config import load_ai_settings
from app.errors import AppError
from app.openrouter import OpenRouterClient, Pricing, check_model, model_info, parse_json_object
from tests.support.openrouter import (HAIKU, JSON_MODE, PLAIN, ROUTER, IMAGE_OUT, KEY, MockOpenRouter,
                                      completion)

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["clips"],
          "properties": {"clips": {"type": "array", "items": {"type": "object"}}}}
MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "transcript"}]


def make(server: MockOpenRouter, **env) -> tuple[OpenRouterClient, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    settings = load_ai_settings({"OPENROUTER_API_KEY": KEY, **env})
    client = OpenRouterClient(settings, transport=server.transport(), sleep=sleep, jitter=lambda: 0.0)
    return client, sleeps


# --- model info and pricing --------------------------------------------------------------

def test_model_info_reads_capabilities_and_prices():
    info = model_info(HAIKU)
    assert info.id == "anthropic/claude-haiku-5.5" and info.context_length == 1_000_000
    assert info.structured_outputs and info.json_mode and info.reasoning
    assert info.lowest_effort == "none" and not info.reasoning_mandatory
    assert info.pricing.prompt == pytest.approx(1e-7) and info.pricing.completion == pytest.approx(5e-7)
    assert "temperature" not in info.parameters


def test_pricing_uses_the_tier_for_long_prompts():
    pricing = model_info(HAIKU).pricing
    assert pricing.cost(1_000, 100) == pytest.approx(1_000 * 1e-7 + 100 * 5e-7)
    assert pricing.cost(200_000, 100) == pytest.approx(200_000 * 5e-7 + 100 * 25e-7)
    assert Pricing(prompt=0.0, completion=0.0).cost(10, 10) == 0.0


@pytest.mark.parametrize("entry,reason", [
    (ROUTER, "price"), (IMAGE_OUT, "text"),
    ({**HAIKU, "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}}, "text"),
    ({**HAIKU, "pricing": {"prompt": "abc", "completion": "1"}}, "price"),
])
def test_check_model_refuses_models_saveme_cant_use(entry, reason):
    with pytest.raises(AppError) as exc:
        check_model(model_info(entry), needed_tokens=1000)
    assert exc.value.code == "model_unsupported" and reason in exc.value.message
    assert entry["id"] in exc.value.message


def test_check_model_needs_room_for_the_prompt():
    small = model_info({**PLAIN, "context_length": 4096})
    check_model(small, needed_tokens=4000)
    with pytest.raises(AppError) as exc:
        check_model(small, needed_tokens=5000)
    assert exc.value.code == "model_unsupported" and "context" in exc.value.message


# --- catalogue ---------------------------------------------------------------------------

async def test_model_comes_from_the_cached_catalogue():
    server = MockOpenRouter()
    clock = [0.0]
    client, _ = make(server)
    client._clock = lambda: clock[0]
    info = await client.model()
    assert info.id == "anthropic/claude-haiku-5.5"
    await client.model()
    assert server.paths() == ["/api/v1/models"]
    clock[0] += 3601
    await client.model()
    assert server.paths() == ["/api/v1/models", "/api/v1/models"]
    await client.aclose()


async def test_an_unknown_model_is_unsupported():
    client, _ = make(MockOpenRouter(), OPENROUTER_MODEL="nobody/nothing")
    with pytest.raises(AppError) as exc:
        await client.model()
    assert exc.value.code == "model_unsupported" and "nobody/nothing" in exc.value.message


# --- chat completions --------------------------------------------------------------------

async def test_completion_request_shape():
    server = MockOpenRouter()
    server.chat.append(completion({"clips": []}))
    client, _ = make(server)
    result = await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA,
                                   schema_name="highlights", max_tokens=900)
    request = server.requests[-1]
    assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert "http-referer" not in request.headers and "x-title" not in request.headers
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": "anthropic/claude-haiku-5.5", "messages": MESSAGES, "max_tokens": 900,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "highlights", "strict": True, "schema": SCHEMA}},
        "reasoning": {"effort": "none"},
        "provider": {"require_parameters": True, "data_collection": "deny"},
    }
    assert result.content == json.dumps({"clips": []})
    assert result.usage.prompt_tokens == 1200 and result.usage.completion_tokens == 80
    assert result.usage.cost == pytest.approx(0.00016)


async def test_completion_falls_back_to_json_mode_then_to_plain_text():
    server = MockOpenRouter()
    server.chat.extend([completion({"clips": []}), completion({"clips": []})])
    client, _ = make(server, OPENROUTER_DATA_COLLECTION="allow")
    await client.complete(MESSAGES, model=model_info(JSON_MODE), schema=SCHEMA, schema_name="h", max_tokens=50)
    body = json.loads(server.requests[-1].content)
    assert body["response_format"] == {"type": "json_object"} and "reasoning" not in body
    assert body["provider"] == {"require_parameters": True, "data_collection": "allow"}
    await client.complete(MESSAGES, model=model_info(PLAIN), schema=SCHEMA, schema_name="h", max_tokens=50)
    body = json.loads(server.requests[-1].content)
    assert "response_format" not in body and "reasoning" not in body and body["max_tokens"] == 50


async def test_completion_reads_content_parts_and_missing_cost():
    server = MockOpenRouter()
    reply = completion({"clips": []}, cost=None)
    reply["choices"][0]["message"]["content"] = [{"type": "text", "text": '{"clips":'},
                                                 {"type": "text", "text": " []}"}]
    server.chat.append(reply)
    client, _ = make(server)
    result = await client.complete(MESSAGES, model=model_info(HAIKU), schema=None, schema_name="h", max_tokens=50)
    assert result.content == '{"clips": []}' and result.usage.cost is None


async def test_rate_limit_is_retried_after_retry_after():
    server = MockOpenRouter()
    server.chat.extend([httpx.Response(429, headers={"Retry-After": "2"},
                                       json={"error": {"code": 429, "message": "slow down"}}),
                        completion({"clips": []})])
    client, sleeps = make(server)
    await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert sleeps == [2.0] and len(server.requests) == 2


async def test_server_errors_back_off_exponentially_then_give_up():
    server = MockOpenRouter()
    server.chat.extend([httpx.Response(503, json={"error": {"code": 503, "message": "x"}})] * 4)
    client, sleeps = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert exc.value.code == "openrouter_unavailable"
    assert sleeps == [1.0, 2.0, 4.0] and len(server.requests) == 4


async def test_rate_limit_that_never_clears_is_reported_as_such():
    server = MockOpenRouter()
    server.chat.extend([httpx.Response(429, json={"error": {"code": 429, "message": "x"}})] * 4)
    client, _ = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert exc.value.code == "openrouter_rate_limited"


async def test_a_long_retry_after_is_not_waited_for():
    server = MockOpenRouter()
    server.chat.append(httpx.Response(429, headers={"Retry-After": "600"}, json={"error": {"code": 429}}))
    client, sleeps = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert exc.value.code == "openrouter_rate_limited" and sleeps == []


@pytest.mark.parametrize("status,code", [(401, "openrouter_auth"), (402, "openrouter_credits"),
                                         (400, "openrouter_refused"), (403, "openrouter_refused"),
                                         (404, "openrouter_refused")])
async def test_client_errors_are_not_retried(status, code):
    server = MockOpenRouter()
    server.chat.append(httpx.Response(status, json={"error": {"code": status,
                                                              "message": "No endpoints\x00 found\n"}}))
    client, sleeps = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert exc.value.code == code and sleeps == [] and len(server.requests) == 1
    if code == "openrouter_refused":
        assert "No endpoints found" in exc.value.message and "\x00" not in exc.value.message


async def test_errors_inside_a_200_are_retried():
    server = MockOpenRouter()
    failed = completion({"clips": []})
    failed["choices"][0]["finish_reason"] = "error"
    server.chat.extend([httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}}),
                        failed, completion({"clips": []})])
    client, sleeps = make(server)
    await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert len(server.requests) == 3 and sleeps == [1.0, 2.0]


async def test_network_errors_and_timeouts_are_retried():
    server = MockOpenRouter()
    server.chat.extend([httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                        httpx.Response(200, content=b"<html>not json"), completion({"clips": []})])
    client, sleeps = make(server)
    await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert len(sleeps) == 3


async def test_a_cancelled_request_stays_cancelled():
    server = MockOpenRouter()
    server.delay = 30
    client, _ = make(server)
    task = asyncio.create_task(client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA,
                                               schema_name="h", max_tokens=50))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_missing_key_means_not_configured():
    client = OpenRouterClient(load_ai_settings({}), transport=MockOpenRouter().transport())
    with pytest.raises(AppError) as exc:
        await client.model()
    assert exc.value.code == "ai_not_configured"


async def test_the_key_never_reaches_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    server = MockOpenRouter()
    server.chat.extend([httpx.Response(401, json={"error": {"code": 401, "message": f"bad key {KEY}"}})])
    client, _ = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert KEY not in caplog.text and KEY not in exc.value.message


# --- key check ---------------------------------------------------------------------------

async def test_key_info():
    server = MockOpenRouter()
    client, _ = make(server)
    info = await client.key_info()
    assert info == {"usage_usd": 1.25, "limit_usd": 10.0, "limit_remaining_usd": 8.75, "is_free_tier": False}
    assert server.requests[-1].url.path == "/api/v1/key"
    server.key_status = 401
    with pytest.raises(AppError) as exc:
        await client.key_info()
    assert exc.value.code == "openrouter_auth"


# --- JSON in answers ---------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"clips": []}', '```json\n{"clips": []}\n```', 'Here you go: {"clips": []} Enjoy!',
])
def test_parse_json_object_finds_the_object(text):
    assert parse_json_object(text) == {"clips": []}


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"clips": [}', "{" * 10])
def test_parse_json_object_refuses_anything_else(text):
    with pytest.raises(AppError) as exc:
        parse_json_object(text)
    assert exc.value.code == "ai_invalid_response"


# --- usage accounting --------------------------------------------------------------------

def test_usage_meter_sums_reported_costs():
    from app.openrouter import Usage, UsageMeter

    meter = UsageMeter(model_info(HAIKU).pricing)
    assert meter.view() == {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                            "cost_usd": 0.0, "cost_reported": False}
    meter.add(Usage(1000, 100, 0.0002))
    meter.add(Usage(2000, 50, 0.0003))
    view = meter.view()
    assert view["calls"] == 2 and view["prompt_tokens"] == 3000 and view["completion_tokens"] == 150
    assert view["cost_usd"] == pytest.approx(0.0005) and view["cost_reported"] is True


def test_usage_meter_prices_calls_without_a_reported_cost():
    from app.openrouter import Usage, UsageMeter

    meter = UsageMeter(model_info(HAIKU).pricing)
    meter.add(Usage(1000, 100, 0.0002))
    meter.add(Usage(1000, 100, None))
    view = meter.view()
    assert view["cost_usd"] == pytest.approx(0.0002 + 1000 * 1e-7 + 100 * 5e-7)
    assert view["cost_reported"] is False
    assert meter.spent == pytest.approx(view["cost_usd"])


async def test_a_key_near_the_message_cut_is_scrubbed_whole():
    server = MockOpenRouter()
    message = "x" * 290 + KEY + " tail"
    server.chat.append(httpx.Response(400, json={"error": {"code": 400, "message": message}}))
    client, _ = make(server)
    with pytest.raises(AppError) as exc:
        await client.complete(MESSAGES, model=model_info(HAIKU), schema=SCHEMA, schema_name="h", max_tokens=50)
    assert KEY[:8] not in exc.value.message and "[key]" in exc.value.message
