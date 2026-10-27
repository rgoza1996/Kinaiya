# ADR 0009: Skill controller, task pins and disclosure grants

The controller in `services/agent/skill_service.py` extends ADR 0007 for issue
#28. Agent/API/command integration is described in ADR 0010. This remains an
experimental Linux implementation, not the complete M1A compatibility gate.

A no-follow, owner-only regular lockfile and nonblocking `flock` permit one
live controller per protected root. An asyncio lock serializes lifecycle,
scopes and pins. The root must be disjoint from Agent's workspace before any
control file is created. The base Agent does not import the optional YAML
skill dependency until skills are explicitly configured.

Owner grants are `local_only` by default, `public_synthetic`, or
`private_cloud_authorized`. Grants bind the exact whole-package digest.
Updating bytes without another explicit grant makes the new version local
only. Active older pins retain their separately reviewed grants across an
explicitly scoped update; reviewing a new public version cannot disclose an
older private pin. Explicit scope changes discard historical grants. Current
revocation and each pinned digest are checked before every read and before
subsequent Agent inference. Already transmitted in-flight requests cannot be
recalled by revocation.

Scope persistence uses a durable intent marker before atomic replacement.
An unfinished `scope-change.pending` refuses restart until the owner inspects
persisted scopes and clears the marker. Write failures restrict the affected
in-memory grant and return an error. Failure before an intent marker can be
written cannot establish durable revocation; the requested change is not
acknowledged. Registry, directory and scope updates are not one transaction.
A changed digest without a matching persisted grant remains local only.
No automatic crash repair or administrator-tamper defense is claimed.

Every Meta adapter counts as Contributor, including model aliases. Contributor
receives only explicitly public/synthetic packages. Other configured cloud
providers require an appropriate owner grant; local models may load eligible
local content. Model catalog filtering precedes exporting ids/descriptions.
Manual-only and author-disabled model invocation are enforced in code.

The first selected load captures verified package bytes for a task/skill.
Later loads use that snapshot through updates and rollback. Current and pinned
author restrictions both apply. Removal reports active dependent tasks and
refuses until their pins are released. Runtime finish/failure/cancellation
releases pins; recovered uncertain tasks stay paused and never resume them.
Bounds are 32 pins and 32 MiB retained. Returned data cannot mutate stored
snapshots. Resources are UTF-8 text with exact path/hash; no code executes on
load. Owner pages contain at most eight records, model pages at most four
records/1600 JSON characters, and loads at most 12000 JSON characters. The
Agent uses a slightly smaller bound to preserve its observation envelope.
Oversized content refuses explicitly; selecting a small resource remains
possible and revocation checking does not reload an oversized body.

Verification: 19 controller tests and 17 Agent/boundary tests exercise real
synthetic files and journal operations. Added lead regressions cover grants
across changed/private versions, failed revocation across restart, root
intersection and hardlinked locks, large catalogs, cancellation and partial
content selection. The 53 foundation tests remain intact. Full offline Python
verification passed 289 tests. Real Contributor task evidence is in
`evaluation/evidence/skills-2026-10-27.json`; it proves one synthetic procedure,
not upstream conformance, dependency execution or autonomous capability
acquisition.
