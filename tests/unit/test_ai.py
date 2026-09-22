"""AI assistant: provider client, diagnosis, config drafting, API and UI wiring.

The provider is always faked; nothing here talks to a real AI service.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from dashdashgo.ai.assistant import visible_text
from dashdashgo.ai.client import Completion, OpenAICompatibleClient, parse_json_object
from dashdashgo.errors import AIError
from dashdashgo.metadata.models import (
    RunRecord,
    RunStatus,
    StageRecord,
    StageStatus,
    Trigger,
    new_run_id,
    utcnow,
)
from dashdashgo.observability.logging import redactor
from dashdashgo.storage import RunArtifacts
from tests.conftest import REPORTS_DIR, TEST_ENV
from tests.unit.test_config_api import make_client

# --- the provider client -------------------------------------------------------------


def _reply(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def make_http_client(
    responses: list[httpx.Response], seen: list[dict[str, Any]], models: Sequence[str] = ("a", "b")
) -> OpenAICompatibleClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers["authorization"],
                "body": json.loads(request.content),
            }
        )
        return responses.pop(0)

    return OpenAICompatibleClient(
        api_key="sk-test-key",
        base_url="https://ai.example.com/v1beta/openai",
        models=models,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_client_sends_json_mode_images_and_the_key() -> None:
    seen: list[dict[str, Any]] = []
    client = make_http_client([httpx.Response(200, json=_reply('{"ok": true}'))], seen)
    completion = client.complete_json("sys", "user text", images=[b"\x89PNG"])
    assert completion == Completion({"ok": True}, "a")
    request = seen[0]
    assert request["url"] == "https://ai.example.com/v1beta/openai/chat/completions"
    assert request["auth"] == "Bearer sk-test-key"
    assert request["body"]["response_format"] == {"type": "json_object"}
    content = request["body"]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "user text"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_client_retries_overload_then_falls_back_to_the_next_model() -> None:
    seen: list[dict[str, Any]] = []
    overloaded = httpx.Response(503, json={"error": {"message": "high demand"}})
    client = make_http_client(
        [overloaded, overloaded, overloaded, httpx.Response(200, json=_reply('{"x": 1}'))], seen
    )
    completion = client.complete_json("s", "u")
    assert completion.model == "b"
    assert [r["body"]["model"] for r in seen] == ["a", "a", "a", "b"]


def test_client_skips_retired_models_immediately() -> None:
    seen: list[dict[str, Any]] = []
    client = make_http_client(
        [
            httpx.Response(404, json=[{"error": {"message": "no longer available"}}]),
            httpx.Response(200, json=_reply('{"x": 1}')),
        ],
        seen,
    )
    assert client.complete_json("s", "u").model == "b"
    assert len(seen) == 2


def test_client_does_not_retry_a_bad_key() -> None:
    seen: list[dict[str, Any]] = []
    client = make_http_client([httpx.Response(401, json={"error": {"message": "bad key"}})], seen)
    with pytest.raises(AIError, match="refused the request: HTTP 401 bad key"):
        client.complete_json("s", "u")
    assert len(seen) == 1


def test_client_reports_every_model_when_all_fail() -> None:
    seen: list[dict[str, Any]] = []
    busy = httpx.Response(429, json={"error": {"message": "quota"}})
    client = make_http_client([busy] * 6, seen)
    with pytest.raises(AIError, match=r"a: HTTP 429 quota; b: HTTP 429 quota"):
        client.complete_json("s", "u")


@pytest.mark.parametrize(
    ("text", "expected"),
    [('{"a": 1}', {"a": 1}), ('```json\n{"a": 1}\n```', {"a": 1}), ("```\n{}\n```", {})],
)
def test_json_replies_may_be_fenced(text: str, expected: dict[str, Any]) -> None:
    assert parse_json_object(text) == expected


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_non_object_replies_are_errors(text: str) -> None:
    with pytest.raises(AIError):
        parse_json_object(text)


def test_visible_text_drops_markup_and_scripts() -> None:
    page = (
        "<html><style>.x{}</style><script>var pw='x'</script><h1>Sign in</h1><p>Bad&amp;wrong</p>"
    )
    assert visible_text(page) == "Sign in Bad&wrong"


# --- a scripted fake provider ---------------------------------------------------------


class FakeAI:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system: str, user: str, *, images: Sequence[bytes] = ()) -> Completion:
        self.calls.append({"system": system, "user": user, "images": list(images)})
        return Completion(self.replies.pop(0), "fake-model")


DIAGNOSIS = {
    "summary": "The login was rejected.",
    "likely_cause": "Metabase says 'did not match stored password'.",
    "category": "credentials",
    "suggested_fix": "Update METABASE_PASSWORD in .env.",
    "transient": False,
    "config_overrides": [],
    "confidence": 0.9,
}


def failed_run(client: TestClient, report: str = "weekly_sales") -> RunRecord:
    container = client.app.state.ddg.container  # type: ignore[attr-defined]
    run = RunRecord(
        run_id=new_run_id(),
        report=report,
        trigger=Trigger.API,
        status=RunStatus.FAILED,
        started_at=utcnow(),
        finished_at=utcnow(),
        error_type="AuthenticationError",
        error_stage="login",
        error_message="Metabase rejected the credentials",
    )
    container.runs.save_run(run)
    container.runs.save_stage(
        StageRecord(
            run_id=run.run_id,
            report=report,
            stage="acquisition.login",
            status=StageStatus.FAILED,
            started_at=utcnow(),
            message="Did not match stored password",
        )
    )
    artifacts = RunArtifacts(run.report, run.run_date, run.run_id)
    log_line = {"level": "ERROR", "stage": "login", "message": "login failed for s3cret-Passw0rd"}
    container.storage.put_bytes(json.dumps(log_line).encode(), artifacts.key("logs", "run.log"))
    container.storage.put_bytes(b"\x89PNG fake", artifacts.key("failures", "login.png"))
    container.storage.put_bytes(
        b"<h1>Sign in to Metabase</h1><script>x()</script>",
        artifacts.key("failures", "login.html"),
    )
    return run


@pytest.fixture
def ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def client(tmp_path: Path, ai: FakeAI) -> Iterator[TestClient]:
    with make_client(tmp_path, ai_client=ai) as test_client:
        yield test_client


# --- diagnosis ---------------------------------------------------------------------


def test_diagnosis_sends_redacted_evidence_and_stores_the_result(
    client: TestClient, ai: FakeAI
) -> None:
    redactor.register(TEST_ENV["METABASE_PASSWORD"])
    ai.replies.append(DIAGNOSIS)
    run = failed_run(client)

    response = client.post(f"/api/runs/{run.run_id}/diagnose")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["category"] == "credentials" and body["model"] == "fake-model"

    call = ai.calls[0]
    evidence = json.loads(call["user"])
    assert evidence["run"]["error_type"] == "AuthenticationError"
    assert evidence["timeline"][0]["message"] == "Did not match stored password"
    assert evidence["page_text"] == "Sign in to Metabase"
    assert "credentials" in evidence["config"]
    assert TEST_ENV["METABASE_PASSWORD"] not in call["user"]  # masked everywhere
    assert call["images"] == [b"\x89PNG fake"]

    stored = client.get(f"/api/runs/{run.run_id}/diagnosis").json()
    assert stored["summary"] == "The login was rejected."
    page = client.get(f"/runs/{run.run_id}").text
    assert "The login was rejected." in page and "Diagnose again" in page


def test_suggested_changes_are_validated_before_they_are_offered(
    client: TestClient, ai: FakeAI
) -> None:
    run = failed_run(client)
    cases = [
        (["browser.navigation_timeout_ms=90000"], ["browser.navigation_timeout_ms=90000"], ""),
        (["retry.max_attempts=0"], [], "does not validate"),
        (["source.base_url=https://evil.example.com"], [], "protected setting"),
        (["source.credentials.password=x"], [], "protected setting"),
        (["not an override"], [], "not key.path=value"),
    ]
    for suggested, offered, note in cases:
        ai.replies.append({**DIAGNOSIS, "config_overrides": suggested})
        body = client.post(f"/api/runs/{run.run_id}/diagnose").json()
        assert body["config_overrides"] == offered, suggested
        assert note in body["overrides_note"]


def test_retry_with_the_suggested_change(client: TestClient, ai: FakeAI) -> None:
    run = failed_run(client)
    ai.replies.append({**DIAGNOSIS, "config_overrides": ["browser.timeout_ms=60000"]})
    client.post(f"/api/runs/{run.run_id}/diagnose")
    page = client.get(f"/runs/{run.run_id}").text
    assert "Retry with suggested change" in page
    assert 'data-overrides="[&#34;browser.timeout_ms=60000&#34;]"' in page

    retry = client.post(
        f"/api/runs/{run.run_id}/retry", json={"overrides": ["browser.timeout_ms=60000"]}
    )
    assert retry.status_code == 202
    assert retry.json()["parent_run_id"] == run.run_id


def test_run_overrides_are_validated_by_the_api(client: TestClient) -> None:
    bad = client.post(
        "/api/reports/weekly_sales/runs", json={"overrides": ["retry.max_attempts=0"]}
    )
    assert bad.status_code == 422
    unsafe = client.post(
        "/api/reports/weekly_sales/runs",
        json={"overrides": ["source.filters.region=${CLICKHOUSE_PASSWORD}"]},
    )
    assert unsafe.status_code == 422 and "not allowed" in unsafe.json()["detail"]


def test_only_failed_runs_can_be_diagnosed(client: TestClient) -> None:
    container = client.app.state.ddg.container  # type: ignore[attr-defined]
    run = RunRecord(
        run_id=new_run_id(),
        report="weekly_sales",
        trigger=Trigger.API,
        status=RunStatus.SUCCESS,
        started_at=utcnow(),
    )
    container.runs.save_run(run)
    assert client.post(f"/api/runs/{run.run_id}/diagnose").status_code == 409
    assert client.post("/api/runs/nope/diagnose").status_code == 404


def test_everything_is_off_without_an_api_key(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        run = failed_run(client)
        assert client.get("/api/health").json()["ai"] == "disabled"
        assert client.get("/api/ai").json()["enabled"] is False
        assert client.post(f"/api/runs/{run.run_id}/diagnose").status_code == 503
        assert "Diagnose with AI" not in client.get(f"/runs/{run.run_id}").text
        assert "Draft with AI" not in client.get("/reports/new").text


def test_health_shows_the_assistant(client: TestClient) -> None:
    assert client.get("/api/health").json()["ai"] == "enabled"
    assert "Draft with AI" in client.get("/reports/new").text


# --- config drafting ---------------------------------------------------------------


SAMPLE = b"Month,Plan,MRR\n2026-07,Pro,1200.50\n2026-08,Pro,1300.00\n"


def drafted_config(name: str, **changes: Any) -> str:
    raw = yaml.safe_load((REPORTS_DIR / "mrr_monthly.yaml").read_text())
    raw["name"] = name
    raw["destination"]["table"] = name
    raw.update(changes)
    return yaml.safe_dump(raw, sort_keys=False)


def _draft(client: TestClient, name: str = "mrr_draft", **extra: Any) -> httpx.Response:
    body = {"filename": "export.csv", "content_base64": base64.b64encode(SAMPLE).decode(), **extra}
    return client.post(f"/api/reports/{name}/config/draft", json=body)


def test_draft_profiles_the_sample_and_validates_the_result(client: TestClient, ai: FakeAI) -> None:
    ai.replies.append({"yaml": drafted_config("mrr_draft"), "notes": ["check the card name"]})
    response = _draft(client, from_report="mrr_monthly")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["problems"] == [] and body["notes"] == ["check the card name"]
    assert body["yaml"].startswith("name: mrr_draft")

    prompt = json.loads(ai.calls[0]["user"])
    assert prompt["format"] == "csv" and prompt["rows_in_sample"] == 2
    assert [c["name"] for c in prompt["columns"]] == ["Month", "Plan", "MRR"]
    assert "name: mrr_draft" in prompt["reference_config"]  # the chosen reference, renamed
    assert "properties" in prompt["config_schema"]


def test_an_invalid_draft_gets_one_repair_round(client: TestClient, ai: FakeAI) -> None:
    ai.replies.extend(
        [
            {"yaml": drafted_config("mrr_draft", retry={"max_attempts": 0}), "notes": []},
            {"yaml": drafted_config("mrr_draft"), "notes": ["fixed retry"]},
        ]
    )
    body = _draft(client).json()
    assert body["problems"] == [] and body["notes"] == ["fixed retry"]
    repair = json.loads(ai.calls[1]["user"])["previous_attempt"]
    assert any("retry.max_attempts" in p for p in repair["problems"])


def test_a_draft_that_stays_invalid_is_returned_with_its_problems(
    client: TestClient, ai: FakeAI
) -> None:
    broken = {"yaml": drafted_config("mrr_draft", retry={"max_attempts": 0}), "notes": []}
    ai.replies.extend([broken, broken])
    body = _draft(client).json()
    assert [p["location"] for p in body["problems"]] == ["retry.max_attempts"]


def test_draft_rejects_bad_samples(client: TestClient) -> None:
    assert _draft(client, filename="report.pdf").status_code == 422
    assert _draft(client, name="Bad Name").status_code == 422
    bad = client.post(
        "/api/reports/x_report/config/draft",
        json={"filename": "a.csv", "content_base64": "%%%"},
    )
    assert bad.status_code == 422
