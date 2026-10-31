# Constant UI decoding diagnostics (#169)

The strict Meta transport accepts JSON text inside `arguments.ui_spec_json`.
Its outer schema cannot establish that this inner text is a bounded, valid
publication object. Eight invalid synthetic values previously produced the
same native failure text, so the retained error could not identify the rejected
condition. This change adds diagnostics; it does not repair malformed proposals
or explain the historical repeated creation of views.

## Native failure record

An authenticated failed `agent.outcome` may additionally contain:

```json
{"provider_diagnostic":{"stage":"ui_arguments","reason":"duplicate_key"}}
```

This additive projection uses the existing version 1.0 journal envelope. The
task's state, error text, identity, evidence and recovery rules are unchanged.
Only a freshly validated constant stage/reason pair enters this field. Raw model
text, object keys, values, parser positions, exception representations and
internal exception causes are excluded. An unavailable or invalid optional
diagnostic does not prevent the ordinary failed outcome. Ordinary HTTP/provider
failures do not acquire an invented decoding diagnosis.

Stages are `ui_arguments` (the inner logical publication object), `ui_wire`
(the strict transport wrapper), and `unavailable_action` (the bounded rejection
envelope for a known operation outside the native advertisement). The opt-in
[typed Meta format](meta-typed-actions.md) adds `typed_wire` for its bounded
wrapper and strict per-operation validation; `typed_wire/schema` reports a
branch/field/type rejection with no Pydantic/input details. The original
flat-format stages and messages remain unchanged.

| Reason | Rejected condition |
| --- | --- |
| `text_type` | The selected JSON text is not a strict string. |
| `size_limit` | Text is empty or exceeds the existing character/UTF-8 byte guard. |
| `json_syntax` | JSON syntax is invalid. |
| `duplicate_key` | An object repeats a key, including in nested data. |
| `non_finite` | A JSON non-finite literal or exponent overflow cannot be represented. |
| `invalid_unicode` | Text or its decoded contents cannot encode as UTF-8. |
| `nesting_limit` | The existing parser/encoder nesting guard rejects the value. |
| `not_object` | Valid JSON decodes to another top-level type. |
| `decode_limit` | Another existing decoder limit rejects the value. |
| `field_shape` | Required top-level publication or wrapper fields do not match. |
| `unknown_fields` | The publication wrapper has undeclared argument keys. |
| `argument_fields` | Active publication arguments are not exactly `ui_spec_json`. |
| `unexpected_ui_field` | Another action supplies a non-null `ui_spec_json`. |

Not every pair is valid. The protected exception constructor allows only the
defined combinations. Diagnostic order follows actual guard order: an actual
unpaired surrogate inside the wrapper can fail at `ui_wire`, whereas an escaped
surrogate decoded from otherwise valid inner JSON fails at `ui_arguments`.
Null/missing `ui_spec_json` fails the active-wire-arguments check; it is not
reclassified as an inner type error. No payload is reparsed merely to obtain a
more convenient diagnosis.

## Decoder compatibility and authority

The shared bounded JSON decoder tags only its five existing rejection sites
with `JsonProtocolError`, a `ValueError` subclass retaining the exact original
messages. Syntax and Unicode errors keep their existing types; successful
decoding is unchanged. In particular, its previously accepted nested exponent
overflow and escaped surrogate remain the UI codec's responsibility to reject
at its stricter serialization boundary. No Whistle framing, numerical policy,
resource limit or transport authority changes.

UI character and UTF-8 byte bounds, closed objects, duplicate-key checks,
non-finite rejection, native metadata exclusion and final component validation
remain required. Decoding failure stays terminal with no automatic inference,
coercion, repair, publication retry or finish. A previously published, verified
artifact remains usable; it does not turn the failed task into success.

## Traceability and limits

Issue [#169](https://github.com/rgoza1996/Kinaiya/issues/169), Roadmap §§9/18/20
and `AGENTS.md` require truthful failures and reproducible evidence. The
implementation is in `packages/providers/text.py`,
`packages/providers/whistle_ipc.py` and `services/agent/runtime.py`. Tests in
`test_json_decode_diagnostics.py`, `test_meta_action_diagnostics.py` and
`tests/integration/test_meta_ui_actions.py` use the actual adapter with
MockTransport, native SQLite journals and a queued-finish execution trap.

These diagnostics classify a future rejected proposal. They cannot recover the
historical invalid value, establish model quality, or establish why a model
chose to create several valid views. #169 remains open for bounded actual-model
qualification through ordinary Converse/Decisions routing.
Initial outer Responses/action syntax errors that cannot identify a valid
operation and final native component-validation failures retain their existing
error paths; this is not a universal provider diagnosis.
