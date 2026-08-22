from __future__ import annotations

import argparse
import asyncio
import io
import inspect
import json
import uuid
from collections import deque

import pytest
from pydantic import ValidationError

import app.adapters as adapters
from app.api.v1 import operator_conversations
from app.adapters.ai.deepseek_adapter import DeepSeekAdapter
from app.adapters.ai.disabled_adapter import DisabledAIAdapter
from app.adapters.base import ProviderTurnAdapter
from app.ai.evaluation import (
    EvaluationObservation,
    EvaluationOutcomeClass,
    EvaluationReplay,
    EvaluationRunMetadata,
    EvaluationRunner,
    RecordedProviderCall,
)
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus
from app.ai.live_evaluation import (
    FIRST_LIVE_CANARY_CASE_IDS,
    FIRST_LIVE_CANARY_PROFILES,
    LiveEvaluationBudgetExceeded,
    LiveEvaluationBudgetState,
    LiveEvaluationConfigurationError,
    LiveEvaluationFailureReport,
    LiveEvaluationRunBudget,
    LiveEvaluationSource,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderIdentity,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService, get_ai_turn_service
from app.config import Settings
from app.modules.m6_relance.hooks import generate_relance_hook
from app.schemas.common import Language
from scripts import run_ai_evaluation

_FAKE_SECRET_SENTINEL = "DO_NOT_PERSIST_DEEPSEEK_SECRET_SENTINEL"
_MULTILINGUAL_REPORT_TEXT = (
    "Bonjour, c\u2019est disponible ?\n"
    "Ndeko, ezali disponible ?\n"
    "L\u2019occasion \U0001F600"
)


class _ScriptedAdapter(ProviderTurnAdapter):
    provider_name = "scripted"
    model = "offline-fixture"

    def __init__(
        self,
        results: list[ProviderTurnResult],
        *,
        secret: str | None = None,
    ) -> None:
        self._results = deque(results)
        self._secret = secret
        self.requests: list[ProviderTurnRequest] = []

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("unexpected provider call")
        return self._results.popleft()


def _case(case_id: str):
    return next(
        case for case in get_mbb_evaluation_corpus().cases if case.case_id == case_id
    )


def _text_result(text: str = "Réponse fictive.") -> ProviderTurnResult:
    return ProviderTurnResult(
        text=text,
        finish_reason=ProviderFinishReason.completed,
    )


def _replay_with_final_text(text: str) -> EvaluationReplay:
    return EvaluationReplay(
        metadata=EvaluationRunMetadata(
            corpus_version="mbb-ai-eval-v1",
            provider="offline",
            model="offline-fixture",
            reasoning_profile=ProviderReasoningProfile.minimal,
            policy_version="mbb-ai-policy-v2",
        ),
        observations=(
            EvaluationObservation(
                case_id="product.discovery.vague_need",
                provider_calls=(
                    RecordedProviderCall(result=_text_result(text)),
                ),
                final_outcome=EvaluationOutcomeClass.answer,
            ),
        ),
    )


def _replay_args(replay_path, *, pretty: bool) -> argparse.Namespace:
    return argparse.Namespace(
        replay=replay_path,
        live=False,
        profiles=None,
        case_ids=["product.discovery.vague_need"],
        output=None,
        pretty=pretty,
    )


def _tool_result(
    call_id: str,
    capability_name: str = "search_products",
) -> ProviderTurnResult:
    return ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id=call_id,
                capability_name=capability_name,
                arguments={"query": "air fryer"},
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )


def _budget(**overrides) -> LiveEvaluationRunBudget:
    return LiveEvaluationRunBudget(**overrides)


def _source(
    adapter: ProviderTurnAdapter,
    *,
    budget: LiveEvaluationRunBudget | None = None,
    profile: ProviderReasoningProfile = ProviderReasoningProfile.default,
    clock=None,
) -> LiveEvaluationSource:
    state = LiveEvaluationBudgetState(
        budget or LiveEvaluationRunBudget(),
        **({} if clock is None else {"clock": clock}),
    )
    return LiveEvaluationSource(
        adapter,
        reasoning_profile=profile,
        budget_state=state,
    )


@pytest.mark.asyncio
async def test_provider_turn_selector_isolated_from_legacy_and_reachable_m6(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(adapters.settings, "ai_adapter", "disabled")
    monkeypatch.setattr(adapters.settings, "ai_turn_provider", "deepseek")
    monkeypatch.setattr(adapters.settings, "deepseek_api_key", _FAKE_SECRET_SENTINEL)
    adapters.get_ai_adapter.cache_clear()
    adapters.get_provider_turn_adapter.cache_clear()
    try:
        assert isinstance(adapters.get_ai_adapter(), DisabledAIAdapter)
        assert isinstance(adapters.get_provider_turn_adapter(), DeepSeekAdapter)
        assert get_ai_turn_service()._provider_identity == ProviderIdentity(
            provider="deepseek",
            model=adapters.settings.deepseek_model,
        )
        hook, hook_type = await generate_relance_hook(
            attempt_number=1,
            language=Language.french,
            product_interest="air fryer",
            city="Kinshasa",
            customer_name=None,
        )
        assert hook
        assert hook_type == "reciprocity"
        assert _FAKE_SECRET_SENTINEL not in caplog.text
    finally:
        adapters.get_ai_adapter.cache_clear()
        adapters.get_provider_turn_adapter.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", tuple(ProviderReasoningProfile))
async def test_ai_turn_service_binds_server_owned_reasoning_profile(profile) -> None:
    adapter = _ScriptedAdapter([_text_result()])
    turn = AITurn(
        user_content="Bonjour",
        language="french",
        expected_ownership_version=1,
        conversation_id=uuid.uuid4(),
        reasoning_profile=profile,
    )

    await AITurnService(adapter).generate_finalized(turn)

    assert adapter.requests[0].reasoning_profile == profile


@pytest.mark.asyncio
async def test_ai_turn_service_default_reasoning_profile_is_unchanged() -> None:
    adapter = _ScriptedAdapter([_text_result()])
    await AITurnService(adapter).generate_finalized(
        AITurn(
            user_content="Bonjour",
            language="french",
            expected_ownership_version=1,
            conversation_id=uuid.uuid4(),
        )
    )
    assert adapter.requests[0].reasoning_profile == ProviderReasoningProfile.default


def test_ai_turn_rejects_untyped_reasoning_profile() -> None:
    with pytest.raises(ValueError, match="provider-neutral and typed"):
        AITurn(
            user_content="Bonjour",
            language="french",
            expected_ownership_version=1,
            conversation_id=uuid.uuid4(),
            reasoning_profile="strong",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", tuple(ProviderReasoningProfile))
async def test_live_source_truthfully_binds_every_reasoning_profile(profile) -> None:
    adapter = _ScriptedAdapter([_text_result()])
    await _source(adapter, profile=profile).observe(_case("product.discovery.vague_need"))
    assert adapter.requests[0].reasoning_profile == profile


@pytest.mark.asyncio
async def test_live_source_uses_fictional_fixture_for_provider_continuation() -> None:
    adapter = _ScriptedAdapter(
        [_tool_result("call_search"), _text_result("Le modèle fictif coûte 55 USD.")]
    )
    observation = await _source(adapter).observe(
        _case("product.discovery.budget_usd")
    )

    assert len(adapter.requests) == 2
    assert adapter.requests[0].max_output_tokens == 512
    assert observation.final_outcome == EvaluationOutcomeClass.answer
    assert observation.tool_results[0].output["items"][0]["current_usd_price"] == (
        "55.00"
    )
    assert adapter.requests[1].messages[-1].role == "tool_result"


@pytest.mark.asyncio
async def test_live_source_terminal_handoff_fixture_stops_continuation() -> None:
    adapter = _ScriptedAdapter(
        [_tool_result("call_handoff", "request_human_handoff")]
    )
    observation = await _source(adapter).observe(_case("handoff.explicit_human"))

    assert observation.final_outcome == EvaluationOutcomeClass.handoff
    assert len(adapter.requests) == 1
    assert observation.tool_results[0].status == "success"
    assert observation.provider_calls[-1].result.text is None


@pytest.mark.asyncio
async def test_total_provider_budget_stops_before_another_case_call() -> None:
    adapter = _ScriptedAdapter([_text_result()])
    source = _source(adapter, budget=_budget(max_total_provider_calls=1))
    case = _case("product.discovery.vague_need")

    await source.observe(case)
    with pytest.raises(LiveEvaluationBudgetExceeded, match="total_provider_calls"):
        await source.observe(case)
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_per_case_provider_budget_stops_before_continuation() -> None:
    adapter = _ScriptedAdapter([_tool_result("call_search")])
    source = _source(adapter, budget=_budget(max_provider_calls_per_case=1))

    with pytest.raises(LiveEvaluationBudgetExceeded, match="provider_calls_per_case"):
        await source.observe(_case("product.discovery.budget_usd"))
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_tool_round_budget_stops_before_second_round_execution() -> None:
    adapter = _ScriptedAdapter(
        [_tool_result("call_1"), _tool_result("call_2")],
        secret=_FAKE_SECRET_SENTINEL,
    )

    with pytest.raises(
        LiveEvaluationBudgetExceeded,
        match="tool_rounds_per_case",
    ) as captured:
        await _source(adapter).observe(_case("product.discovery.budget_usd"))
    assert len(adapter.requests) == 2
    evidence = captured.value.evidence
    assert evidence is not None
    assert evidence.case_id == "product.discovery.budget_usd"
    assert evidence.completed_provider_calls == 2
    assert evidence.completed_tool_rounds == 1
    assert evidence.completed_capability_executions == 1
    assert evidence.first_tool_call is not None
    assert evidence.first_tool_call.capability_name == "search_products"
    assert evidence.first_tool_call.arguments == {"query": "air fryer"}
    assert evidence.first_tool_result is not None
    assert evidence.first_tool_result.status == "success"
    assert evidence.first_tool_result.output is not None
    assert evidence.first_tool_result.output["items"][0]["current_usd_price"] == "55.00"
    assert evidence.rejected_tool_calls[0].capability_name == "search_products"
    assert evidence.rejected_tool_calls[0].arguments == {"query": "air fryer"}
    assert evidence.exceeded_budget == "tool_rounds_per_case"
    assert evidence.configured_limit == 1

    serialized = run_ai_evaluation._serialize_report(
        LiveEvaluationFailureReport(failure=evidence),
        pretty=True,
    )
    report = json.loads(serialized)
    assert report["status"] == "failed"
    assert report["failure"]["case_id"] == "product.discovery.budget_usd"
    assert _FAKE_SECRET_SENTINEL not in serialized


@pytest.mark.asyncio
async def test_capability_budget_stops_before_any_fixture_execution() -> None:
    result = ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id="call_1",
                capability_name="search_products",
                arguments={"query": "air fryer"},
            ),
            ProviderToolCall(
                call_id="call_2",
                capability_name="search_products",
                arguments={"query": "air fryer"},
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )
    adapter = _ScriptedAdapter([result])

    with pytest.raises(
        LiveEvaluationBudgetExceeded,
        match="capability_executions_per_case",
    ):
        await _source(adapter).observe(_case("product.discovery.budget_usd"))
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_wall_clock_budget_stops_before_transport() -> None:
    now = 0.0

    def _clock() -> float:
        return now

    adapter = _ScriptedAdapter([_text_result()])
    source = _source(adapter, clock=_clock)
    now = 2701.0

    with pytest.raises(LiveEvaluationBudgetExceeded, match="wall_clock"):
        await source.observe(_case("product.discovery.vague_need"))
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_http_timeout_cancels_without_retry() -> None:
    class _BlockingAdapter(ProviderTurnAdapter):
        provider_name = "scripted"
        model = "offline-fixture"

        def __init__(self) -> None:
            self.calls = 0

        async def generate_turn(
            self,
            _request: ProviderTurnRequest,
        ) -> ProviderTurnResult:
            self.calls += 1
            await asyncio.sleep(10)
            raise AssertionError("timeout did not cancel provider call")

    adapter = _BlockingAdapter()
    with pytest.raises(LiveEvaluationBudgetExceeded, match="http_timeout"):
        await _source(
            adapter,
            budget=_budget(http_timeout_seconds=1),
        ).observe(_case("product.discovery.vague_need"))
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_case_budget_preflight_stops_before_partial_runner_activity() -> None:
    adapter = _ScriptedAdapter([_text_result()])
    budget = _budget(max_case_executions=1)
    source = _source(adapter, budget=budget)
    metadata = run_ai_evaluation.EvaluationRunMetadata(
        corpus_version="mbb-ai-eval-v1",
        provider="scripted",
        model="offline-fixture",
        reasoning_profile=ProviderReasoningProfile.default,
        policy_version="mbb-ai-policy-v2",
    )

    with pytest.raises(LiveEvaluationBudgetExceeded, match="case_executions"):
        await EvaluationRunner(source, metadata).run(
            get_mbb_evaluation_corpus(),
            case_ids=("product.discovery.vague_need", "evidence.contradictory_price"),
        )
    assert adapter.requests == []


def _live_args(*profiles: str) -> argparse.Namespace:
    return argparse.Namespace(
        replay=None,
        live=True,
        profiles=list(profiles),
        case_ids=None,
        output=None,
        pretty=False,
    )


def _safe_live_settings(*, api_key: str) -> Settings:
    return Settings(
        _env_file=None,
        ai_adapter="disabled",
        ai_turn_provider="deepseek",
        deepseek_api_key=api_key,
        whatsapp_send_enabled=False,
        crm_send_enabled=False,
        payment_send_enabled=False,
        relance_enabled=False,
        scheduled_tasks_enabled=False,
        m1_maps_fanout_enabled=False,
    )


def test_settings_bind_provider_turn_selector_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_ADAPTER", "disabled")
    monkeypatch.setenv("AI_TURN_PROVIDER", "deepseek")
    settings = Settings(_env_file=None)
    assert settings.ai_adapter == "disabled"
    assert settings.ai_turn_provider == "deepseek"


@pytest.mark.asyncio
async def test_missing_live_key_fails_before_adapter_construction() -> None:
    factory_calls = 0

    def _factory() -> ProviderTurnAdapter:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("adapter must not be constructed")

    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="provider_credentials_unavailable",
    ):
        await run_ai_evaluation._run_live(
            _live_args("minimal"),
            configured_settings=_safe_live_settings(api_key=""),
            adapter_factory=_factory,
            environment={},
        )
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_live_key_must_come_from_process_environment() -> None:
    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="provider_credentials_unavailable",
    ):
        await run_ai_evaluation._run_live(
            _live_args("minimal"),
            configured_settings=_safe_live_settings(
                api_key=_FAKE_SECRET_SENTINEL
            ),
            adapter_factory=lambda: _ScriptedAdapter([_text_result()]),
            environment={},
        )


@pytest.mark.asyncio
async def test_external_effect_gate_failure_precedes_adapter_construction() -> None:
    factory_calls = 0

    def _factory() -> ProviderTurnAdapter:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("adapter must not be constructed")

    settings = _safe_live_settings(api_key=_FAKE_SECRET_SENTINEL)
    settings.m1_maps_fanout_enabled = True
    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="external_effect_gates_not_disabled",
    ):
        await run_ai_evaluation._run_live(
            _live_args("minimal"),
            configured_settings=settings,
            adapter_factory=_factory,
            environment={"DEEPSEEK_API_KEY": _FAKE_SECRET_SENTINEL},
        )
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_live_matrix_never_serializes_or_logs_ephemeral_secret(caplog) -> None:
    adapter = _ScriptedAdapter(
        [_text_result(_MULTILINGUAL_REPORT_TEXT) for _ in FIRST_LIVE_CANARY_CASE_IDS],
        secret=_FAKE_SECRET_SENTINEL,
    )
    output = await run_ai_evaluation._run_live(
        _live_args("minimal"),
        configured_settings=_safe_live_settings(api_key=_FAKE_SECRET_SENTINEL),
        adapter_factory=lambda: adapter,
        environment={"DEEPSEEK_API_KEY": _FAKE_SECRET_SENTINEL},
    )

    assert _FAKE_SECRET_SENTINEL not in output
    assert _FAKE_SECRET_SENTINEL not in caplog.text
    assert output.isascii()
    assert json.loads(output)["reports"][0]["case_results"][0]["final_text"] == (
        _MULTILINGUAL_REPORT_TEXT
    )
    assert len(adapter.requests) == len(FIRST_LIVE_CANARY_CASE_IDS)


@pytest.mark.asyncio
async def test_live_matrix_binds_all_four_profiles_to_actual_requests() -> None:
    adapter = _ScriptedAdapter(
        [
            _text_result()
            for _ in range(
                len(FIRST_LIVE_CANARY_CASE_IDS)
                * len(FIRST_LIVE_CANARY_PROFILES)
            )
        ]
    )
    output = await run_ai_evaluation._run_live(
        _live_args(*(profile.value for profile in FIRST_LIVE_CANARY_PROFILES)),
        configured_settings=_safe_live_settings(
            api_key=_FAKE_SECRET_SENTINEL
        ),
        adapter_factory=lambda: adapter,
        environment={"DEEPSEEK_API_KEY": _FAKE_SECRET_SENTINEL},
    )

    report = json.loads(output)
    assert report["reasoning_profiles"] == [
        profile.value for profile in FIRST_LIVE_CANARY_PROFILES
    ]
    assert len(adapter.requests) == 20
    for index, profile in enumerate(FIRST_LIVE_CANARY_PROFILES):
        start = index * len(FIRST_LIVE_CANARY_CASE_IDS)
        stop = start + len(FIRST_LIVE_CANARY_CASE_IDS)
        assert {
            request.reasoning_profile for request in adapter.requests[start:stop]
        } == {profile}


@pytest.mark.asyncio
@pytest.mark.parametrize("pretty", (False, True))
async def test_replay_report_serialization_preserves_multilingual_content(
    tmp_path,
    pretty: bool,
) -> None:
    replay_path = tmp_path / "unicode-replay.json"
    replay_path.write_text(
        _replay_with_final_text(_MULTILINGUAL_REPORT_TEXT).model_dump_json(),
        encoding="utf-8",
    )

    output = await run_ai_evaluation._run_replay(
        _replay_args(replay_path, pretty=pretty)
    )

    assert output.isascii()
    assert ("\n" in output) is pretty
    assert json.loads(output)["case_results"][0]["final_text"] == (
        _MULTILINGUAL_REPORT_TEXT
    )


@pytest.mark.parametrize("pretty", (False, True))
def test_cli_emits_multilingual_replay_to_cp1252_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    pretty: bool,
) -> None:
    replay_path = tmp_path / "unicode-replay.json"
    replay_path.write_text(
        _replay_with_final_text(_MULTILINGUAL_REPORT_TEXT).model_dump_json(),
        encoding="utf-8",
    )
    output_bytes = io.BytesIO()
    output_stream = io.TextIOWrapper(
        output_bytes,
        encoding="cp1252",
        errors="strict",
    )

    monkeypatch.setattr(
        run_ai_evaluation,
        "build_parser",
        lambda: type("Parser", (), {"parse_args": lambda _self: _replay_args(
            replay_path,
            pretty=pretty,
        )})(),
    )
    monkeypatch.setattr(run_ai_evaluation.sys, "stdout", output_stream)

    assert run_ai_evaluation.main() == 0
    output_stream.flush()
    emitted = output_bytes.getvalue().decode("cp1252")
    assert emitted.isascii()
    assert json.loads(emitted)["case_results"][0]["final_text"] == (
        _MULTILINGUAL_REPORT_TEXT
    )


def test_replay_report_serialization_preserves_ascii_output(tmp_path) -> None:
    replay_path = tmp_path / "ascii-replay.json"
    replay_path.write_text(
        _replay_with_final_text("ASCII evaluation response.").model_dump_json(),
        encoding="utf-8",
    )

    output = asyncio.run(
        run_ai_evaluation._run_replay(_replay_args(replay_path, pretty=False))
    )

    assert "\\u" not in output
    assert json.loads(output)["case_results"][0]["final_text"] == (
        "ASCII evaluation response."
    )


def test_cli_error_reporting_redacts_arbitrary_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Parser:
        def parse_args(self):
            return _live_args("minimal")

    async def _fail(_args):
        raise RuntimeError(_FAKE_SECRET_SENTINEL)

    monkeypatch.setattr(run_ai_evaluation, "build_parser", lambda: _Parser())
    monkeypatch.setattr(run_ai_evaluation, "_run", _fail)

    assert run_ai_evaluation.main() == 1
    captured = capsys.readouterr()
    assert _FAKE_SECRET_SENTINEL not in captured.out
    assert _FAKE_SECRET_SENTINEL not in captured.err
    assert "RuntimeError" in captured.err


def test_cli_writes_partial_failure_report_before_returning_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    output_path = tmp_path / "partial-failure.json"
    args = _live_args("default")
    args.output = output_path
    args.pretty = True

    class _Parser:
        def parse_args(self):
            return args

    async def _fail(_args):
        adapter = _ScriptedAdapter(
            [_tool_result("call_1"), _tool_result("call_2")],
            secret=_FAKE_SECRET_SENTINEL,
        )
        await _source(adapter).observe(_case("product.discovery.budget_usd"))

    monkeypatch.setattr(run_ai_evaluation, "build_parser", lambda: _Parser())
    monkeypatch.setattr(run_ai_evaluation, "_run", _fail)

    assert run_ai_evaluation.main() == 1
    report_text = output_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["status"] == "failed"
    assert report["failure"]["case_id"] == "product.discovery.budget_usd"
    assert report["failure"]["completed_provider_calls"] == 2
    assert report["failure"]["rejected_tool_calls"][0]["call_id"] == "call_2"
    assert _FAKE_SECRET_SENTINEL not in report_text
    assert "tool_rounds_per_case" in capsys.readouterr().err


def test_default_cli_requires_explicit_replay_or_live_mode() -> None:
    parser = run_ai_evaluation.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_first_live_canary_and_hard_budget_are_frozen() -> None:
    assert FIRST_LIVE_CANARY_CASE_IDS == (
        "product.discovery.budget_usd",
        "product.discovery.normal",
        "handoff.explicit_human",
        "injection.pretend_stock",
        "language.french_lingala",
    )
    assert FIRST_LIVE_CANARY_PROFILES == (
        ProviderReasoningProfile.minimal,
        ProviderReasoningProfile.standard,
        ProviderReasoningProfile.strong,
        ProviderReasoningProfile.default,
    )
    assert len(get_mbb_evaluation_corpus().cases) == 24
    assert LiveEvaluationRunBudget().model_dump() == {
        "max_case_executions": 20,
        "max_total_provider_calls": 40,
        "max_provider_calls_per_case": 2,
        "max_tool_rounds_per_case": 1,
        "max_capability_executions_per_case": 1,
        "max_output_tokens_per_call": 512,
        "wall_clock_seconds": 2700,
        "http_timeout_seconds": 60,
        "transport_retries": 0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_case_executions", 21),
        ("max_total_provider_calls", 41),
        ("max_provider_calls_per_case", 3),
        ("max_tool_rounds_per_case", 2),
        ("max_capability_executions_per_case", 2),
        ("max_output_tokens_per_call", 513),
        ("wall_clock_seconds", 2701),
        ("http_timeout_seconds", 61),
        ("transport_retries", 1),
    ),
)
def test_live_budget_cannot_exceed_first_canary_ceiling(field, value) -> None:
    with pytest.raises(ValidationError):
        LiveEvaluationRunBudget(**{field: value})


def test_return_to_ai_eligibility_uses_provider_turn_selector() -> None:
    source = inspect.getsource(
        operator_conversations.change_operator_conversation_ownership
    )
    assert "ai_adapter=settings.ai_turn_provider" in source
    assert "ai_adapter=settings.ai_adapter" not in source
