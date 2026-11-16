# Dart/native fixture source isolation (#637)

Baseline: `548f85d84874b2b0d3366ffdb36f8c74aa880ae8`. The actual
`test_owner_and_companion_dart_transports_against_live_session_asgi` previously
ran Flutter directly in the checked-out application directory. The existing
Flutter 3.47.8 SDK migrates analyzer options to exclude present platform
directories. During Python 3.13 qualification this added Android/iOS exclusions
to tracked `analysis_options.yaml`.

The first independent source-preservation oracle runs the real Dart case
successfully, then fails because one of 396 declared Flutter files changed:
the analyzer file's SHA-256 moves from
`2d0b8ceae1bd2f560b46e315924b1c35c5464e342e88e895982111e0ef57308c`
to `8af10319e1913b8d8b0cdffa362a2aeb5072daa9d1baf35113328868b1178fcf`.
The other 395 inputs remain identical. That run fails one case in 8.59 seconds;
its source-side-effect diff and original log are retained. The new helper's
preimplementation unit run also records its missing-module collection failure.

`tests/flutter_project_helpers.py` now copies only current Git-declared regular
application inputs to a separate test-owned project. It preserves tracked
working edits and the dependency lock, excludes undeclared notes and ignored
SDK caches, and refuses an in-checkout destination, missing manifest or symlink
escape. Flutter performs its normal preparation/build/test in that copy. No
SDK, analyzer rule, native API, production code or runtime permission changes.

The original actual Dart/Session case and its assertions remain. Independent
hashes now require all 396 original input files unchanged and the copy's lock
identical after SDK execution. The original temporary Session thread must
retire within the existing join budget. Transport remains real authenticated
loopback with public fixture credentials; there is no fabricated Dart result.

Final checks:

- Python 3.13.18: all four copy-boundary cases and the complete 35-case
  companion/native module pass: **39 passed in 23.78 seconds**.
- Python 3.12.3: the four copy-boundary cases and actual SDK/native case pass:
  **5 passed in 8.07 seconds**. These five overlap the first run's cases and
  are not added to its count.
- Ruff check, format check (1,042 files) and diff checks pass. One existing
  Starlette/httpx deprecation warning remains in each test run; dependencies
  and expectations are not changed to silence it.

[Receipts](verification.json) pin complete first/final logs and changed Python
inputs. Raw logs remain under local `output/issue-637-*.log`. Exact-head CI is
required before merge. This main-branch increment supplies the reusable helper
and fixes the existing companion case. The newer Core case in stacked PR #632
has the same source-directory ownership pattern and still requires its own
isolated-project integration before #637 is fully closed.

The ongoing #624 full pinned-Python run is in a different worktree and is not
modified mid-run. Its original SDK side effect remains separately recorded.
There are no model calls, quality gates, owner runtime promotions or changes
to the Python support range in this fixture repair.
