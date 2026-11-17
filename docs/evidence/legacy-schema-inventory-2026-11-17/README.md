# Legacy schema inventory correction (#684)

The legacy freeze helper accidentally included five new additive live-stream
contracts, changing its observed inventory from 184 to 189. This increment adds
only those exact names to the helper's existing additive-family inventory.
The original 184-count/digest assertion, all 74 assertion ASTs, and every codec
byte/depth/node-limit check remain unchanged. No contract or runtime changes.

Baseline: `b39b4303e50be844f92be4e93d6c082147ecb356` on
`codex/623-adaptive-call-integration`. [Issue #684](https://github.com/rgoza1996/Kinaiya/issues/684)
was filed before repair. The [manifest](manifest.json) records source, assertion,
schema and log digests, including original source under `sources/`.

| Check | Result | Receipt |
| --- | --- | --- |
| Original local reproduction on Python 3.13 | Failed: 189 instead of 184 | [First command and pins](logs/issue-684-legacy-schema-first-receipt.json) |
| Complete legacy finalization plus live-stream, negative-recovery and utterance contract modules | 97 passed on Python 3.13.18 | [Command and pins](logs/issue-684-schema-contracts-313-receipt.json) |
| Same scope | 97 passed on Python 3.14.10 | [Command and pins](logs/issue-684-schema-contracts-314-receipt.json) |
| Ruff check | Passed | [Command and pins](logs/issue-684-schema-ruff-receipt.json) |
| Ruff format check | 1,222 files already formatted | [Command and pins](logs/issue-684-schema-format-receipt.json) |
| Complete schema export with explicit worktree imports | 599 schema hashes unchanged | [Export record](schemas.json) |

The restored legacy digest remains
`21351c70f152823480441448d140c49cd561aa9be0a9a4e760c1d8035257328a`.
Readback against main `50272e8a9dcc3786994ce63651126f88fddbc22f` confirms all
594 existing schema files are byte-identical. The only additions are
`LiveStreamAbort`, `LiveStreamAbortCapability`, `LiveStreamAbortProof`,
`LiveStreamNextUtterance` and `LiveUtteranceCapability`. Their dedicated contract
modules verify their complete generated schema, closed fields, required fields
and authority boundaries.

The failed PR #683 [CI run](https://github.com/rgoza1996/Kinaiya/actions/runs/44081144139)
used head `05f73fae87664e2f9ccbcb0888b3a1bd68bd2672`. Python 3.12 job
`134031749643` and Python 3.14 job `134031749835` each report one failed,
16,091 passed, 23 skipped and 49 deselected tests, with the same inventory
assertion failure. Both complete original job logs and their receipts remain
under `logs/`; these are failure evidence, not passing qualification.

All 923 Python inputs match the four repaired-source local checks. Compared with
the [453-case Call qualification](../adaptive-call-permission-observer-2026-11-17/README.md),
only this separate inventory test changes; production Python, frontend inputs
and the fifteen actual native/Dart scenarios remain unchanged. That record still
qualifies its stated runtime revision; it is not relabeled as a new all-source
run. Updated PR CI must qualify the final composition.

Each `.log.json` stores exact original UTF-8 output and its raw byte SHA-256;
encoding `text` recovers the raw bytes. The manifest separately pins archive and
unchanged receipt digests. The original source and all assertion ASTs are
preserved. No dependency installs/upgrades, model calls, held native CI retries
or installed-owner promotion were performed locally. Adaptive Live stays gated
off; #623 remains open.
