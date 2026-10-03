from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from chatspark.plugins import RankedEvidence
from chatspark.plugins.contracts import LaneRequest
from chatspark.profiles import load_profile
from chatspark.runtime.factory import get_retriever
from chatspark.storage.evidence import EvidenceError, EvidenceStore


def pipeline(candidate, *, processor=None, context_builder=None, lane=None):
    database, build = candidate
    retriever = get_retriever(
        database=database, profile=load_profile("example"), chunk_set_id=build.chunk_set_id
    )
    from chatspark.retrieval.pipeline import HybridRetriever

    retriever = HybridRetriever(
        context=retriever.context, profile=retriever.profile, registry=retriever.registry
    )
    if processor:
        retriever.processor = processor
    if context_builder:
        retriever.context_builder = context_builder
    if lane:
        retriever.lanes = {"sparse": lane}
    return retriever


def test_malicious_lane_text_cannot_replace_canonical_content(candidate):
    class Malicious:
        def retrieve(self, query, limit, context):
            return [
                {
                    "evidence_id": "fake",
                    "score": 1,
                    "content": "stolen",
                    "url": "https://bad.invalid",
                }
            ]

    with pytest.raises(EvidenceError, match="invalid evidence"):
        pipeline(candidate, lane=Malicious()).retrieve("registration", 3)


@pytest.mark.parametrize("stage", ["processor", "context_builder"])
def test_extensions_cannot_add_unknown_or_unauthorized_evidence(candidate, stage):
    class Invalid:
        def process(self, query, candidates, context):
            return [RankedEvidence("unknown", 1)]

        build = process

    with pytest.raises(EvidenceError, match="unauthorized"):
        pipeline(candidate, **{stage: Invalid()}).retrieve("registration", 3)


def test_processor_cannot_introduce_another_authorized_candidate(candidate):
    retriever = pipeline(candidate)
    ids = {
        r["chunk_id"]
        for r in EvidenceStore(retriever.context.database).rows(
            corpus_id=retriever.context.corpus_id, chunk_set_id=retriever.context.chunk_set_id
        )
    }
    chosen = retriever.retrieve("registration", 3)[0].chunk_id
    other = (ids - {chosen}).pop()
    retriever.processor = SimpleNamespace(process=lambda *args: [RankedEvidence(other, 2)])
    with pytest.raises(EvidenceError):
        retriever.retrieve("registration", 3)


def test_result_budget_and_deadline(candidate):
    retriever = pipeline(candidate)
    with pytest.raises(EvidenceError):
        retriever.retrieve("registration", 0)
    retriever.context = replace(
        retriever.context, deadline_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(TimeoutError):
        retriever.retrieve("registration", 3)


def test_core_and_installed_private_package_have_no_automatic_effect(candidate):
    retriever = pipeline(candidate)
    assert set(retriever.lanes) == {"sparse"}
    assert all(p["distribution"] == "chatspark-core" for p in retriever.registry.manifest())


def test_filter_narrowing_and_canonical_urls(candidate):
    retriever = pipeline(candidate)
    chunks = retriever.retrieve("registration", 3)
    assert chunks[0].metadata["url"].startswith("https://sources.invalid/")
    assert not retriever.retrieve("registration", 3, {"doc_id": "other"})


def test_provider_receives_requested_limit(candidate):
    retriever = pipeline(candidate)
    original = retriever.lanes["sparse"]

    class Recording:
        def retrieve(self, query, limit, context):
            assert context.requested_limit == limit
            return original.retrieve(query, limit, context)

    retriever.lanes["sparse"] = Recording()
    assert retriever.retrieve("registration", 3)


def test_context_provider_receives_effective_final_limit(candidate):
    retriever = pipeline(candidate)
    retriever.profile = retriever.profile.model_copy(update={"final_k": 1})

    class Recording:
        def build(self, query, candidates, context):
            assert context.requested_limit == 1
            return candidates

    retriever.context_builder = Recording()
    assert len(retriever.retrieve("registration", 3)) == 1


@pytest.mark.parametrize(
    "planned_request",
    [
        {},
        LaneRequest([], "registration", 3),
        LaneRequest("sparse", None, 3),
        LaneRequest("sparse", " ", 3),
        LaneRequest("sparse", "x" * 16001, 3),
        LaneRequest("sparse", "registration", True),
        LaneRequest("sparse", "registration", 3.5),
        LaneRequest("sparse", "registration", 3, weight="1"),
        LaneRequest("sparse", "registration", 3, weight=True),
        LaneRequest("sparse", "registration", 3, weight=float("nan")),
        LaneRequest("sparse", "registration", 3, filters=[]),
        LaneRequest("sparse", "registration", 3, filters={"doc_id": [["nested"]]}),
    ],
)
def test_malformed_plan_rejected_before_lane_execution(candidate, planned_request):
    retriever = pipeline(candidate)
    retriever.planner = SimpleNamespace(plan=lambda *args: [planned_request])

    def forbidden(*args):
        pytest.fail("Invalid request reached retrieval provider")

    retriever.lanes = {"sparse": SimpleNamespace(retrieve=forbidden)}
    with pytest.raises(EvidenceError):
        retriever.retrieve("registration", 3)


@pytest.mark.parametrize("stage", ["planner", "processor", "context_builder"])
def test_late_extension_results_cannot_pass_deadline(candidate, monkeypatch, stage):
    from chatspark.retrieval import pipeline as module

    retriever = pipeline(candidate)
    now = datetime.now(UTC)
    clock = SimpleNamespace(now=lambda tz: now)
    monkeypatch.setattr(module, "datetime", clock)
    retriever.context = replace(retriever.context, deadline_at=now + timedelta(seconds=1))

    def delayed(query, candidates_or_context, context=None):
        clock.now = lambda tz: now + timedelta(seconds=2)
        if stage == "planner":
            return [LaneRequest("sparse", query, 3)]
        return candidates_or_context

    setattr(
        retriever,
        stage,
        SimpleNamespace(
            **{
                "planner": {"plan": delayed},
                "processor": {"process": delayed},
                "context_builder": {"build": delayed},
            }[stage]
        ),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        retriever.retrieve("registration", 3)


def test_boolean_limit_and_invalid_caller_filters_rejected(candidate):
    retriever = pipeline(candidate)
    for limit, filters in [(True, None), (3, []), (3, {"doc_id": {"nested": "value"}})]:
        with pytest.raises(EvidenceError):
            retriever.retrieve("registration", limit, filters)
