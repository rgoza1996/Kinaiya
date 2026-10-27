# Initial threat model and release limits

The trusted boundary is the session service, Agent supervisor, policy broker and owner
control plane. Model text, skills, tool output and generated extensions are untrusted.
Only authenticated service credentials establish journal provenance. A user's model
cannot manufacture runtime evidence by emitting JSON or claiming success.

Service credentials are distinct random secrets in a mode-0600 local file. Services
bind loopback by default. Browsers need an explicitly permitted origin. This initial
single-owner deployment does not claim multi-user authorization or mobile pairing.
The device administrator can modify installed software. That is outside the boundary.

Generated code must execute in an OS sandbox with no core/evaluation/credential mounts,
no network and no supervisor IPC. Missing isolation fails closed; no subprocess-only
production guarantee. Workspace permissions never grant writes to core or evaluation
assets. Mounts and symlinks need adversarial tests, not only string path checks.

Contributor receives only public source and synthetic fixtures in this development
run. Provider profiles must explicitly authorize cloud disclosure. No private Korina
source, user conversations, emails, names, documents or credentials enter prompts.
The prototype was inspected by the lead for architectural lessons without copying it.

The run-local Meta proxy admits only Contributor, clamps every output allocation,
reserves the full catalog context at twice verified Contributor rates without cache
discounts, preserves an ordinary-Spark comparison separately, and closes admission before
$22 including unsettled reservations. A $3 buffer remains inside the user's $25 cap.
Actual provider token usage and cached tokens are reported separately from calculated
cost. Provider billing receipts are unavailable; catalog-derived dollars are estimates.
Requests bypassing the proxy are forbidden for development workers. No Wonder calls
are authorized by the current implementation assignment.

The proxy supports only the metered `POST /v1/responses`, read-only model discovery
and its local budget endpoint. A nonblocking OS file lock owns the ledger for the
server lifetime; writes use an owner-only temporary file, fsync and atomic replace.
Restart reconciliation preserves prior admission charges and every unresolved
reservation, including requests whose usage is absent or malformed. Only complete
nonnegative integer input/output counters (with consistent legacy aliases and valid
cached-token counts) release a reservation. The persisted usage is normalized to
counts, excluding arbitrary upstream debug fields. This is development admission
control, not a provider invoice, multi-user quota service or protection against a
device administrator altering the ledger. Offline regression tests use synthetic
upstreams; no credentials or paid inference are needed.

Benchmark methodology: synthetic fixtures, independent supervisor evidence, same
model/tool/token limits for comparisons, and explicit sample counts. Generated/decoded
audio timing is distinct from audible playback. Never label server timestamps as
audible latency without a client playback acknowledgment.
