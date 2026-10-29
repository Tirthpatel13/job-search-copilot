from types import SimpleNamespace

import pytest
from conftest import FakeLLM, make_job

from jobcopilot.config import Settings
from jobcopilot.llm import ClaudeLLM, LLMError
from jobcopilot.scoring import JobScore, job_prompt, score_job, score_pending


def test_score_is_clamped_and_cleaned(profile):
    llm = FakeLLM(
        {
            JobScore: JobScore(
                score=140,
                reason="  Strong   Python match. ",
                matched=["Python"],
                gaps=["Kubernetes", " "],
            )
        }
    )
    result = score_job(llm, profile, make_job())
    assert result.score == 100
    assert result.reason == "Strong Python match."
    assert result.gaps == ["Kubernetes"]
    call = llm.calls[0]
    assert "Alex Rivera" in call["profile"]  # profile is grounded context
    assert "Backend Engineer" in call["prompt"]
    assert "0-100" in call["system"]


def test_job_prompt_truncates_long_descriptions():
    prompt = job_prompt(make_job(description="x" * 50_000))
    assert len(prompt) < 13_000


def test_score_pending_saves_results_and_isolates_failures(db, profile):
    db.upsert_jobs([make_job(id="a"), make_job(id="b", title="Data Engineer")])

    def respond(prompt: str):
        if "Data Engineer" in prompt:
            raise LLMError("refused")
        return JobScore(score=72, reason="Good fit.", matched=["Python"], gaps=["Kubernetes"])

    class Flaky(FakeLLM):
        def generate(self, **kwargs):
            return respond(kwargs["prompt"])

    assert score_pending(db, Flaky({}), profile, limit=10) == {
        "scored": 1,
        "score_failures": 1,
        "score_error": "refused",
    }
    job = db.get_job("a")
    assert (job.score, job.gaps) == (72, ["Kubernetes"])
    assert db.get_job("b").score is None


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(response):
    messages = _FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_claude_llm_request_shape():
    parsed = JobScore(score=50, reason="ok", matched=[], gaps=[])
    client, messages = _client(SimpleNamespace(stop_reason="end_turn", parsed_output=parsed))
    llm = ClaudeLLM(Settings(anthropic_api_key="k", claude_model="claude-test"), client=client)
    assert llm.generate(system="S", profile="P", prompt="Q", schema=JobScore) == parsed
    kw = messages.kwargs
    assert kw["model"] == "claude-test"
    assert kw["output_format"] is JobScore
    assert kw["fallbacks"] == "default"
    assert kw["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "Never invent" in kw["system"][0]["text"]


def test_claude_llm_without_fallbacks():
    parsed = JobScore(score=50, reason="ok", matched=[], gaps=[])
    client, messages = _client(SimpleNamespace(stop_reason="end_turn", parsed_output=parsed))
    ClaudeLLM(Settings(anthropic_api_key="k", claude_fallbacks=False), client=client).generate(
        system="S", profile="P", prompt="Q", schema=JobScore
    )
    assert "fallbacks" not in messages.kwargs


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_llm_raises_on_unusable_stop(stop_reason):
    client, _ = _client(SimpleNamespace(stop_reason=stop_reason, parsed_output=None))
    llm = ClaudeLLM(Settings(anthropic_api_key="k"), client=client)
    with pytest.raises(LLMError):
        llm.generate(system="S", profile="P", prompt="Q", schema=JobScore)


def test_claude_llm_requires_key():
    with pytest.raises(LLMError):
        ClaudeLLM(Settings(anthropic_api_key=""))


def test_score_pending_stops_on_account_error(db, profile):
    db.upsert_jobs([make_job(id="a"), make_job(id="b", title="Data Engineer")])
    error = LLMError("Claude API error 400: Your credit balance is too low", fatal=True)
    llm = FakeLLM({JobScore: error})
    summary = score_pending(db, llm, profile, limit=10)
    assert summary == {
        "scored": 0,
        "score_failures": 1,
        "score_error": str(error),
        "score_stopped": True,
    }
    assert len(llm.calls) == 1


class _RaisingMessages:
    def __init__(self, exc):
        self.exc = exc

    def parse(self, **kwargs):
        raise self.exc


def _raising_llm(exc):
    client = SimpleNamespace(beta=SimpleNamespace(messages=_RaisingMessages(exc)))
    return ClaudeLLM(Settings(anthropic_api_key="k"), client=client)


def _validation_error():
    from pydantic import ValidationError

    try:
        JobScore.model_validate({"score": "lots"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_claude_llm_maps_generic_api_and_validation_errors():
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in (anthropic.APIError("weird", request, body=None), _validation_error()):
        with pytest.raises(LLMError) as info:
            _raising_llm(exc).generate(system="S", profile="P", prompt="Q", schema=JobScore)
        assert not info.value.fatal


def test_claude_llm_rate_limit_stops_batch():
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request)
    exc = anthropic.RateLimitError("slow down", response=response, body=None)
    with pytest.raises(LLMError) as info:
        _raising_llm(exc).generate(system="S", profile="P", prompt="Q", schema=JobScore)
    assert info.value.stop_batch and not info.value.fatal


def test_score_pending_stops_on_rate_limit(db, profile):
    db.upsert_jobs([make_job(id="a"), make_job(id="b", title="Data Engineer")])
    llm = FakeLLM({JobScore: LLMError("Claude rate limit reached", stop_batch=True)})
    summary = score_pending(db, llm, profile, limit=10)
    assert summary == {
        "scored": 0,
        "score_failures": 1,
        "score_error": "Claude rate limit reached",
        "score_stopped": True,
    }
    assert len(llm.calls) == 1


def test_score_pending_isolates_unexpected_errors(db, profile):
    db.upsert_jobs([make_job(id="a"), make_job(id="b", title="Data Engineer")])

    class Weird(FakeLLM):
        def generate(self, **kwargs):
            if "Data Engineer" in kwargs["prompt"]:
                raise KeyError("boom")
            return JobScore(score=60, reason="ok", matched=[], gaps=[])

    summary = score_pending(db, Weird({}), profile, limit=10)
    assert summary["scored"] == 1 and summary["score_failures"] == 1
    assert summary["score_error"].startswith("KeyError")
    assert db.get_job("a").score == 60
