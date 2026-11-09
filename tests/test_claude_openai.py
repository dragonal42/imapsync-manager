import json
from unittest.mock import Mock

import pytest
import requests

import ai_preprocessing as ai


def reply(engine, answer):
    usage = {"input_tokens": 100, "output_tokens": 20}
    if engine == "Claude":
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(answer)}], "usage": usage}
    return {"status": "completed", "output": [{"type": "message", "status": "completed",
             "content": [{"type": "output_text", "text": json.dumps(answer)}]}], "usage": usage}


@pytest.mark.parametrize("engine,endpoint", [
    ("Claude", "https://api.anthropic.com/v1/messages"),
    ("OpenAI", "https://api.openai.com/v1/responses"),
])
@pytest.mark.parametrize("batch", [False, True])
def test_new_provider_request_and_usage(monkeypatch, engine, endpoint, batch):
    rows = [{"id": "52", "verdict": "legitimate"}, {"id": "51", "verdict": "scam"}]
    metadata = {"emails": [{"id": "51"}, {"id": "52"}]} if batch else {"subject": "Test"}
    answer = {"results": rows} if batch else {"verdict": "scam"}
    post = Mock(return_value=Mock(status_code=200, headers={}, json=Mock(return_value=reply(engine, answer))))
    monkeypatch.setattr(ai.requests, "post", post)
    result = ai.classify(engine, "test-key", metadata, "test-model")
    assert result == ({"51": "scam", "52": "legitimate"} if batch else "scam")
    assert result.usage == {"input": 100, "output": 20, "total": 120}
    assert post.call_args.args == (endpoint,)
    kwargs = post.call_args.kwargs
    payload = kwargs["json"]
    assert payload["model"] == "test-model" and kwargs["allow_redirects"] is False
    if engine == "Claude":
        assert kwargs["headers"]["x-api-key"] == "test-key"
        assert payload["output_config"]["format"]["schema"] == ai.request_options(metadata)[1]
        assert json.loads(payload["messages"][0]["content"]) == metadata
    else:
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert payload["store"] is False and payload["text"]["format"]["strict"] is True
        assert json.loads(payload["input"]) == metadata


@pytest.mark.parametrize("engine", ["Claude", "OpenAI"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_invalid_batch_or_incomplete_response_keeps_usage(monkeypatch, engine, incomplete):
    body = reply(engine, {"results": [{"id": "99", "verdict": "scam"}]})
    if incomplete:
        body.update(status="incomplete", stop_reason="max_tokens")
    monkeypatch.setattr(ai.requests, "post", Mock(return_value=Mock(status_code=200, headers={},
                        json=Mock(return_value=body))))
    with pytest.raises(ai.PreprocessingError) as error:
        ai.classify(engine, "test-key", {"emails": [{"id": "51"}]})
    assert error.value.usage["total"] == 120


def test_openai_exhausted_quota_is_not_retried(monkeypatch):
    import asyncio

    response = Mock(status_code=429, headers={}, json=Mock(return_value={"error": {"code": "insufficient_quota"}}))
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    post = Mock(return_value=response)
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ai.ai_quotas, "_provider_memory", {})
    monkeypatch.setattr(ai, "_other_next", {})

    async def call(fn, *args):
        return fn(*args)

    with pytest.raises(ai.PreprocessingError, match="Aucun retry") as error:
        asyncio.run(ai.provider_call({"owner": "openai-test"}, "test-key", {}, {"cancelled": False}, call,
                                     lambda _: None, "OpenAI"))
    assert error.value.quota_exhausted
    assert post.call_count == 1
