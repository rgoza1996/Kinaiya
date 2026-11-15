# Native realtime compatibility candidates

The additional [native history candidate](8ca4060-native-history.patch) addresses
#483/#626. Apply it **after** the disabled-tool patch below, on the same exact
base commit. It adds the advertised `native_history.v1` extension, real chat-role
history, bounded digest acknowledgments and collision-safe turn framing. The
[protocol and verification](../../docs/native-realtime-history.md) distinguish
native roles from the explicit limited JSON mode. Neither patch installs or
promotes an owner engine, changes model defaults, or promises model answer quality.

```sh
git apply --unidiff-zero --check /path/to/8ca4060-realtime-no-tools.patch
git apply --unidiff-zero /path/to/8ca4060-realtime-no-tools.patch
git apply --unidiff-zero --check /path/to/8ca4060-native-history.patch
git apply --unidiff-zero /path/to/8ca4060-native-history.patch
```

Ordered application was verified in a separate detached worktree. All six final
source files match [the source manifest](8ca4060-native-history-source.json)
byte-for-byte. The added `test-realtime-history` CMake target checks framing,
history parsing/digests and the original-method negative control; the retained
qualification also runs it against the pinned official LFM3 template. The
private engine overlay compiles the changed realtime object and links existing
other objects/libraries. It does not establish a clean rebuild, platform-wide
qualification or upstream support. No upstream contribution is made by this
downstream evaluation candidate.

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
