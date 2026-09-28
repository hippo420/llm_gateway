import argparse
import json

import pytest

from llm_gateway.adapters.base import AdapterChatChunk
from llm_gateway.adapters.factory import ADAPTER_REGISTRY
from llm_gateway.core.errors import UpstreamUnavailableError
from llm_gateway.experiment.benchmark import markdown, measure, run
from llm_gateway.schemas.chat import ChatCompletionRequest

from .conftest import FakeAdapter
from .test_dynamic_config import BODY, DEPLOYMENT
from .test_dynamic_config import system as system
from .test_registry import SAMPLE
from .test_resilience import fault_system as fault_system


async def test_fixed_benchmark_warmup_no_retry_fallback_or_prompt_output(fault_system):
    manager, app, _, plans, attempts, _, _ = fault_system
    dep = manager.registry.candidates("qwen-7b")[0]
    plans[DEPLOYMENT] = [
        [AdapterChatChunk(delta="private output", finish_reason="stop")],
        UpstreamUnavailableError("private upstream failure"),
    ]
    result = await measure(
        app.state.chat_service,
        dep,
        [ChatCompletionRequest.model_validate(BODY)],
        concurrency=2,
        repeat=3,
        warmup=1,
    )
    assert attempts == {DEPLOYMENT: 4}  # 1 warmup + 3 measured, no retry
    assert result["summary"]["samples"] == 3
    assert result["summary"]["error_rate"] == pytest.approx(1 / 3)
    assert len(result["samples"]) == 3
    assert result["samples"][0]["error_code"] == "GW-5002"
    assert result["gpu_memory_peak_gb"] is None and result["quality"] is None
    assert "private" not in json.dumps(result)


async def test_cli_runner_produces_sequential_conditions_and_report(tmp_path, monkeypatch):
    monkeypatch.setitem(ADAPTER_REGISTRY, "ollama", FakeAdapter)
    config, data = tmp_path / "config.yaml", tmp_path / "dataset.jsonl"
    config.write_text(SAMPLE, encoding="utf-8")
    data.write_text(json.dumps(BODY) + "\n", encoding="utf-8")
    args = argparse.Namespace(
        config=config,
        dataset=data,
        deployment=[DEPLOYMENT],
        concurrency=[1, 2],
        repeat=2,
        warmup=1,
        out=tmp_path / "out.json",
    )
    result = await run(args)
    assert [r["concurrency"] for r in result["runs"]] == [1, 2]
    assert all(r["summary"]["samples"] == 2 for r in result["runs"])
    assert all(r["summary"]["input_tokens"] == 24 for r in result["runs"])
    assert len(result["dataset_sha256"]) == 64
    rendered = markdown(result)
    assert "## 실행 조건" in rendered and "## 결론 / 결정" in rendered
    assert "자동 채택 판단 없음" in rendered
    assert "hello" not in json.dumps(result)


async def test_failed_warmup_aborts_measured_phase(fault_system):
    manager, app, _, plans, attempts, _, _ = fault_system
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline")]
    dep = manager.registry.candidates("qwen-7b")[0]
    with pytest.raises(ValueError, match="warm-up failed"):
        await measure(
            app.state.chat_service,
            dep,
            [ChatCompletionRequest.model_validate(BODY)],
            concurrency=1,
            repeat=20,
            warmup=1,
        )
    assert attempts == {DEPLOYMENT: 1}
