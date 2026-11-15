# Subsystem setup modes — 2026-11-15

This records actual CLI setup/save behavior for #566 at clean integrated source
`decc37278dd13155ae07ca57e0dceb821d1e62d1`. The six production Python files
are individually pinned in `verification.json`; all tracked Python source bytes
and the clean worktree were checked before and after the native run. The later
evidence commit adds documentation only.

The trusted helper ran inside a fresh bubblewrap namespace with unshared network,
all capabilities dropped, read-only source/project runtime, no owner home or GPU
devices, and private writable scratch. It used the existing project Python
runtime, installed nothing, and called the real `python -m services.launcher`
entry point. Every original CLI process was waited; each exited zero.

| Saved mode | Agent | Converse | Decisions | Actual CLI calls |
| --- | --- | --- | --- | --- |
| Core only | false | false | false | 3 |
| Agent alone | true | false | false | 3 |
| Decisions alone | false | false | true | 3 |
| Agent + Decisions | true | false | true | 3 |
| Converse + Decisions | false | true | true | 3 |
| All three | true | true | true | 3 |

Each case performs first setup, a mode-only save to Core, and a mode-only save
back to its selected mode. The helper checks the actual JSON receipt and saved
configuration against independent fixed boolean expectations. Both edits retain
the original provider declarations and original service-token bytes. Every receipt
reports `ready: false` and `provider_connection: not_checked`; mode-only receipts
add `activation: restart_required`.

`native-cli-helper.py.txt` is the retained executed helper, not an application
entry point. `native-cli-result.json` contains the original bounded results and
arguments for all 18 calls. These use only public synthetic model names and
loopback endpoints. Original stdout/stderr, namespace command, full source pins,
first failures and test commands remain in local
`output/issue-566-mode-selection/`.

The integrated related selection passes **333 tests in 11.32 seconds** across
15 files, including all 36 old/new mode transitions, invalid graphs, cancellation,
independent role settings, disclosure and credential preservation. The nine
modified existing test files retain every original assertion AST. Fixture answer
order and the prompt-aware terminal were updated for the earlier mode question;
initial constructor/adapter fixture mistakes and first failures remain retained.
Ruff lint and format checks pass, and contract schema export changes no bytes.

This is setup/save qualification only. It does not establish running service
startup, degradation, recovery, managed-resource or external-Agent behavior,
provider reachability, model quality or audio delivery. The earlier actual
service startup/shutdown matrix is separate evidence. The Options mode editor,
independent Decisions consumer policies and complete lifecycle matrix remain
required; **#566 remains incomplete**.

## Compatibility follow-up

A search across every wizard caller adds five files to the original selection.
The retained first run has two failures and 71 passes: onboarding and animation
fixtures still use the old answer order. With only their input order corrected,
the original onboarding oracle exposes redundant new Agent-only selector fields.
The implementation now preserves the original `{converse: false}` configuration
shape when documented defaults express the same validated graph. Existing
explicit selectors are updated; mode-only saves remain explicit. The original
onboarding assertion is retained.

At clean source `c71c8afa` the complete union passes **406 tests in 21.75 seconds
across 20 files**, including every wizard caller. All original assertion ASTs in
the eleven changed existing test files are unchanged. The identical trusted
native helper is run once against this corrected source in another fresh private
namespace: all six modes and all **18 original CLI processes** pass again, with
unchanged source, retained profiles/token bytes and truthful untested receipts.

`compatibility-native-cli-result.json` and `compatibility-verification.json`
retain those original results and the complete source identity/pins. This changes
the setup representation, not the qualified graph or the scope limits above.
The first failure logs remain in local `output/issue-566-mode-selection/`.
