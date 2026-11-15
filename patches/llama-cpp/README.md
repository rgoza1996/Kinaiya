# Native realtime disabled-tool compatibility candidate

This is a downstream qualification candidate for llama.cpp source commit
`8ca4060b7e30e76baaa5670ee1ec1b760b7f8552`, associated with issue #483.
It is not installed by Kinaiya, does not change an owner engine, and does not
repair the observed model's delegated-work dialogue behavior.

The realtime engine supports no tool invocation. The patch makes that contract
explicit: session metadata reports empty `tools` and `tool_choice: "none"`,
and session/response requests attempting to enable tools or supplying malformed
tool settings fail before changing session state or creating a response.
Missing settings retain the disabled default. No prompt, model, sampler, audio
mode or Kinaiya output guard changes.

Use a separate source/build directory for evaluation. Check the exact base
commit and source SHA-256 recorded in
[the qualification evidence](../../docs/evidence/native-converse-2026-11-14/observations.json)
before applying this zero-context patch:

```sh
git apply --unidiff-zero --check /path/to/8ca4060-realtime-no-tools.patch
git apply --unidiff-zero /path/to/8ca4060-realtime-no-tools.patch
```

The patch's resulting source was independently compared byte-for-byte with the
source used to build the private test overlay. Eighteen real WebSocket controls
passed against that compiled overlay. All installed/source files were retained;
the overlay used the existing build's other objects/libraries. This does not
establish a clean rebuild, a new supported engine release, reference-model
equivalence, or audio/dialogue quality.

The original engine's missing acknowledgment remains a failed qualification.
Do not remove Kinaiya's positive acknowledgment check or assume that an endpoint
supports disabled tools just because it ignores unknown request fields.
No change has been submitted to the upstream project.
