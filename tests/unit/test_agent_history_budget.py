"""Synthetic whole-body tests for history_budget.

Fake native prepare builds real PreparedComplete and real PromptTokenCount
with request_digest. No actual tokenizer or provider capacity claim is made.
"""

import asyncio
import hashlib
import json

import pytest

from packages.providers.completion_budget import PreparedComplete
from packages.providers.prompt_budget import PromptCountUnavailable, PromptTokenCount
from packages.providers.text import ProviderCleanupError
from services.agent.history_budget import (
    AgentContextOverflow,
    AgentContextStale,
    AgentContextUnavailable,
    HistorySelection,
    select_history,
)

BINDING = "ab" * 32
CONTEXT = 100000
RESERVED = 128


def _digest(messages):
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()


def _make_count(*, fits, digest, binding=BINDING, context=CONTEXT, reserved=RESERVED):
    return PromptTokenCount(
        input_tokens=context - reserved if fits else context - reserved + 1,
        context_tokens=context,
        reserved_output_tokens=reserved,
        binding_sha256=binding,
        request_sha256=digest,
    )


def _gen_never():
    async def _g():
        raise AssertionError("selector must not generate")

    return _g


def _base():
    return [
        {"role": "system", "content": "trusted instructions v1"},
        {"role": "user", "content": "current objective: fix file"},
    ]


def _ex(tag):
    return [
        {
            "role": "assistant",
            "content": json.dumps({"op": "read", "tag": tag}, separators=(",", ":")),
        },
        {"role": "user", "content": f"result {tag}"},
    ]


async def _ok_check():
    return None


def _always_fits(record=None, binding=BINDING):
    async def _p(messages):
        if record is not None:
            record.append([dict(m) for m in messages])
        d = _digest(messages)
        return PreparedComplete(_make_count(fits=True, digest=d, binding=binding), _gen_never())

    return _p


def test_full_history_fits_keeps_all_exact_order():
    base = _base()
    exs = [_ex("a"), _ex("b")]
    sel = asyncio.run(select_history(base, exs, _always_fits(), _ok_check))
    assert isinstance(sel, HistorySelection)
    assert sel.kept_exchanges == 2 and sel.dropped_exchanges == 0
    assert sel.measurement_attempts == 1
    assert sel.messages == base + exs[0] + exs[1]
    assert sel.messages is not base and sel.messages[0] is not base[0]


def test_tail_over_2000_preserved():
    tail = "x" * 2500
    base = _base()
    exs = [[{"role": "assistant", "content": "do read"}, {"role": "user", "content": tail}]]
    sel = asyncio.run(select_history(base, exs, _always_fits(), _ok_check))
    assert sel.messages[-1]["content"] == tail
    assert len(sel.messages[-1]["content"]) > 2000


def test_unicode_and_json_exact():
    content = '{"action":"edit","path":"caf\u00e9/\U0001f600"}\nline2\t"quoted"'
    base = _base()
    exs = [
        [{"role": "assistant", "content": content}, {"role": "user", "content": "ok \u6f22\u5b57"}]
    ]
    before = json.dumps(base + exs[0], ensure_ascii=False)
    sel = asyncio.run(select_history(base, exs, _always_fits(), _ok_check))
    assert sel.messages[2]["content"] == content
    assert json.loads(json.dumps(sel.messages, ensure_ascii=False))[2]["content"] == content
    assert before == json.dumps(base + exs[0], ensure_ascii=False)


def test_more_than_eight_messages_retained_when_fit():
    base = _base()
    exs = [_ex(str(i)) for i in range(5)]
    sel = asyncio.run(select_history(base, exs, _always_fits(), _ok_check))
    assert len(sel.messages) == 2 + 10
    assert sel.kept_exchanges == 5


def test_whole_pair_eviction_drops_exactly_one_oldest():
    base = _base()
    exs = [_ex("0"), _ex("1"), _ex("2")]

    async def _p(messages):
        fits = len(messages) <= 2 + 4
        return PreparedComplete(_make_count(fits=fits, digest=_digest(messages)), _gen_never())

    sel = asyncio.run(select_history(base, exs, _p, _ok_check))
    assert sel.kept_exchanges == 2 and sel.dropped_exchanges == 1
    assert sel.measurement_attempts == 2
    assert sel.messages == base + exs[1] + exs[2]


def test_protected_overflow_base_only():
    async def _p(messages):
        return PreparedComplete(_make_count(fits=False, digest=_digest(messages)), _gen_never())

    with pytest.raises(AgentContextOverflow):
        asyncio.run(select_history(_base(), [], _p, _ok_check))


def test_protected_overflow_latest():
    async def _p(messages):
        return PreparedComplete(_make_count(fits=False, digest=_digest(messages)), _gen_never())

    with pytest.raises(AgentContextOverflow):
        asyncio.run(select_history(_base(), [_ex("only")], _p, _ok_check))


def test_lone_user_notice_kept():
    base = _base()
    exs = [[{"role": "user", "content": "native steering notice"}]]
    sel = asyncio.run(select_history(base, exs, _always_fits(), _ok_check))
    assert sel.kept_exchanges == 1
    assert sel.messages[-1]["content"] == "native steering notice"


def test_byte_limit_refuses_huge_without_prepare():
    called = []

    async def _p(messages):
        called.append(1)
        return PreparedComplete(_make_count(fits=True, digest=_digest(messages)), _gen_never())

    big = "a" * (17 * 1024 * 1024)
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": big}]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(base, [], _p, _ok_check))
    assert called == []


def test_byte_limit_counts_json_escaping():
    called = []

    async def _p(messages):
        called.append(1)
        return PreparedComplete(_make_count(fits=True, digest=_digest(messages)), _gen_never())

    quote = '"' * (9 * 1024 * 1024)
    base = [{"role": "system", "content": "s"}, {"role": "user", "content": quote}]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(base, [], _p, _ok_check))
    assert called == []


def test_512_exchange_limit():
    async def _p(messages):
        return PreparedComplete(_make_count(fits=True, digest=_digest(messages)), _gen_never())

    exs = [[{"role": "user", "content": "n"}] for _ in range(513)]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), exs, _p, _ok_check))


def test_malformed_roles_rejected():
    bad_base = [{"role": "system", "content": "s"}, {"role": "tool", "content": "x"}]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(bad_base, [], _always_fits(), _ok_check))
    bad_ex = [[{"role": "system", "content": "s"}]]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), bad_ex, _always_fits(), _ok_check))


def test_field_types_rejected():
    for bad in [
        [{"role": "system", "content": "s"}, {"role": "user"}],
        [{"role": "system", "content": "s"}, {"role": "user", "content": "x", "extra": 1}],
        [{"role": "system", "content": "s"}, {"role": "user", "content": 123}],
        [{"role": "system", "content": "s"}],
    ]:
        with pytest.raises(AgentContextUnavailable):
            asyncio.run(select_history(bad, [], _always_fits(), _ok_check))
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [[]], _always_fits(), _ok_check))


def test_surrogate_rejected():
    bad = [{"role": "system", "content": "s"}, {"role": "user", "content": "\ud800"}]
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(bad, [], _always_fits(), _ok_check))


def test_bool_and_range_max_rejected():
    for v in [True, False, 0, 514, "256", 2.5, None]:
        with pytest.raises(AgentContextUnavailable):
            asyncio.run(select_history(_base(), [], _always_fits(), _ok_check, max_measurements=v))


def test_unsupported_prepare_before_inference():
    async def _never_check():
        raise AssertionError("check must not run")

    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [], None, _never_check))
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [], "not-callable", _never_check))


def test_constant_non_payload_error():
    with pytest.raises(AgentContextUnavailable) as e1:
        asyncio.run(
            select_history([{"role": "system", "content": "s"}], [], _always_fits(), _ok_check)
        )
    with pytest.raises(AgentContextUnavailable) as e2:
        asyncio.run(
            select_history(
                _base(), [[{"role": "system", "content": "secret-xyz"}]], _always_fits(), _ok_check
            )
        )
    assert str(e1.value) == str(e2.value)
    assert "secret-xyz" not in str(e2.value)


def test_caller_mutation_during_pre_check_is_stale():
    base = _base()
    exs = [_ex("a")]

    async def _mut_check():
        base.append({"role": "user", "content": "injected"})

    with pytest.raises(AgentContextStale):
        asyncio.run(select_history(base, exs, _always_fits(), _mut_check))


def test_preparer_mutation_is_stale_and_discarded():
    created = []

    async def _p(messages):
        messages[0]["content"] = "tampered"
        c = _make_count(fits=True, digest=_digest(messages))
        p = PreparedComplete(c, _gen_never())
        created.append(p)
        return p

    with pytest.raises(AgentContextStale):
        asyncio.run(select_history(_base(), [_ex("a")], _p, _ok_check))
    assert len(created) == 1

    async def _use():
        await created[0].complete()

    with pytest.raises(PromptCountUnavailable):
        asyncio.run(_use())


def test_changed_binding_is_stale():
    n = {"i": 0}

    async def _p(messages):
        n["i"] += 1
        if n["i"] == 1:
            c = _make_count(fits=False, digest=_digest(messages), binding="aa" * 32)
        else:
            c = _make_count(fits=True, digest=_digest(messages), binding="bb" * 32)
        return PreparedComplete(c, _gen_never())

    with pytest.raises(AgentContextStale):
        asyncio.run(select_history(_base(), [_ex("a"), _ex("b")], _p, _ok_check))


def test_changed_context_and_reserved_are_stale():
    for kw in [{"context": CONTEXT + 1}, {"reserved": RESERVED + 1}]:
        n = {"i": 0}

        async def _p(messages, _kw=kw, _n=n):
            _n["i"] += 1
            ctx = CONTEXT if _n["i"] == 1 else _kw.get("context", CONTEXT)
            res = RESERVED if _n["i"] == 1 else _kw.get("reserved", RESERVED)
            fits = _n["i"] > 1
            c = _make_count(fits=fits, digest=_digest(messages), context=ctx, reserved=res)
            return PreparedComplete(c, _gen_never())

        with pytest.raises(AgentContextStale):
            asyncio.run(select_history(_base(), [_ex("a"), _ex("b")], _p, _ok_check))


def test_request_digest_may_differ():
    async def _p(messages):
        return PreparedComplete(
            _make_count(fits=len(messages) <= 4, digest=_digest(messages)), _gen_never()
        )

    sel = asyncio.run(select_history(_base(), [_ex("a"), _ex("b")], _p, _ok_check))
    assert sel.dropped_exchanges == 1


def test_budget_exhausted():
    calls = []

    async def _p(messages):
        calls.append(1)
        return PreparedComplete(_make_count(fits=False, digest=_digest(messages)), _gen_never())

    with pytest.raises(AgentContextUnavailable):
        asyncio.run(
            select_history(_base(), [_ex("a"), _ex("b")], _p, _ok_check, max_measurements=1)
        )
    assert len(calls) == 1


def test_cancellation_preserved():
    async def _p(messages):
        raise asyncio.CancelledError()

    try:
        asyncio.run(select_history(_base(), [_ex("a")], _p, _ok_check))
        raise AssertionError("must cancel")
    except asyncio.CancelledError:
        pass


def test_check_failure_discards_and_propagates():
    created = []

    async def _p(messages):
        c = _make_count(fits=True, digest=_digest(messages))
        p = PreparedComplete(c, _gen_never())
        created.append(p)
        return p

    checks = 0

    async def _bad_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ProviderCleanupError("steer")

    with pytest.raises(ProviderCleanupError):
        asyncio.run(select_history(_base(), [_ex("a")], _p, _bad_check))

    async def _use():
        await created[0].complete()

    with pytest.raises(PromptCountUnavailable):
        asyncio.run(_use())


def test_preparer_wrong_types():
    for bad in [None, "x", 123, {"a": 1}]:

        async def _p(messages, _bad=bad):
            return _bad

        with pytest.raises(AgentContextUnavailable):
            asyncio.run(select_history(_base(), [_ex("a")], _p, _ok_check))


def test_count_failure_not_retried():
    calls = []

    async def _p(messages):
        calls.append(1)
        raise PromptCountUnavailable("no count")

    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [_ex("a")], _p, _ok_check))
    assert calls == [1]


def test_concurrent_invocations_isolated():
    async def _main():
        async def _pa(messages):
            return PreparedComplete(
                _make_count(fits=True, digest=_digest(messages), binding="aa" * 32), _gen_never()
            )

        async def _pb(messages):
            return PreparedComplete(
                _make_count(fits=True, digest=_digest(messages), binding="bb" * 32), _gen_never()
            )

        async def _c():
            return None

        r1, r2 = await asyncio.gather(
            select_history(_base(), [_ex("a")], _pa, _c),
            select_history(_base(), [_ex("b"), _ex("c")], _pb, _c),
        )
        assert r1.kept_exchanges == 1 and r2.kept_exchanges == 2
        assert r1.messages[-1]["content"] != r2.messages[-1]["content"]

    asyncio.run(_main())


def test_fixture_path_keeps_full():
    async def _p(messages):
        async def _g():
            return "fixture-output"

        return PreparedComplete(None, _g, fixture=True)

    base = _base()
    exs = [_ex(str(i)) for i in range(4)]
    sel = asyncio.run(select_history(base, exs, _p, _ok_check))
    assert sel.kept_exchanges == 4 and sel.dropped_exchanges == 0
    assert sel.measurement_attempts == 1
    assert sel.prepared.fixture is True
    assert sel.prepared.measurement is None
    assert sel.messages == base + exs[0] + exs[1] + exs[2] + exs[3]


def test_exact_fit_uses_computed_count_and_single_use_generation():
    generated = []
    prepared = []

    async def prepare(messages):
        count = _make_count(fits=True, digest=_digest(messages))
        assert count.input_tokens + count.reserved_output_tokens == count.context_tokens

        async def generate():
            generated.append(1)
            return "native completion"

        result = PreparedComplete(count, generate)
        prepared.append(result)
        return result

    async def main():
        selection = await select_history(_base(), [_ex("latest")], prepare, _ok_check)
        assert selection.prepared is prepared[0]
        assert generated == []
        assert await selection.prepared.complete() == "native completion"
        with pytest.raises(PromptCountUnavailable):
            await selection.prepared.complete()

    asyncio.run(main())
    assert generated == [1]


@pytest.mark.parametrize("error_type", [ValueError, asyncio.CancelledError, ProviderCleanupError])
def test_generation_failure_is_consumed_without_retry(error_type):
    calls, generations = [], []

    async def prepare(messages):
        calls.append(1)

        async def generate():
            generations.append(1)
            raise error_type("synthetic failure")

        return PreparedComplete(_make_count(fits=True, digest=_digest(messages)), generate)

    async def main():
        selection = await select_history(_base(), [_ex("latest")], prepare, _ok_check)
        with pytest.raises(error_type):
            await selection.prepared.complete()
        with pytest.raises(PromptCountUnavailable):
            await selection.prepared.complete()

    asyncio.run(main())
    assert calls == generations == [1]


@pytest.mark.parametrize("point", ["pre1", "prepare1", "post1", "pre2", "prepare2", "post2"])
@pytest.mark.parametrize("source", ["base", "exchange", "detached"])
def test_mutation_at_each_selection_await_is_stale_and_all_callbacks_discarded(point, source):
    base, exchanges = _base(), [_ex("old"), _ex("latest")]
    created, measured, check_calls = [], [], 0

    def mutate(detached=None):
        if source == "base":
            base[-1]["content"] += " changed"
        elif source == "exchange":
            exchanges[-1][-1]["content"] += " changed"
        elif detached is not None:
            detached[-1]["content"] += " changed"
        else:
            # Before prepare there is no detached list available to a native
            # check. Mutate the caller exchange at that boundary instead.
            exchanges[-1][-1]["content"] += " changed"

    async def prepare(messages):
        measured.append(messages)
        await asyncio.sleep(0)
        candidate = PreparedComplete(
            _make_count(fits=len(measured) == 2, digest=_digest(messages)), _gen_never()
        )
        created.append(candidate)
        if point == f"prepare{len(measured)}":
            mutate(messages)
        return candidate

    async def check():
        nonlocal check_calls
        check_calls += 1
        await asyncio.sleep(0)
        name = {1: "pre1", 2: "post1", 3: "pre2", 4: "post2"}[check_calls]
        if point == name:
            mutate(measured[-1] if measured and not name.startswith("pre") else None)

    async def main():
        with pytest.raises(AgentContextStale):
            await select_history(base, exchanges, prepare, check)
        for candidate in created:
            with pytest.raises(PromptCountUnavailable):
                await candidate.complete()

    asyncio.run(main())
    assert len(measured) <= 2


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, ProviderCleanupError])
def test_post_prepare_failure_keeps_real_type_and_discards_owned_callback(error_type):
    created, checks = [], 0

    async def prepare(messages):
        candidate = PreparedComplete(_make_count(fits=True, digest=_digest(messages)), _gen_never())
        created.append(candidate)
        return candidate

    async def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise error_type("native fence")

    async def main():
        with pytest.raises(error_type):
            await select_history(_base(), [_ex("latest")], prepare, check)
        assert len(created) == 1
        with pytest.raises(PromptCountUnavailable):
            await created[0].complete()

    asyncio.run(main())


@pytest.mark.parametrize("error_type", [ProviderCleanupError, ValueError])
def test_failed_preparation_is_not_retried_or_name_matched(error_type):
    calls = []

    async def prepare(messages):
        calls.append(1)
        raise error_type("native failure")

    with pytest.raises(error_type):
        asyncio.run(select_history(_base(), [_ex("old"), _ex("new")], prepare, _ok_check))
    assert calls == [1]


def test_count_record_replaced_during_native_check_is_stale():
    candidate = None
    checks = 0

    async def prepare(messages):
        nonlocal candidate
        candidate = PreparedComplete(_make_count(fits=True, digest=_digest(messages)), _gen_never())
        return candidate

    async def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            candidate._measurement = _make_count(fits=True, digest="cd" * 32)

    async def main():
        with pytest.raises(AgentContextStale):
            await select_history(_base(), [_ex("latest")], prepare, check)
        with pytest.raises(PromptCountUnavailable):
            await candidate.complete()

    asyncio.run(main())


def test_all_attempts_are_complete_suffixes_and_rejected_callbacks_are_discarded():
    base, exchanges = _base(), [_ex(str(i)) for i in range(4)]
    measured, candidates = [], []

    async def prepare(messages):
        measured.append([dict(message) for message in messages])
        # Synthetic complete-request counts need not vary monotonically.
        input_count = [CONTEXT + 70, CONTEXT + 200, CONTEXT - RESERVED][len(measured) - 1]
        count = PromptTokenCount(input_count, CONTEXT, RESERVED, _digest(messages), BINDING)
        result = PreparedComplete(count, _gen_never())
        candidates.append(result)
        return result

    async def main():
        selection = await select_history(base, exchanges, prepare, _ok_check)
        assert (
            selection.kept_exchanges,
            selection.dropped_exchanges,
            selection.measurement_attempts,
        ) == (2, 2, 3)
        for i, messages in enumerate(measured):
            assert messages == base + [
                message for exchange in exchanges[i:] for message in exchange
            ]
        assert selection.messages == measured[-1]
        assert len({c.measurement.request_sha256 for c in candidates}) == 3
        for rejected in candidates[:-1]:
            with pytest.raises(PromptCountUnavailable):
                await rejected.complete()

    asyncio.run(main())


@pytest.mark.parametrize("text", ["plain", "é漢😀", '"\\\n\t\x00\x01\b\f\r'])
def test_native_byte_limit_is_exact_for_multibyte_and_escaped_json(monkeypatch, text):
    from services.agent import history_budget

    base = [{"role": "system", "content": text}, {"role": "user", "content": text}]
    exchange = [[{"role": "user", "content": text}]]
    exact = len(json.dumps(base + exchange[0], ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(history_budget, "_MAX_BYTES", exact)
    measured = []
    selection = asyncio.run(select_history(base, exchange, _always_fits(measured), _ok_check))
    assert selection.messages == base + exchange[0]
    assert len(measured) == 1
    base[-1]["content"] += "a"
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(base, exchange, _always_fits(measured), _ok_check))
    assert len(measured) == 1


@pytest.mark.parametrize("bad", ["\ud800", "\udfff", "valid then \ud834\udd1e"])
def test_invalid_unicode_in_exchange_is_refused_before_prepare(bad):
    measured = []
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(
            select_history(
                _base(), [[{"role": "user", "content": bad}]], _always_fits(measured), _ok_check
            )
        )
    assert measured == []


def test_hostile_container_subclasses_are_rejected_without_dispatching_methods():
    class HostileDict(dict):
        def keys(self):
            raise AssertionError("must not inspect custom mapping")

    class HostileList(list):
        def __iter__(self):
            raise AssertionError("must not iterate custom list")

    for base, exchanges in [
        (HostileList(_base()), []),
        (_base(), HostileList([])),
        (_base(), [HostileList(_ex("x"))]),
        (_base(), [[HostileDict(role="user", content="x")]]),
    ]:
        with pytest.raises(AgentContextUnavailable):
            asyncio.run(select_history(base, exchanges, _always_fits(), _ok_check))


def test_wrong_prepared_object_cannot_trigger_duck_typed_cleanup():
    class NotPrepared:
        def discard(self):
            raise AssertionError("selector must not invoke arbitrary cleanup")

    async def prepare(messages):
        return NotPrepared()

    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [_ex("latest")], prepare, _ok_check))


def test_counting_capability_failure_has_no_fixture_or_generation_fallback():
    calls = []

    async def prepare(messages):
        calls.append(1)
        raise PromptCountUnavailable("unsupported complete-count capability")

    with pytest.raises(AgentContextUnavailable, match="^Agent history context is unavailable$"):
        asyncio.run(select_history(_base(), [_ex("latest")], prepare, _ok_check))
    assert calls == [1]


def test_fixture_cannot_replace_counted_overflow_candidate():
    calls, created = [], []

    async def prepare(messages):
        calls.append(1)
        candidate = (
            PreparedComplete(_make_count(fits=False, digest=_digest(messages)), _gen_never())
            if len(calls) == 1
            else PreparedComplete(None, _gen_never(), fixture=True)
        )
        created.append(candidate)
        return candidate

    async def main():
        with pytest.raises(AgentContextStale):
            await select_history(_base(), [_ex("old"), _ex("latest")], prepare, _ok_check)
        for candidate in created:
            with pytest.raises(PromptCountUnavailable):
                await candidate.complete()

    asyncio.run(main())
    assert calls == [1, 1]


def test_mutating_discarded_candidate_list_cannot_corrupt_later_suffix():
    previous = []
    checks = 0

    async def prepare(messages):
        previous.append(messages)
        return PreparedComplete(
            _make_count(fits=len(previous) == 2, digest=_digest(messages)), _gen_never()
        )

    async def check():
        nonlocal checks
        checks += 1
        if checks == 3:
            previous[0][-1]["content"] = "expired candidate only"

    base, exchanges = _base(), [_ex("old"), _ex("latest")]
    selection = asyncio.run(select_history(base, exchanges, prepare, check))
    assert selection.messages == base + exchanges[-1]
    assert selection.messages[-1]["content"] == "result latest"


@pytest.mark.parametrize(
    "error", [AgentContextUnavailable, AgentContextOverflow, AgentContextStale]
)
def test_native_errors_never_include_optional_payload(error):
    assert str(error("secret source content", payload="secret provider body")) == str(error())


@pytest.mark.parametrize("which", ["prepare", "check"])
def test_nonawaitable_capability_is_refused_without_generation(which):
    prepare = (lambda messages: None) if which == "prepare" else _always_fits()
    check = (lambda: None) if which == "check" else _ok_check
    with pytest.raises(AgentContextUnavailable):
        asyncio.run(select_history(_base(), [], prepare, check))


def test_measurement_limit_discards_every_rejected_preparation():
    candidates = []

    async def prepare(messages):
        candidate = PreparedComplete(
            _make_count(fits=False, digest=_digest(messages)), _gen_never()
        )
        candidates.append(candidate)
        return candidate

    async def main():
        with pytest.raises(AgentContextUnavailable):
            await select_history(
                _base(), [_ex("old"), _ex("new")], prepare, _ok_check, max_measurements=1
            )
        assert len(candidates) == 1
        with pytest.raises(PromptCountUnavailable):
            await candidates[0].complete()

    asyncio.run(main())


def test_exchange_limit_accepts_exactly_512_complete_notices():
    exchanges = [[{"role": "user", "content": f"notice {i}"}] for i in range(512)]
    selection = asyncio.run(select_history(_base(), exchanges, _always_fits(), _ok_check))
    assert selection.kept_exchanges == 512
    assert selection.messages[-1]["content"] == "notice 511"


def test_selected_messages_are_detached_from_prepared_generation_body():
    seen = []

    async def prepare(messages):
        async def generate():
            seen.extend(dict(message) for message in messages)
            return "ok"

        return PreparedComplete(_make_count(fits=True, digest=_digest(messages)), generate)

    async def main():
        base, exchanges = _base(), [_ex("latest")]
        original = base + exchanges[0]
        selection = await select_history(base, exchanges, prepare, _ok_check)
        selection.messages[-1]["content"] = "caller-local presentation change"
        assert await selection.prepared.complete() == "ok"
        assert seen == original

    asyncio.run(main())


@pytest.mark.parametrize(
    "error", [AgentContextStale, AgentContextOverflow, AgentContextUnavailable]
)
def test_preparation_preserves_native_context_failure_type(error):
    calls = []
    failure = error()

    async def prepare(messages):
        calls.append(1)
        raise failure

    with pytest.raises(error) as raised:
        asyncio.run(select_history(_base(), [_ex("latest")], prepare, _ok_check))
    assert raised.value is failure
    assert calls == [1]
