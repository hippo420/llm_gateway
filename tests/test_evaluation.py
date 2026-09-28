from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from llm_gateway.adapters.base import AdapterChatChunk
from llm_gateway.core.errors import UpstreamUnavailableError
from llm_gateway.evaluation.calibration import calibrate, ranks
from llm_gateway.evaluation.cli import execute
from llm_gateway.evaluation.config import EvaluationConfig, SimilarityConfig
from llm_gateway.evaluation.dataset import Dataset, load_dataset
from llm_gateway.evaluation.metrics import quality_metrics
from llm_gateway.evaluation.models import Case, DatasetInfo, Performance, Run, Sample
from llm_gateway.evaluation.report import build_summary, markdown
from llm_gateway.evaluation.runner import evaluate
from llm_gateway.evaluation.scorers.judge import JudgeScores
from llm_gateway.evaluation.scorers.rule import score
from llm_gateway.evaluation.scorers.similarity import SimilarityScorer
from llm_gateway.evaluation.store import EvaluationStore

from .test_dynamic_config import DEPLOYMENT
from .test_dynamic_config import system as system
from .test_resilience import CONFIG as RESILIENCE_CONFIG
from .test_resilience import SECOND
from .test_resilience import fault_system as fault_system


@pytest.fixture
def case():
    return Case(
        id="qa-001",
        request_type="simple_qa",
        question="매출은?",
        context=["가상기업의 매출은 1200억원이다."],
        reference_answer="REFERENCE_ONLY 매출은 1200억원이다. [1]",
        must_include=["매출"],
        must_not_include=["확정 수익"],
        expected_numbers=["1200"],
        require_citations=True,
    )


def test_rule_checks_numeric_boundaries_citations_and_json(case):
    assert score(case, "매출은 1,200억원이다. [1]")[0] == 1
    value, checks = score(case, "매출은 12000억원이며 확정 수익이다. [2]")
    assert value < 1
    assert not checks["number_0"] and not checks["exclude_0"] and not checks["citation_indices"]
    assert not score(case, "매출은 -1200억원이다. [1]")[1]["number_0"]
    assert score(case, "매출은 １２００억원이다. [1]")[0] == 1
    assert score(case, "매출: 1200. [1]")[0] == 1
    citation_case = case.model_copy(update={"expected_numbers": ["1"]})
    assert not score(citation_case, "매출 미공개. [1]")[1]["number_0"]
    json_case = case.model_copy(update={"expected_format": "json_object", "max_chars": 50})
    assert score(json_case, '{"매출":1200,"source":"[1]"}')[0] == 1
    assert not score(json_case, "[]")[1]["json_object"]
    assert not score(case, "")[1]["nonempty"]


def test_default_datasets_are_synthetic_unique_and_references_pass_rules():
    config = EvaluationConfig.load(Path("config/evaluation.yaml"))
    datasets = [load_dataset(spec) for spec in config.datasets]
    assert len(datasets) == 3
    assert {d.info.request_type for d in datasets} == {
        "simple_qa",
        "report_analysis",
        "news_summary",
    }
    for dataset in datasets:
        assert dataset.info.provenance == "synthetic"
        assert len(dataset.cases) == 30
        assert all("합성" in case.context[0] for case in dataset.cases)
        assert all(score(case, case.reference_answer)[0] == 1 for case in dataset.cases)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../bad"},
        {"context": []},
        {"request_type": "arbitrary"},
        {"must_include": ["x"], "must_not_include": ["x"]},
        {"min_chars": 10, "max_chars": 2},
        {"expected_numbers": ["nan"]},
    ],
)
def test_invalid_case_rejected(case, change):
    with pytest.raises(ValidationError):
        Case.model_validate({**case.model_dump(), **change})


def test_dataset_rejects_duplicate_ids(tmp_path, case):
    path = tmp_path / "data.jsonl"
    path.write_text((case.model_dump_json() + "\n") * 2, encoding="utf-8")
    config = EvaluationConfig.model_validate({"datasets": [{"name": "qa", "path": path}]})
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(config.datasets[0])


@pytest.mark.parametrize(
    "vectors,expected",
    [
        ([[1, 0], [1, 0]], 1),
        ([[1, 0], [0, 1]], 0.5),
        ([[1, 0], [-1, 0]], 0),
    ],
)
async def test_embedding_contract_and_cosine_scaling(vectors, expected):
    def handler(request):
        assert request.url.path == "/api/embed"
        assert json.loads(request.content) == {
            "model": "bge-m3",
            "input": ["answer", "reference"],
            "truncate": False,
        }
        return httpx.Response(200, json={"embeddings": vectors})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scorer = SimilarityScorer(SimilarityConfig(enabled=True, endpoint="http://embed"), client)
        assert await scorer.score("answer", "reference") == expected
        await scorer.aclose()
        assert not client.is_closed  # injected client remains owned by its caller


@pytest.mark.parametrize(
    "vectors",
    [[], [[1], [1, 2]], [[0, 0], [1, 0]], [[True], [1]], [[float("nan")], [1]], ["bad", [1]]],
)
async def test_malformed_embeddings_are_missing_not_zero(vectors):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps({"embeddings": vectors}))
        )
    ) as client:
        scorer = SimilarityScorer(SimilarityConfig(endpoint="http://embed"), client)
        with pytest.raises(ValueError):
            await scorer.score("answer", "reference")


def batch_config(**changes):
    return EvaluationConfig.model_validate(
        {"datasets": [{"name": "qa", "path": "unused"}], "concurrency": 2, **changes}
    )


def dataset_for(case, count=3):
    cases = [case.model_copy(update={"id": f"qa-{i:03}"}) for i in range(count)]
    return Dataset(
        DatasetInfo(
            name="qa",
            sha256="a" * 64,
            provenance="synthetic",
            request_type="simple_qa",
            cases=count,
        ),
        cases,
    )


JUDGE = {
    "faithfulness": 0.9,
    "answer_relevance": 0.8,
    "context_relevance": 0.1,
    "citation_accuracy": 1.0,
    "hallucination": 0.0,
}


@pytest.mark.parametrize("bad", [float("nan"), 1.1, -0.1, True, "0.9"])
def test_judge_scores_strict_and_finite(bad):
    with pytest.raises(ValidationError):
        JudgeScores.model_validate({**JUDGE, "faithfulness": bad})


async def test_runner_stages_performance_before_judge_and_keeps_failures(fault_system, case):
    manager, app, _, plans, attempts, _, calls = fault_system
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline")]
    plans[SECOND] = [
        [AdapterChatChunk(delta=json.dumps(JUDGE), finish_reason="stop")],
        [AdapterChatChunk(delta="invalid judge JSON", finish_reason="stop")],
    ]
    config = batch_config(judge={"deployment_id": SECOND})
    run = await evaluate(
        manager.registry,
        app.state.chat_service,
        config,
        [dataset_for(case)],
        [DEPLOYMENT],
        "test-run",
        save_answers=True,
    )
    assert attempts == {DEPLOYMENT: 3, SECOND: 2}  # no retry/fallback despite gateway defaults
    assert [name for name, _ in calls] == [DEPLOYMENT] * 3 + [SECOND] * 2
    for _, request in calls[:3]:
        assert "REFERENCE_ONLY" not in " ".join(m.content for m in request.messages)
    assert "REFERENCE_ONLY" in calls[-1][1].messages[1].content
    assert run.samples[0].status == "error" and run.samples[0].scores == {}
    assert run.samples[1].scores["overall"] == pytest.approx((0.9 + 0.8 + 1 + 1) / 4)
    assert run.samples[1].scores["context_relevance"] == 0.1  # not folded into model quality
    assert run.samples[2].scorer_errors == {"judge": "judge_failed"}
    assert "overall" not in run.samples[2].scores
    summary = build_summary(run)
    assert summary.rows[0].error_rate == pytest.approx(1 / 3)
    assert summary.rows[0].score_samples["overall"] == 1
    assert summary.policy_input["recommendations"][0]["deployment_id"] is None
    assert "미측정" in markdown(summary)


async def test_self_judging_rejected_before_target_call(fault_system, case):
    manager, app, _, _, attempts, _, _ = fault_system
    with pytest.raises(ValueError, match="judge model"):
        await evaluate(
            manager.registry,
            app.state.chat_service,
            batch_config(judge={"deployment_id": DEPLOYMENT}),
            [dataset_for(case)],
            [DEPLOYMENT],
            "self-judge",
        )
    assert not attempts


async def test_l1_only_batch_does_not_store_answers_or_invent_overall(fault_system, case):
    manager, app, _, _, attempts, _, _ = fault_system
    run = await evaluate(
        manager.registry,
        app.state.chat_service,
        batch_config(),
        [dataset_for(case)],
        [DEPLOYMENT, SECOND],
        "l1-only",
    )
    assert attempts == {DEPLOYMENT: 3, SECOND: 3}
    assert all(s.answer is None and s.answer_sha256 for s in run.samples)
    assert all(set(s.scores) == {"rule"} for s in run.samples)
    assert "REFERENCE_ONLY" not in run.model_dump_json()
    assert all(r.scores["overall"] is None for r in build_summary(run).rows)


async def test_cli_batch_disables_production_breakers_and_persists(fault_system, tmp_path, case):
    _, _, _, plans, attempts, closed, _ = fault_system
    gateway = tmp_path / "gateway.yaml"
    gateway.write_text(
        RESILIENCE_CONFIG.replace(
            "enabled: false, failure_threshold: 2", "enabled: true, failure_threshold: 1"
        ),
        encoding="utf-8",
    )
    data = tmp_path / "cases.jsonl"
    data.write_text(
        "\n".join(c.model_dump_json() for c in dataset_for(case).cases), encoding="utf-8"
    )
    config = tmp_path / "evaluation.yaml"
    config.write_text(
        json.dumps({"datasets": [{"name": "qa", "path": "cases.jsonl"}]}), encoding="utf-8"
    )
    plans[DEPLOYMENT] = [UpstreamUnavailableError("offline"), UpstreamUnavailableError("offline")]
    args = Namespace(
        evaluation_config=config,
        config=gateway,
        out_dir=tmp_path / "results",
        run_id="cli-run",
        deployment=[DEPLOYMENT],
        save_answers=False,
    )
    await execute(args)
    run = EvaluationStore(args.out_dir).load_run(args.run_id)
    assert attempts == closed == {DEPLOYMENT: 3}
    assert [s.status for s in run.samples] == ["error", "error", "success"]
    assert (args.out_dir / args.run_id / "report.md").is_file()
    with pytest.raises(ValueError, match="already exists"):
        await execute(args)
    assert attempts == {DEPLOYMENT: 3}


async def test_embedding_failure_is_missing_and_all_embeddings_precede_judge(fault_system, case):
    manager, app, _, plans, _, _, calls = fault_system
    plans[SECOND] = [[AdapterChatChunk(delta=json.dumps(JUDGE), finish_reason="stop")]] * 3
    invocation = 0

    def embedding_response(request):
        nonlocal invocation
        invocation += 1
        assert len(calls) == 3  # all targets completed; no judge has started
        if invocation == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"embeddings": [[1, 0], [1, 0]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(embedding_response)) as client:
        config = batch_config(similarity={"enabled": True}, judge={"deployment_id": SECOND})
        run = await evaluate(
            manager.registry,
            app.state.chat_service,
            config,
            [dataset_for(case)],
            [DEPLOYMENT],
            "embedding-run",
            similarity=SimilarityScorer(config.similarity, client),
        )
    assert invocation == 3
    assert run.samples[0].scorer_errors == {"similarity": "embedding_failed"}
    assert "similarity" not in run.samples[0].scores
    assert build_summary(run).rows[0].score_samples["similarity"] == 2
    assert all("overall" in s.scores for s in run.samples)


@pytest.mark.parametrize("finish,citation", [("length", 1.0), ("stop", None)])
async def test_incomplete_judge_and_missing_required_citation_are_rejected(
    fault_system, case, finish, citation
):
    manager, app, _, plans, _, _, _ = fault_system
    plans[SECOND] = [
        [
            AdapterChatChunk(
                delta=json.dumps({**JUDGE, "citation_accuracy": citation}), finish_reason=finish
            )
        ]
    ]
    run = await evaluate(
        manager.registry,
        app.state.chat_service,
        batch_config(judge={"deployment_id": SECOND}),
        [dataset_for(case, 1)],
        [DEPLOYMENT],
        "bad-judge",
    )
    assert run.samples[0].scorer_errors == {"judge": "judge_failed"}
    assert "overall" not in run.samples[0].scores


def scored_run(provenance="service", count=20):
    info = DatasetInfo(
        name="qa", sha256="b" * 64, provenance=provenance, request_type="simple_qa", cases=count
    )
    samples = [
        Sample(
            dataset="qa",
            case_id=f"qa-{i:03}",
            request_type="simple_qa",
            deployment_id=DEPLOYMENT,
            status="success",
            answer_sha256=hashlib.sha256(f"answer-{i}".encode()).hexdigest(),
            answer=f"private answer {i}",
            scores={"rule": 1, "overall": 0.8 + i / 100},
            performance=Performance(total_sec=1, ttft_sec=0.1, output_tps=20),
        )
        for i in range(count)
    ]
    return Run(
        run_id="scored-run",
        created_at="2026-09-28T00:00:00+00:00",
        datasets=[info],
        deployments={DEPLOYMENT: {"config_sha256": "c" * 64}},
        scorer_metadata={"judge": {"enabled": True, "model": "judge", "prompt_version": "v1"}},
        criteria=batch_config().criteria.model_dump(),
        samples=samples,
    )


def write_ratings(path, run, transform=lambda value: value):
    rows = [
        {
            "run_id": run.run_id,
            "dataset": sample.dataset,
            "dataset_sha256": run.datasets[0].sha256,
            "case_id": sample.case_id,
            "deployment_id": sample.deployment_id,
            "answer_sha256": sample.answer_sha256,
            "human_score": transform(sample.scores["overall"]),
            "reviewer": "test-reviewer",
        }
        for sample in run.samples
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return rows


def test_human_calibration_requires_20_distinct_cases_and_correct_answer_hash(tmp_path):
    run = scored_run(count=19)
    path = tmp_path / "human.jsonl"
    write_ratings(path, run)
    assert calibrate(run, path).status == "insufficient_samples"
    run = scored_run()
    rows = write_ratings(path, run)
    calibration = calibrate(run, path)
    assert calibration.status == "validated"
    assert calibration.pearson == calibration.spearman == 1
    assert calibration.mae == 0
    rows[0]["answer_sha256"] = "wrong"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        calibrate(run, path)
    rows = write_ratings(path, run)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(rows[0]) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        calibrate(run, path)


@pytest.mark.parametrize(
    "mode,status", [("inverse", "below_threshold"), ("constant", "constant_scores")]
)
def test_bad_or_constant_judge_correlation_is_not_validated(tmp_path, mode, status):
    run = scored_run()
    path = tmp_path / "human.jsonl"
    write_ratings(path, run, lambda score: 1 - score if mode == "inverse" else 0.5)
    assert calibrate(run, path).status == status
    assert ranks([1, 1, 3, 2]) == [1.5, 1.5, 4, 3]


def test_policy_requires_real_data_calibration_coverage_and_quality(tmp_path):
    run = scored_run()
    assert build_summary(run).policy_input["recommendations"][0]["deployment_id"] is None
    path = tmp_path / "human.jsonl"
    write_ratings(path, run)
    run.calibration = calibrate(run, path)
    policy = build_summary(run).policy_input
    assert policy["recommendations"][0]["deployment_id"] == DEPLOYMENT
    assert policy["automatic_routing_change"] is False
    run.datasets[0].provenance = "synthetic"
    assert not build_summary(run).policy_input["matrix"][0]["eligible"]
    run.datasets[0].provenance = "service"
    run.samples[0].scores.pop("overall")
    assert "incomplete_judge_coverage" in build_summary(run).policy_input["matrix"][0]["reasons"]


def test_store_atomic_visibility_missing_metrics_and_safe_paths(tmp_path, case):
    store = EvaluationStore(tmp_path)
    run = scored_run()
    dataset = dataset_for(case, 20)
    dataset.info.sha256 = run.datasets[0].sha256
    summary = store.save(run, [dataset])
    assert store.load_run(run.run_id) == run
    assert store.load_summary(run.run_id) == summary
    assert "private answer" not in summary.model_dump_json()
    assert "private answer" in (store.path(run.run_id) / "human-review.jsonl").read_text(
        encoding="utf-8"
    )
    with pytest.raises(FileExistsError):
        store.save(run, [dataset])
    with pytest.raises(ValueError):
        store.path("../outside")
    bad = tmp_path / "bad-run"
    bad.mkdir()
    (bad / "summary.json").write_text("{broken", encoding="utf-8")
    assert len(store.summaries()) == 1
    exposed = quality_metrics(tmp_path, {DEPLOYMENT}).decode()
    assert 'scorer="overall"' in exposed
    assert 'scorer="similarity"' not in exposed
    assert "private answer" not in exposed and run.run_id not in exposed
    latest = run.model_copy(
        deep=True, update={"run_id": "later-run", "created_at": "2026-09-29T00:00:00+00:00"}
    )
    for sample in latest.samples:
        sample.scores.pop("overall")
    store.save(latest, [dataset])
    assert 'scorer="overall"' not in quality_metrics(tmp_path, {DEPLOYMENT}).decode()
    assert 'deployment_id="' not in quality_metrics(tmp_path, set()).decode()


async def test_admin_reports_and_metrics_read_completed_batch_without_exposing_answers(
    system, tmp_path, case
):
    _, _, _, app, client = system
    app.state.settings.evaluation_results_path = tmp_path / "evaluation-results"
    run = scored_run()
    EvaluationStore(app.state.settings.evaluation_results_path).save(run, [dataset_for(case, 20)])
    assert (
        await client.get("/admin/evaluations", headers={"Authorization": ""})
    ).status_code == 401
    response = await client.get(f"/admin/evaluations/{run.run_id}")
    assert response.status_code == 200
    assert "private answer" not in response.text
    missing = await client.get("/admin/evaluations/unknown")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "GW-4008"
    assert missing.json()["error"]["request_id"]
    assert len((await client.get("/admin/evaluations")).json()["runs"]) == 1
    assert "llm_gateway_quality_score" in (await client.get("/metrics")).text
