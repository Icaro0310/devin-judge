import json

from poordjaevin.backends import ollama_logits
from poordjaevin.backends.ollama_logits import OllamaLogitsBackend


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_backend_reports_local_token_and_request_usage(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, json.loads(request.data), timeout))
        return FakeResponse({
            "logprobs": [{
                "top_logprobs": [
                    {"token": "Yes", "logprob": -0.1},
                    {"token": "No", "logprob": -2.0},
                ],
            }],
            "prompt_eval_count": 37,
            "eval_count": 1,
        })

    monkeypatch.setattr(ollama_logits.urllib.request, "urlopen", fake_urlopen)
    backend = OllamaLogitsBackend(model="test", host="http://localhost:11434")

    assert backend.entail_probs([("state", "hypothesis")])[0] > 0.5
    usage = backend.usage()

    assert len(requests) == 1
    assert requests[0][0].endswith("/api/chat")  # native endpoint honours keep_alive
    assert requests[0][1]["options"]["num_predict"] == 1
    assert requests[0][1]["logprobs"] is True
    assert usage["ollama_requests"] == 1
    assert usage["ollama_prompt_tokens"] == 37
    assert usage["ollama_completion_tokens"] == 1
    assert usage["external_api_tokens"] == 0
    assert usage["external_credit_cost"] == 0


def test_backend_separates_warmup_usage(monkeypatch):
    def fake_urlopen(_request, timeout):
        return FakeResponse({
            "logprobs": [{
                "top_logprobs": [
                    {"token": "Yes", "logprob": -0.1},
                    {"token": "No", "logprob": -2.0},
                ],
            }],
            "prompt_eval_count": 20,
            "eval_count": 1,
        })

    monkeypatch.setattr(ollama_logits.urllib.request, "urlopen", fake_urlopen)
    backend = OllamaLogitsBackend(model="test", host="http://localhost:11434")
    backend.warmup()
    backend.entail_probs([("state", "hypothesis")])

    usage = backend.usage()
    assert usage["ollama_requests"] == 1
    assert usage["warmup_requests"] == 1
    assert usage["ollama_prompt_tokens"] == 20
    assert usage["warmup_prompt_tokens"] == 20
