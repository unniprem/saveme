"""Opt-in end-to-end check against the real OpenRouter API. It spends a fraction of a cent.

Run with `OPENROUTER_API_KEY=sk-or-... .venv/bin/pytest -m network tests/network/test_openrouter_live.py -v`
(OPENROUTER_MODEL picks another model). Skipped without a key; never part of the default run.
"""
import os

import pytest

from app.config import load_ai_settings
from app.highlights import Options, analyze, estimate_from_transcript
from app.openrouter import OpenRouterClient, UsageMeter, check_model
from app.transcript import Segment, Transcript

pytestmark = [pytest.mark.network,
              pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="needs OPENROUTER_API_KEY")]

LINES = [
    "Welcome back to the show, today we're talking about two things.",
    "First, artificial intelligence and how it's changing software development.",
    "I use an assistant every day now to write tests and to refactor old code.",
    "It doesn't replace thinking, but it removes a lot of the boring work.",
    "The best advice I can give beginners is to read the code it writes, line by line.",
    "Never ship something you don't understand, even if a tool wrote it for you.",
    "Okay, second topic: electric vehicles.",
    "Batteries got about forty percent cheaper over the last five years.",
    "That means a family car can now cost the same as a petrol one.",
    "And here's a funny story: my neighbour plugged his car into the garden light socket.",
    "It took three days to charge, and the garden was dark all week.",
    "Anyway, that's all for today, thanks for watching.",
]


def _transcript() -> Transcript:
    segments = [Segment(start=i * 8.0, end=i * 8.0 + 7.5, text=text, language="en")
                for i, text in enumerate(LINES)]
    return Transcript(origin="subtitles", model=None, language="en", language_probability=None,
                      requested_language="en", duration=len(LINES) * 8.0, segments=segments)


async def test_live_analysis_returns_valid_highlights_and_reports_cost():
    settings = load_ai_settings(os.environ)
    client = OpenRouterClient(settings)
    try:
        key = await client.key_info()
        assert key["usage_usd"] is None or key["usage_usd"] >= 0
        model = await client.model()
        check_model(model, needed_tokens=4000)
        transcript = _transcript()
        options = Options(instruction="Find the most useful advice and the funniest moment.",
                          min_seconds=10, max_seconds=40, count=2, min_score=0)
        estimate = estimate_from_transcript(transcript, options, model)
        assert estimate.max_usd < 0.01
        meter = UsageMeter(model.pricing)
        result = await analyze(client, model, transcript, options, limit_usd=0.02, meter=meter,
                               on_progress=lambda done, total: None)
    finally:
        await client.aclose()
    assert 1 <= len(result.candidates) <= 2
    for candidate in result.candidates:
        assert 0 <= candidate.start < candidate.end <= transcript.duration
        assert 10 <= candidate.end - candidate.start <= 40
        assert candidate.title and 0 <= candidate.score <= 100
    usage = meter.view()
    assert usage["calls"] >= 1 and usage["prompt_tokens"] > 0 and usage["cost_usd"] <= 0.02
    print(f"\nlive: {usage}, estimate {estimate}")
