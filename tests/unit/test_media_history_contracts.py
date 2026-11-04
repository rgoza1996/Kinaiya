"""Independent finite-history DTO oracles, authored candidate-body blind.

Frozen protocol shapes describe metadata only. These synthetic references do
not prove owner permission, a real body/track/page relationship, actual input
EOF, decoder qualification, cleanup, publication or an executable media demo.
"""

import copy
import hashlib
import json

import pytest

from packages.contracts import media as m

_MAX = 9_007_199_254_740_991
_ROLES = (
    "converse",
    "decisions",
    "input_asr",
    "assistant_asr",
    "tts",
    "playback",
    "agent_media",
    "decoder",
)
_NAMES = (
    "Disclosure",
    "Consumer",
    "Blob",
    "TrackRange",
    "Unit",
    "ManifestPage",
    "ManifestSeal",
    "MediaRange",
)


def _json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(domain, value):
    return hashlib.sha256(b"kinaiya.media.v1/" + domain.encode() + b"\0" + _json(value)).hexdigest()


def _id(index):
    return f"{index:032x}"


def _hash(index):
    return f"{index:064x}"


def _ref(index=1):
    return {"backend_id": _id(1), "receipt_id": _id(index), "sha256": _hash(index)}


def _q(num=0, den=1):
    return {"num": num, "den": den}


def _disclosure(role="converse", scope="local_only"):
    return {
        "owner_event": "synthetic-owner-event",
        "role": role,
        "selection_sha256": _hash(2),
        "protocol_sha256": _hash(3),
        "scope": scope,
    }


def _pin_consumer(value):
    result = copy.deepcopy(value)
    result.pop("permit_sha256", None)
    result["permit_sha256"] = _digest("Consumer", result)
    return result


def _consumer(role="decoder", scope="local_only", **changes):
    value = {
        "v": 1,
        "consumer": {"consumer_id": _id(2), "generation": 1},
        "source": _ref(3),
        "fence": {
            "session": {"session_id": "synthetic-session", "thread_id": "thread", "epoch": 1},
            "source_revision": 1,
            "generation": 1,
            "lease_generation": 1,
        },
        "role": role,
        "instance": _id(4),
        "capability_sha256": None if role == "decoder" else _hash(5),
        "decoder_profile_sha256": _hash(6) if role == "decoder" else None,
        "scope": scope,
        "disclosure": None,
        "anchor": None,
        "task_admission": None,
        "owner_intent": None,
        "ranges": [],
        "permit_sha256": _hash(0),
    }
    value.update(changes)
    return _pin_consumer(value)


def _blob(*, audio=True):
    return {
        "v": 1,
        "source": _ref(3),
        "generation": 1,
        "track_id": _id(7),
        "source_seq": 1,
        "track_seq": 1,
        "pts": {
            "pts": -16 if audio else -2,
            "time_base_num": 1,
            "time_base_den": 16000 if audio else 25,
            "sample_start": 0 if audio else None,
            "sample_count": 16 if audio else None,
            "frame_index": None if audio else 0,
        },
        "start": _q(),
        "end": _q(1, 1000) if audio else _q(),
        "format_sha256": _hash(8),
        "body_bytes": 32 if audio else 8,
        "body_sha256": _hash(9),
        "raw_state": "buffered",
    }


def _track_range(index=1, *, frames=False, count=0):
    return {
        "track_id": _id(index),
        "first_track_seq": 1,
        "through_track_seq": 1,
        "first_sample": None if frames else 0,
        "sample_count": None if frames else count,
        "first_frame": 0 if frames else None,
        "frame_count": count if frames else None,
    }


def _unit(index=1):
    return {
        "unit_id": _id(index),
        "start": _q(index - 1),
        "end": _q(index),
        "first_source_seq": 2 * index - 1,
        "through_source_seq": 2 * index,
        "tracks": [_track_range(1, count=16), _track_range(2, frames=True, count=1)],
    }


def _page():
    return {
        "v": 1,
        "manifest_id": _id(10),
        "source": _ref(3),
        "generation": 1,
        "index": 0,
        "previous_page": None,
        "units": [_unit()],
    }


def _seal():
    return {
        "v": 1,
        "manifest_id": _id(10),
        "source": _ref(3),
        "generation": 1,
        "last_page": _ref(11),
        "page_count": 1,
        "unit_count": 1,
        "through_source_seq": 2,
        "input_end": None,
        "complete": False,
    }


def _range():
    return {
        "v": 1,
        "range_id": _id(12),
        "source": _ref(3),
        "source_equivalence_id": _id(13),
        "source_revision": 1,
        "generation": 1,
        "manifest_id": _id(10),
        "through_page": _ref(11),
        "tracks": [_track_range(1, count=16), _track_range(2, frames=True, count=1)],
        "start": _q(),
        "end": _q(1),
        "first_source_seq": 1,
        "through_source_seq": 2,
    }


_BUILDERS = {
    "Disclosure": _disclosure,
    "Consumer": _consumer,
    "Blob": _blob,
    "TrackRange": _track_range,
    "Unit": _unit,
    "ManifestPage": _page,
    "ManifestSeal": _seal,
    "MediaRange": _range,
}


def _kind(name):
    return getattr(m, name if name == "MediaRange" else "Media" + name)


def _typed(name, value=None):
    return m.media_json(_json(_BUILDERS[name]() if value is None else value), _kind(name))


def _reject(name, value, *, code=None):
    raw = value if type(value) is bytes else _json(value)
    with pytest.raises(m.MediaContractError) as failure:
        m.media_json(raw, _kind(name))
    assert failure.value.code in {"bad_packet", "bound"}
    if code is not None:
        assert failure.value.code == code


@pytest.mark.parametrize("name", _NAMES)
def test_exact_registered_type_schema_wire_and_hash_domain(name):
    raw = _BUILDERS[name]()
    value = _typed(name, raw)
    assert type(value) is _kind(name)
    assert m.media_bytes(value) == _json(raw)
    assert m.media_json(m.media_bytes(value), _kind(name)) == value
    model_name = "MediaRange" if name == "MediaRange" else "Media" + name
    assert m.MEDIA_SCHEMAS[model_name] is _kind(name)
    schema = m.MEDIA_SCHEMAS[model_name].model_json_schema()
    assert set(schema["properties"]) == set(raw)
    assert set(schema["required"]) == set(raw)
    assert schema["additionalProperties"] is False
    unsigned = {
        key: item for key, item in raw.items() if name != "Consumer" or key != "permit_sha256"
    }
    assert m.media_digest(name, value) == _digest(name, unsigned)


@pytest.mark.parametrize(
    "name,field",
    [(name, field) for name in _NAMES for field in _BUILDERS[name]()],
    ids=lambda value: str(value),
)
def test_every_declared_field_is_required_including_nullable_fields(name, field):
    raw = _BUILDERS[name]()
    assert _kind(name).model_fields[field].is_required()
    del raw[field]
    if name == "Consumer" and field != "permit_sha256":
        raw = _pin_consumer(raw)
    _reject(name, raw)


@pytest.mark.parametrize("name", _NAMES)
def test_unknown_fields_and_duplicate_root_json_keys_are_rejected(name):
    raw = _BUILDERS[name]()
    field = next(iter(raw))
    duplicate = b"{" + _json(field) + b":" + _json(raw[field]) + b"," + _json(raw)[1:]
    _reject(name, duplicate, code="bad_packet")
    raw["native_permission_override"] = True
    # Keep Consumer's otherwise-valid original permit. A decoder that wrongly
    # strips the extra field would then accept, rather than fail a stale hash.
    _reject(name, raw, code="bad_packet")


@pytest.mark.parametrize("name", ["Consumer", "Blob", "ManifestPage", "ManifestSeal", "MediaRange"])
@pytest.mark.parametrize("version", [True, 1.0, "1", 2], ids=["bool", "float", "string", "v2"])
def test_history_versions_are_exact_integer_one(name, version):
    raw = _BUILDERS[name]()
    raw["v"] = version
    if name == "Consumer":
        raw = _pin_consumer(raw)
    _reject(name, raw, code="bad_packet")


@pytest.mark.parametrize("role", _ROLES)
def test_disclosure_role_enum_and_utf8_reference_boundary(role):
    raw = _disclosure(role)
    raw["owner_event"] = "é" * 64
    assert _typed("Disclosure", raw).owner_event == "é" * 64
    raw["owner_event"] += "é"
    _reject("Disclosure", raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_event", ""),
        ("owner_event", 17),
        ("role", "owner"),
        ("scope", "private"),
        ("selection_sha256", "A" * 64),
        ("protocol_sha256", "0" * 63),
    ],
    ids=["empty-ref", "coerced-ref", "unknown-role", "unknown-scope", "upper-hash", "short-hash"],
)
def test_disclosure_lexical_shapes(field, value):
    raw = _disclosure()
    raw[field] = value
    _reject("Disclosure", raw)


@pytest.mark.parametrize("role", _ROLES)
def test_consumer_role_and_matching_optional_disclosure_roundtrip(role):
    raw = _consumer(role, "public_synthetic", disclosure=_disclosure(role, "public_synthetic"))
    assert m.media_bytes(_typed("Consumer", raw)) == _json(raw)


@pytest.mark.parametrize("role", _ROLES[:-1])
@pytest.mark.parametrize(
    "capability,profile",
    [
        (None, None),
        (_hash(5), None),
        (None, _hash(6)),
        (_hash(5), _hash(6)),
    ],
    ids=["both-null", "capability-only", "profile-only", "both-hashes"],
)
def test_nondecoder_nullable_selection_fields_do_not_mint_native_role_policy(
    role, capability, profile
):
    raw = _consumer(
        role,
        "public_synthetic",
        capability_sha256=capability,
        decoder_profile_sha256=profile,
    )
    assert m.media_bytes(_typed("Consumer", raw)) == _json(raw)


@pytest.mark.parametrize(
    "fault",
    [
        "disclosure-role",
        "disclosure-scope",
        "decoder-no-profile",
        "decoder-capability",
        "unknown-role",
        "endpoint",
    ],
)
def test_consumer_exact_shape_relations_without_native_reference_resolution(fault):
    raw = _consumer()
    if fault == "disclosure-role":
        raw["disclosure"] = _disclosure("converse")
    elif fault == "disclosure-scope":
        raw["disclosure"] = _disclosure("decoder", "public_synthetic")
    elif fault == "decoder-no-profile":
        raw["decoder_profile_sha256"] = None
    elif fault == "decoder-capability":
        raw["capability_sha256"] = _hash(9)
    elif fault == "unknown-role":
        raw["role"] = "owner"
    else:
        raw["endpoint"] = {"address": "http://127.0.0.1"}
    _reject("Consumer", _pin_consumer(raw))


def test_consumer_permit_omits_only_root_self_field_and_preserves_nested_hashes():
    raw = _consumer(
        "converse",
        "private_cloud_authorized",
        disclosure=_disclosure("converse", "private_cloud_authorized"),
        anchor=_ref(20),
        task_admission=_ref(21),
        owner_intent=_ref(22),
        ranges=[_ref(23)],
    )
    unsigned = copy.deepcopy(raw)
    del unsigned["permit_sha256"]
    expected = _digest("Consumer", unsigned)
    assert raw["permit_sha256"] == expected
    assert m.media_digest("Consumer", unsigned) == expected
    assert m.media_digest("Consumer", raw) == expected
    altered_self = dict(raw, permit_sha256=_hash(99))
    assert m.media_digest("Consumer", altered_self) == expected
    _reject("Consumer", altered_self)
    for path in (
        ("source", "sha256"),
        ("disclosure", "protocol_sha256"),
        ("disclosure", "selection_sha256"),
        ("anchor", "sha256"),
        ("ranges", 0, "sha256"),
    ):
        changed = copy.deepcopy(raw)
        parent = changed
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = _hash(100)
        _reject("Consumer", changed)
        repaired = _pin_consumer(changed)
        assert repaired["permit_sha256"] != expected
        assert m.media_bytes(_typed("Consumer", repaired)) == _json(repaired)


def test_consumer_ranges_cap_and_distinct_exact_references():
    raw = _consumer(ranges=[_ref(index) for index in range(64)])
    assert len(_typed("Consumer", raw).ranges) == 64
    _reject("Consumer", _consumer(ranges=[_ref(index) for index in range(65)]), code="bound")
    _reject("Consumer", _consumer(ranges=[_ref(1), _ref(1)]))
    # A conflicting native digest remains a distinct structural ref. Resolving
    # either ref requires native stored-kind/digest checks, outside this DTO.
    other_digest = dict(_ref(1), sha256=_hash(99))
    assert len(_typed("Consumer", _consumer(ranges=[_ref(1), other_digest])).ranges) == 2


@pytest.mark.parametrize(
    "raw_state", ["buffered", "retained", "expired", "delete_pending", "deleted"]
)
def test_blob_signed_audio_clock_and_all_raw_states(raw_state):
    raw = _blob()
    raw["raw_state"] = raw_state
    value = _typed("Blob", raw)
    assert value.pts.pts == -16 and value.pts.sample_start == 0
    assert value.raw_state == raw_state


def test_blob_audio_and_frame_boundaries_and_point_images():
    audio = _blob()
    audio.update(end=_q(1), body_bytes=384000)
    assert _typed("Blob", audio).body_bytes == 384000
    audio["body_bytes"] += 1
    _reject("Blob", audio, code="bound")
    audio = _blob()
    audio["end"] = _q(16001, 16000)
    _reject("Blob", audio, code="bound")
    frame = _blob(audio=False)
    frame["body_bytes"] = 8388608
    value = _typed("Blob", frame)
    assert value.start == value.end and value.pts.frame_index == 0
    frame["body_bytes"] += 1
    _reject("Blob", frame, code="bound")


@pytest.mark.parametrize(
    "fault",
    [
        "mixed-pts",
        "partial-audio",
        "no-pts-lane",
        "reverse-time",
        "nonreduced",
        "bad-hash",
        "bad-track",
        "path",
        "zero-body",
    ],
)
def test_blob_shape_and_lengths_do_not_supply_native_authority(fault):
    raw = _blob()
    if fault == "mixed-pts":
        raw["pts"]["frame_index"] = 0
    elif fault == "partial-audio":
        raw["pts"]["sample_start"] = None
    elif fault == "no-pts-lane":
        raw["pts"]["sample_start"] = raw["pts"]["sample_count"] = None
    elif fault == "reverse-time":
        raw["start"] = _q(1)
    elif fault == "nonreduced":
        raw["end"] = _q(2, 2000)
    elif fault == "bad-hash":
        raw["body_sha256"] = "z" * 64
    elif fault == "bad-track":
        raw["track_id"] = "A" * 32
    elif fault == "path":
        raw["path"] = "/synthetic/source.wav"
    else:
        raw["body_bytes"] = 0
    _reject("Blob", raw)


@pytest.mark.parametrize("mask", range(16), ids=lambda mask: f"nullable-pairs-{mask:04b}")
def test_track_range_has_exactly_one_complete_nullable_pair_and_zero_count_is_legal(mask):
    raw = _track_range()
    fields = ("first_sample", "sample_count", "first_frame", "frame_count")
    for index, field in enumerate(fields):
        raw[field] = 0 if mask & (1 << index) else None
    if mask in (3, 12):
        assert m.media_bytes(_typed("TrackRange", raw)) == _json(raw)
    else:
        _reject("TrackRange", raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_track_seq", 0),
        ("through_track_seq", 0),
        ("first_sample", -1),
        ("sample_count", -1),
        ("first_sample", _MAX + 1),
        ("sample_count", _MAX + 1),
    ],
)
def test_track_range_integer_boundaries(field, value):
    raw = _track_range()
    raw[field] = value
    _reject("TrackRange", raw)


def test_track_range_order_and_safe_zero_or_maximum_counts():
    raw = _track_range()
    raw.update(first_track_seq=2, through_track_seq=1)
    _reject("TrackRange", raw)
    for frames in (False, True):
        for count in (0, _MAX):
            assert _typed("TrackRange", _track_range(frames=frames, count=count)) is not None


@pytest.mark.parametrize("name", ["Unit", "MediaRange"])
def test_unit_and_range_allow_multiple_tracks_points_and_eight_track_boundary(name):
    raw = _BUILDERS[name]()
    raw["end"] = copy.deepcopy(raw["start"])
    assert len(_typed(name, raw).tracks) == 2
    raw["tracks"] = [_track_range(index, frames=index % 2 == 0) for index in range(8)]
    assert len(_typed(name, raw).tracks) == 8
    raw["tracks"].append(_track_range(9))
    _reject(name, raw, code="bound")


@pytest.mark.parametrize("name", ["Unit", "MediaRange"])
@pytest.mark.parametrize(
    "fault", ["empty-tracks", "duplicate-track", "reverse-time", "reverse-source-sequence"]
)
def test_unit_and_range_shape_refusals(name, fault):
    raw = _BUILDERS[name]()
    if fault == "empty-tracks":
        raw["tracks"] = []
    elif fault == "duplicate-track":
        raw["tracks"][1]["track_id"] = raw["tracks"][0]["track_id"]
    elif fault == "reverse-time":
        raw["start"], raw["end"] = _q(2), _q(1)
    else:
        raw["first_source_seq"], raw["through_source_seq"] = 3, 2
    _reject(name, raw)


def test_unit_duration_ten_seconds_and_ranges_can_span_more_than_one_unit():
    raw = _unit()
    raw["end"] = _q(10)
    assert _typed("Unit", raw).end.num == 10
    raw["end"] = _q(10001, 1000)
    _reject("Unit", raw, code="bound")
    raw = _range()
    raw["end"] = _q(11)
    assert _typed("MediaRange", raw).end.num == 11


def test_manifest_empty_pages_chaining_and_sixty_four_unit_boundary():
    raw = _page()
    raw["units"] = []
    assert _typed("ManifestPage", raw).units == []
    raw.update(index=1, previous_page=_ref(30))
    assert _typed("ManifestPage", raw).previous_page is not None
    raw.update(index=0, previous_page=None, units=[_unit(index) for index in range(1, 65)])
    assert len(_typed("ManifestPage", raw).units) == 64
    raw["units"].append(_unit(65))
    _reject("ManifestPage", raw, code="bound")


@pytest.mark.parametrize(
    "fault",
    [
        "first-has-previous",
        "later-no-previous",
        "duplicate-unit",
        "reverse-presentation",
        "overlap-presentation",
        "reverse-sequence",
        "overlap-sequence",
    ],
)
def test_manifest_page_chain_and_ordered_nonoverlapping_unit_spans(fault):
    raw = _page()
    raw["units"] = [_unit(1), _unit(2)]
    if fault == "first-has-previous":
        raw["previous_page"] = _ref(30)
    elif fault == "later-no-previous":
        raw["index"] = 1
    elif fault == "duplicate-unit":
        raw["units"][1]["unit_id"] = raw["units"][0]["unit_id"]
    elif fault == "reverse-presentation":
        raw["units"][0]["start"], raw["units"][0]["end"] = _q(3), _q(4)
    elif fault == "overlap-presentation":
        raw["units"][1]["start"] = _q(1, 2)
    elif fault == "reverse-sequence":
        raw["units"][0]["first_source_seq"] = 5
        raw["units"][0]["through_source_seq"] = 6
    else:
        raw["units"][1]["first_source_seq"] = 2
    _reject("ManifestPage", raw)


def test_manifest_equal_presentation_points_but_distinct_source_spans_are_legal():
    raw = _page()
    raw["units"] = [_unit(1), _unit(2)]
    for unit in raw["units"]:
        unit["start"] = unit["end"] = _q()
    assert len(_typed("ManifestPage", raw).units) == 2


@pytest.mark.parametrize(
    "complete,input_end,allowed",
    [
        (False, None, True),
        (False, _ref(40), True),
        (True, None, False),
        (True, _ref(40), True),
    ],
    ids=["incomplete-no-end", "incomplete-has-end", "complete-no-end", "complete-has-end"],
)
def test_manifest_complete_requires_input_end_in_only_one_direction(complete, input_end, allowed):
    raw = _seal()
    raw.update(complete=complete, input_end=input_end)
    if allowed:
        value = _typed("ManifestSeal", raw)
        assert value.complete is complete
    else:
        _reject("ManifestSeal", raw)


@pytest.mark.parametrize("field", ["page_count", "unit_count", "through_source_seq", "generation"])
def test_manifest_seal_positive_counts_are_not_coerced_to_zero(field):
    raw = _seal()
    raw[field] = 0
    _reject("ManifestSeal", raw)


@pytest.mark.parametrize(
    "name,field",
    [
        ("Blob", "generation"),
        ("Blob", "source_seq"),
        ("TrackRange", "first_track_seq"),
        ("TrackRange", "sample_count"),
        ("Unit", "first_source_seq"),
        ("ManifestPage", "index"),
        ("ManifestSeal", "page_count"),
        ("MediaRange", "source_revision"),
    ],
)
@pytest.mark.parametrize("value", [True, "1", 1.0], ids=["bool", "string", "float"])
def test_no_numeric_scalar_coercion(name, field, value):
    raw = _BUILDERS[name]()
    raw[field] = value
    _reject(name, raw, code="bad_packet")


def test_nested_scalar_and_boolean_coercion_are_refused():
    consumer = _consumer()
    consumer["consumer"]["generation"] = True
    _reject("Consumer", _pin_consumer(consumer), code="bad_packet")
    blob = _blob()
    blob["pts"]["time_base_den"] = "16000"
    _reject("Blob", blob, code="bad_packet")
    seal = _seal()
    seal["complete"] = 0
    _reject("ManifestSeal", seal, code="bad_packet")


@pytest.mark.parametrize(
    "number", [b"NaN", b"Infinity", b"1.0", b"1e0"], ids=["nan", "infinity", "decimal", "exponent"]
)
def test_wire_numbers_do_not_admit_nonfinite_or_floating_representations(number):
    raw = _json(_track_range())
    assert b'"sample_count":0' in raw
    raw = raw.replace(b'"sample_count":0', b'"sample_count":' + number, 1)
    _reject("TrackRange", raw, code="bad_packet")


@pytest.mark.parametrize("name", _NAMES)
def test_receipt_and_found_roundtrips_keep_exact_registered_domain(name):
    kind = _kind(name)
    body = _typed(name)
    unsigned = {
        "v": 1,
        "id": _id(50),
        "key": "synthetic-history-receipt",
        "request_sha256": _hash(51),
        "body": body.model_dump(mode="json"),
    }
    expected = _digest(f"R<{name}>", unsigned)
    raw = dict(unsigned, sha256=expected)
    receipt_kind, found_kind = m.MediaReceipt[kind], m.MediaFound[kind]
    assert m.MEDIA_SCHEMAS["MediaReceipt" + name] is receipt_kind
    assert m.MEDIA_SCHEMAS["MediaFound" + name] is found_kind
    receipt = m.media_json(_json(raw), receipt_kind)
    assert m.media_bytes(receipt) == _json(raw)
    assert m.media_digest(f"R<{name}>", receipt) == expected
    for found in (False, True):
        envelope = {"v": 1, "found": found, "value": raw if found else None}
        assert m.media_bytes(m.media_json(_json(envelope), found_kind)) == _json(envelope)
    for envelope in (
        {"v": 1, "found": False, "value": raw},
        {"v": 1, "found": True, "value": None},
    ):
        with pytest.raises(m.MediaContractError):
            m.media_json(_json(envelope), found_kind)
    raw["sha256"] = _hash(99)
    with pytest.raises(m.MediaContractError):
        m.media_json(_json(raw), receipt_kind)


def test_media_range_does_not_use_unselected_range_domain_or_wrapper_alias():
    assert "MediaReceiptRange" not in m.MEDIA_SCHEMAS
    assert "MediaFoundRange" not in m.MEDIA_SCHEMAS
    assert "MediaReceiptMediaRange" in m.MEDIA_SCHEMAS
    assert "MediaFoundMediaRange" in m.MEDIA_SCHEMAS
    with pytest.raises(m.MediaContractError):
        m.media_digest("Range", _range())


@pytest.mark.parametrize("name", _NAMES)
def test_mutated_typed_models_subclasses_and_extra_graph_fields_refuse(name):
    value = _typed(name)
    value.__dict__["unselected_authority"] = object()
    with pytest.raises(m.MediaContractError) as failure:
        m.media_bytes(value)
    assert failure.value.code == "bad_packet"
    subclass = type("UnselectedHistorySubclass", (_kind(name),), {})
    with pytest.raises(ValueError):
        unsupported = subclass(**_BUILDERS[name]())
        m.media_bytes(unsupported)
    with pytest.raises(m.MediaContractError):
        m.media_json(_json(_BUILDERS[name]()), subclass)


def test_mutated_nested_refs_and_nullable_pairs_are_revalidated_by_codec():
    consumer = _typed("Consumer")
    consumer.source.__dict__["sha256"] = "invalid"
    with pytest.raises(m.MediaContractError):
        m.media_bytes(consumer)
    unit = _typed("Unit")
    unit.tracks[0].__dict__["first_frame"] = 0
    with pytest.raises(m.MediaContractError):
        m.media_bytes(unit)
    blob = _typed("Blob")
    blob.pts.__dict__["frame_index"] = 0
    with pytest.raises(m.MediaContractError):
        m.media_digest("Blob", blob)


def test_valid_count_bounded_manifest_still_obeys_ordinary_byte_limit():
    raw = _page()
    raw["units"] = [_unit(index) for index in range(1, 65)]
    for unit in raw["units"]:
        unit["tracks"] = [_track_range(index) for index in range(8)]
    assert len(_json(raw)) > 65536
    _reject("ManifestPage", raw, code="bound")
    value = _typed("ManifestPage", dict(_page(), units=[]))
    value.__dict__["units"] = [_typed("Unit", unit) for unit in raw["units"]]
    with pytest.raises(m.MediaContractError) as failure:
        m.media_bytes(value)
    assert failure.value.code == "bound"


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("limit", ["bytes", "depth", "nodes"])
def test_new_history_types_do_not_relax_existing_root_envelope_limits(name, limit):
    raw = _BUILDERS[name]()
    if limit == "bytes":
        extra = "x" * 65536
    elif limit == "depth":
        extra = None
        for _ in range(30):
            extra = [extra]
    else:
        extra = [None] * 8300
    raw["unselected"] = extra
    encoded = _json(raw)
    if limit != "bytes":
        assert len(encoded) < 65536
    _reject(name, encoded, code="bound")
