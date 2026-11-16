# Native live inference and publication evidence (#623, #665)

This increment adds the explicit v2 source/attempt ledger view, original-task
provider facade and sole exact result publisher. It leaves Adaptive Live off.
It does not complete the scheduler, microphone/STT/consumer integration,
backchannel playback, timing comparison or default rollout required by #623.

The parent is `f48d67199ffc18035c2a9100e65897295f0e9caa` (capture permission,
PR #664). [The contract](../../live-decisions-attempts.md),
[verification.json](verification.json) and [Python input pins](python-inputs.json)
identify the current source, environment, exact commands, results and log hashes.

Current focused qualification passes **158 cases on Python 3.13.18 in 25.95s**
and the same **158 on Python 3.14.10 in 26.05s**. Each run preserves all 877
declared Python inputs. Nine additive inference schemas have an independent
`66c476073f97a5bdf9ebd4131f7dbad81106918601ef7c8d30cdde206114f9d7`
byte pin. All 557 previous schema files remain byte-identical, and the original
184-schema count/hash remains unchanged.

The controls exercise actual SQLite/ASGI/JournalClient and request-bound compatible
provider transports, including all 20 combinations of the five Converse outcomes
and four independent Agent routes. They preserve exact labels and native source
bindings; they do not score model behavior. Coverage includes:

- Eight simultaneous competing admissions producing one original charge;
  explicit v1/v2 refusal, required version/model pin, canonical eligibility across
  duplicate raw same-slot/next-slot snapshots, and stale/reset/owner-off fences.
- Exact original completion and positive outcome proof, full source/decision
  digests, native origin/cause/scope/task references and append refusal.
- Lost admission, completion and publication acknowledgments both before and
  after commit, followed by read-only original lookups with no inference or
  mutation replay and exactly one original close.
- Actual model lease and sole close retained through cancellation, caller-list
  mutation after send, native service-role parity, post-body credential rotation,
  closed JSON/envelope validation, byte bounds and finite timing preservation.
- Sticky disclosure restrictions on included context, even after a later public
  relabel; publication refuses while the original raw/completed output and charge
  remain intact.

The first failures remain distinct from passing qualification:

- Live transaction controls initially report 3 failures/30 passes in 4.74s.
  Separate finalized-v1 controls report 3 failures in 0.51s (#665). Entering a
  connection context before refusing another caller's active transaction rolled
  back that caller's work. The owned helper now refuses beforehand; all original
  uncommitted-name/active-transaction assertions remain. Frozen ledger and control
  sources are included here. The intermediate repair passes 36 cases in 5.09s.
- Two positive-publication proof controls initially fail in 0.44s: a substituted
  source or valid alternative label could pass a receipt-only check. The explicit
  source and decision digests now bind that evidence. The initial contract and
  controls are retained here; the first repaired ledger run passes 38 in 5.28s.
- The first combined HTTP check passes 38 ledger cases but has 9 setup errors in
  5.56s because an imported fixture's `env` dependency was absent. No HTTP
  assertions ran. Explicit fixture registration gives 47 passes in 8.58s.
- The first expanded check has 152 passes/3 failures in 18.55s: its pre-commit
  synthetic response was already closed before the replacement stream was
  installed. Starting with an open ByteStream preserves the original no-replay
  and single-close assertions; the repair passes 155 in 20.29s.
- Three sticky-disclosure controls fail in 0.73s against the original publisher:
  it checked capture/primary eligibility but omitted included context floors.
  Publication now runs the complete admitted-request currentness check inside
  the same transaction. These unchanged controls pass in 0.61s. Frozen original
  publisher and controls are included here.

The broader existing finalized/window/provider/disclosure/wake/native-permission/
passive-owner regression passes **1,008 cases in 302.27s**, with one existing
Starlette deprecation warning. All 877 Python inputs remain unchanged. Counts
overlap the focused matrix and are not additive. Ruff lint passes and all 869
in-scope files pass the formatting check; the staged diff check passes.
No live model call, physical-device, acoustic/latency, dependency, installed-owner,
host-policy or SDK change is included. Original model output remains diagnostic;
architectural correctness is the acceptance criterion.
