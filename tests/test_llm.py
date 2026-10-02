from types import SimpleNamespace

import pytest

from app.llm import LLMClient, LLMError, cache_key


class FakeMessages:
    def __init__(self, text="hello", stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []

    def create(self, **params):
        self.calls.append(params)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=self.text)])


def _live(tmp_path, **kw):
    fake = FakeMessages(**kw)
    return LLMClient("live", cache_dir=tmp_path, sdk_client=SimpleNamespace(messages=fake)), fake


def test_live_caches_then_replays(tmp_path):
    llm, fake = _live(tmp_path)
    assert llm.complete("t", "claude-opus-5-5", "sys", "p") == "hello"
    assert llm.complete("t", "claude-opus-5-5", "sys", "p") == "hello"
    assert len(fake.calls) == 1, "second call must come from the disk cache"

    offline = LLMClient("offline", cache_dir=tmp_path)
    assert offline.complete("t", "claude-opus-5-5", "sys", "p") == "hello"


def test_offline_miss_raises(tmp_path):
    with pytest.raises(LLMError):
        LLMClient("offline", cache_dir=tmp_path).complete("t", "m", "s", "p")


def test_cache_key_depends_on_model_system_and_prompt():
    base = cache_key("m", "s", "p")
    assert len({base, cache_key("m2", "s", "p"), cache_key("m", "s2", "p"), cache_key("m", "s", "p2")}) == 4


def test_temperature_only_for_models_that_accept_it(tmp_path):
    llm, fake = _live(tmp_path)
    llm.complete("t", "claude-haiku-4-5-20251001", "s", "p")
    llm.complete("t", "claude-opus-5-5", "s", "p")
    haiku, opus = fake.calls
    assert haiku["temperature"] == 0
    assert "temperature" not in opus and opus["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("kw", [{"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}, {"text": "  "}])
def test_bad_responses_raise_and_are_not_cached(tmp_path, kw):
    llm, _ = _live(tmp_path, **kw)
    with pytest.raises(LLMError):
        llm.complete("t", "claude-opus-5-5", "s", "p")
    assert not list(tmp_path.iterdir())


def test_stub_mode():
    llm = LLMClient("stub", stub=lambda task, s, p: f"{task}:{p}")
    assert llm.complete("score_fit", "m", "s", "x") == "score_fit:x"
    with pytest.raises(LLMError):
        LLMClient("stub").complete("t", "m", "s", "p")
