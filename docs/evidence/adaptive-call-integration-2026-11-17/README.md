# Host and native Call transport qualification (#623)

This is an unfinished host/Call integration checkpoint. The trusted release
gate stays false; actual CharacterView microphone integration, complete
lifecycle/timing qualification and default rollout remain open. See the
[design and remaining work](../../adaptive-call-integration.md).

The starting source is `d03a6177e0068575b77800677a8e95e7433dbfd6`, tree
`e35bab9c8dfea5d43712303689d64bfcfae7bb7c`. PR #678 subsequently merged as
`50272e8a9dcc3786994ce63651126f88fddbc22f` with that identical tree after all
eight standard CI44018144078 checks passed; four opt-in checks skipped.
That hosted CI qualifies the preceding Converse consumer, not this new work.

The [manifest](manifest.json) records exact commands, runtime/dependency lock
digests, all 918 current Python inputs and 327 frontend inputs, and per-run
input changes. Every retained run reports its actual exit code and log digest.
The current Python runs use identical inputs on Python 3.13.18 and 3.14.10.
The existing Dart SDK reports 3.13.7; the Flutter dependency lock is unchanged.
The native Dart fixture copies declared application files into a temporary
project and reuses existing pinned cache metadata with `--no-pub`. No package
or SDK installation, live model endpoint or physical audio is involved.

| Current checks | Result |
| --- | --- |
| Launcher/capture/native consumer/readiness/stream controls, plus actual fresh Dart/native HTTP/IPC rollover, Python 3.13 | 206 passed; one existing Starlette warning |
| Same combined scope, Python 3.14 | 206 passed; same existing warning |
| Stream/common clients, capture mode, model/runtime settings and provider codec | 132 Flutter tests passed |
| Seven affected Dart files, `flutter analyze --no-pub` | No issues |
| `ruff check .` / `ruff format --check .` | Passed / 1,207 files already formatted |

The two combined Python log digests are
`2b925675be8d3a9b7d13c321f0169b05fd6874012d15c07957adfaeeab35603f`
and `2fa299c600ddc4bd748882ee28bd615eb4f517598c34de498cb35e8984b0c4f1`.
The current frontend log digest is
`5a745815845b8e07d82d336851ce6cb1c88e2bd367d08cd395c8c0c595eec79f`.

Actual host controls exercise both qualified/gated branches with Rolling
Windows enabled/disabled, original shared owners, startup, native readiness,
shutdown, no fabricated capture/provider probes and explicit preference
preservation. The retained native fixture and fresh live Dart control exercise
begin, contiguous PCM, paired publication, exact lookup/reconcile, finish,
same-mic decoder rollover and cancellation. The fresh control verifies two
decoder starts, exactly one PCM submission and finish, and one cancellation,
with the original STT lease retained across rollover and retired on cancellation.
Frontend controls also cover substituted source/owner/decoder metadata,
uncertain acknowledgments without audio replay, immutable originals, bounded
strict JSON, actual late transport cleanup and both client constructors.

The [first logs](logs/) and [original sources](sources/) remain retained:
Log envelopes preserve exact UTF-8 bytes and their original SHA-256, including
whitespace emitted by failing tools. Original logs were not trimmed.

- The initial rollout control exposed the missing trusted owner parameter.
- The first host controls exposed two fixture errors (private directory mode
  and consumed HTTP response stream), then the missing native host owner and
  an incorrect SQLite Row comparison in the fixture. Private storage and
  bounded HTTP guards remained intact.
- The first no-pub attempt stopped before testing because cached package-graph
  metadata was absent. Matching existing cache metadata was reused.
- The first broader frontend run found the missing mock-constructor initializer.
- The first fresh native Dart control found a frontend rollover predicate that
  incorrectly required STT process release. The same native mic correctly
  retained that lifetime. The repair requires an exact confirmed finish plus
  decoder completion and actual decoder/HTTP ownership retirement, preserving
  process release separately. Its original failing assertions remain unchanged.

Each finding was recorded in #623 before repair. The constructor source patch
was reconstructed against its recorded Git blob using `patch --fuzz=0` and
compared byte-for-byte with the original snapshot. Model answer, classification,
persona and speech quality are diagnostic observations; none gates these checks.
