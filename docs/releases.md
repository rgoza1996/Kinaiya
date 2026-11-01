# Release engineering: stable and nightly (#262)

This is an experimental distribution contract, not a production qualification.
A successful build does not close #11, #13, mobile-calling, provider, or clean-device
acceptance gates. Android/iOS publication and installation evidence are separate
from Linux/web publication. Debug APKs and unsigned simulator archives are not
signed release artifacts or proof of store/tester distribution.

## Identity and selection

`packages.distribution.releases.resolve_release(channel, fetch_json=None)` returns
a validated manifest dictionary, not an arbitrary checkout or an installation.
The optional callable is `fetch_json(url) -> decoded JSON`, for offline tests.
It must propagate HTTP/network failures rather than returning an empty feed.

- **Stable:** only the official repository's GitHub REST `/releases/latest`.
  Drafts, prereleases, alpha tags and non-final versions are rejected. No published
  stable release means no stable installation; there is no fallback to `main`.
- **Nightly:** published prereleases with the exact immutable tag
  `nightly-<build_number>-<source_sha first 12 hex characters>`. Old alpha tags,
  drafts and stable releases do not become nightly candidates. Select the largest
  unique build number; duplicate build identities or a malformed nightly tag are
  errors, not a reason to silently install an older release.
- The nightly scan covers at most five pages of 100 releases. Reaching a full
  final page is an explicit error because the global selection is incomplete.
  Operators must keep the active release inventory within this bound. The resolver
  does not trust pagination links or silently truncate the feed.
- The release's exact target SHA, tag, channel, official origin and uploaded assets
  must agree with the manifest. Missing/duplicate/inconsistent assets fail closed.

`ReleaseError.code` distinguishes `release_absent` (nothing qualified),
`feed_unavailable` (network/HTTP outage or rate limit), `invalid_release` (malformed,
ambiguous or incomplete metadata), `unsupported_platform`, and `stale_release`.
These are not interchangeable with an installed release being current.
`update_status(manifest, installed_build)` returns `available` or `current` and
refuses an older build. Consumers must persist a per-channel high-water mark;
this helper is not a trusted clock, an expiry service or a revocation feed.
A first installation cannot detect replay of an otherwise valid old release.

### Manifest v1

`release-manifest.json` is bounded to 1 MiB. Required identity fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Exactly integer `1`; booleans are not integers |
| `channel` | `stable` or `nightly` |
| `version` | Stable `X.Y.Z`; nightly `X.Y.Z-nightly.BUILD+SHA12`, no leading-zero version components |
| `build_number` | Positive integer, at most signed 64-bit maximum |
| `source_sha` | Exactly 40 lowercase hexadecimal characters |
| `protocol_min`, `protocol_max` | Positive compatibility-generation integers, ordered inclusive range |
| `state_schema` | Positive distribution state-compatibility generation, not a promise of an automatic DB migration |
| `min_updater` | Positive minimum updater-contract generation |
| `artifacts` | 1–32 unambiguous artifact records |

Each artifact has `name`, `platform`, `architecture`, `url`, `sha256`, and `size`.
Names are bounded portable basenames; URLs must be exact HTTPS GitHub download
URLs under `rgoza1996/Kinaiya`, using the identity's immutable tag and basename.
Digests are lowercase SHA-256; size is positive and at most 512 MiB. Platforms are
`linux`, `web`, `android`, `ios`; architectures are `any`, `x86_64`, `aarch64`,
`arm64`. A target `(platform, architecture)` and basename may appear only once.
A Linux `any` `*-source.tar.gz` asset is mandatory. Publishing additionally requires
the matching `web/any` archive. `select_artifact` selects an exact architecture
before the same platform's `any` asset, or refuses the unsupported platform.

The initial publisher declares protocol generation 1, state generation 1, updater
generation 1. These numbers do not assert compatibility with every historical
client, SQLite layout or provider. A future protocol/state transition requires
qualification and a versioned change, not simply editing metadata to bypass a
consumer refusal. Host/client compatibility checks and platform-specific
instructions belong to their consuming runtime/client implementations.

## Trust and integrity

The baseline metadata authority is **the official GitHub repository and HTTPS/TLS**.
Repository administrators, GitHub, Actions and their release permissions are
inside that trust boundary. SHA-256 and size detect changed/truncated bytes;
a digest downloaded beside an artifact is **not independent publisher identity**.
A compromised repository/workflow could replace both. We do not label this as an
offline independently signed update system or claim protection from that authority.

The built-in public transport pins official API paths and immutable asset URLs,
uses a 20-second socket timeout, limits JSON to 1 MiB, rejects duplicate keys, and
allows at most three HTTPS asset redirects to the exact GitHub CDN hosts
`release-assets.githubusercontent.com` or `objects.githubusercontent.com`.
API redirects, credentials in redirect authorities, non-HTTPS and other hosts
are rejected before following them. No GitHub/user token is sent by this transport.
`verify_artifact` streams a size-bounded digest check; consumers must do this before
opening archives/executing code, in private staging storage that cannot be swapped
by another local process. OS administrator compromise is outside this boundary.

The privileged workflow also emits GitHub OIDC/Sigstore build-provenance
attestations for every staged file, including the manifest and release notes,
and verifies them before publication. To enforce that additional authority as a
consumer, use a supported GitHub CLI and reject verification failure:

```bash
gh attestation verify release-manifest.json \
  --repo rgoza1996/Kinaiya \
  --cert-identity 'https://github.com/rgoza1996/Kinaiya/.github/workflows/distribution.yml@refs/heads/main' \
  --deny-self-hosted-runners
# Repeat for the selected archive, then check its manifest digest and size.
```

The resolver itself enforces repository/TLS authority, not the optional CLI
attestation policy. A consumer requiring attestations must verify **both manifest
and selected artifact**; no fallback on missing `gh`, missing attestation, network
failure or invalid signature. The signing workflow runs from main, which can
be different from an approved stable source tag; do not incorrectly equate its
OIDC workflow-source digest with the manifest's target-source digest. The workflow
gates and signed manifest bind the latter. Dry-run artifacts are deliberately
unattested and never represented as authenticated public releases.

## Deterministic bundling and bounded validation

Run from a repository containing the committed Linux runtime lock:

```bash
python -m scripts.build_release \
  --output dist --channel nightly --version 0.1.0 --build-number 123 \
  --source-sha "$(git rev-parse HEAD)" \
  --web-dir apps/kinaiya_flutter/build/web \
  --web-source-sha "$(git rev-parse HEAD)"
python -m scripts.build_release --validate dist
```

The example build number is for a **local, unpublished** staging test only. The
workflow uses the distribution workflow's increasing `GITHUB_RUN_NUMBER`. A
rerun reuses its identity and cannot overwrite an existing release/draft/tag.
Do not reset this workflow's sequence or reuse an old build number. Stable uses
an explicitly approved, existing final `vX.Y.Z` tag. The base version must match.

The source archive contains flat repository-relative paths (no enclosing directory),
`release-identity.json`, `release-inventory.json`, the runtime lock, and matching
prebuilt web files under `apps/kinaiya_flutter/build/web/`. The standalone web archive
has `index.html`, `main.dart.js`, assets and `release-identity.json` at its root.
The inventory records included file digests/modes and the exact runtime-lock digest.
It is a source/dependency-input inventory, **not** a complete SPDX/CycloneDX SBOM or
proof that optional model/provider assets have all been redistributed/licensed.

Only committed Git blobs are source inputs, not modified/untracked working files.
Allowed runtime roots are `core`, `services`, `packages`, `harnesses`, plus
`evaluation/harness` and its package initializer, with `.py`, `.json`, `.txt`, `.patch`
suffixes. Reviewed root files are `pyproject.toml`, `requirements-runtime.lock`,
`LICENSE`, `README.md`, `install.sh`. Hidden/cache files and conventional non-code
config/credential/secret/token filenames are excluded. Data/workspace directories,
`.env`, private keys, developer configs and environment dumps are not included.
This is not a general secret scanner: approved source must still be reviewed for
accidentally committed secrets. The clean CI builder receives no publishing token,
OIDC privilege or signing/store secrets.

Gzip timestamp/name and tar ownership, timestamps, modes and ordering are normalized.
Given the same committed blobs, Python/zlib toolchain, identity and compiled web
bytes, the archives/manifest are byte-for-byte reproducible. The workflow bundles
twice and compares them. This is **not** a claim that two independent Flutter
compilations are byte-identical: the SDK commit and pub lock are pinned and the
compiled web tree is recorded as an input. Missing runtime lock, malformed identity,
missing compiled web files, mismatched declared web source, or existing output
are explicit build refusals. A source-only local build is available by omitting
web arguments; the publisher will not accept it.

Selected source symlinks/submodules and all web symlinks are rejected. Tar entries
are regular files only, with portable relative names: no traversal, absolute paths,
Windows aliases, duplicate/case-colliding members or sparse/link entries. Limits:
10,000 files, 64 MiB/file, 512 MiB total uncompressed input, 512 MiB/asset. Local
bundle validation checks digests, archive identity, required files and bounded
archive members without extraction. Installers must independently enforce their
own extraction boundary; successful publisher validation does not authorize an
unsafe `extractall` on later downloaded bytes.

## Workflow modes and promotion

`.github/workflows/distribution.yml` has a read-only qualification job, an
unprivileged build job, and a separate protected publishing job. Actions and
Flutter are commit-pinned; Python and runtime/development dependencies use locks.

1. **Nonpublishing manual validation (default):** dispatch with `dry_run=true`,
   `channel=nightly`. A feature-branch build is allowed. It builds/tests the real
   web client, bundles twice, validates, and retains a workflow artifact for 30
   days. It does not create a release/tag, attest, access a protected environment,
   use signing/store credentials or claim stable promotion. GitHub requires the
   workflow file on the default branch before it can be manually dispatched;
   local CLI/tests are available before merge. No remote dispatch was run as part
   of implementing this change.
2. **Nightly publication:** a successful official-main `push` run of
   `deterministic-ci`, or manual non-dry dispatch on main. Exact source must still
   be current main and its latest CI attempt must have succeeded. Fork/PR/failed
   events do not qualify. There is no automatic alpha promotion or nightly build
   after an arbitrary scheduled checkout. This workflow does not promise daily
   mobile availability or store processing.
3. **Stable publication:** manual non-dry dispatch on main, a matching final tag
   whose commit is on main with successful exact-source CI, plus an independent
   reviewer in the protected `release-stable` environment. Older successful CI
   cannot hide a newer failed rerun. Approval and eligibility are checked again
   after the environment gate.

Publication is disabled until an administrator explicitly sets repository variable
`RELEASE_PUBLISH_ENABLED=true`, protects main and configures `release-nightly` and
`release-stable` environments for protected branches only. Stable must have
required reviewers and prevent self-review. Missing settings/API access fail
closed; the workflow does not create a missing environment and call it approved.
Configure GitHub release immutability/tag protections as defense in depth. The
workflow refuses to replace releases, drafts or nightly tags even if server-side
immutability has not been enabled. Repository admins remain an authority.

Only the publisher has `contents:write`, `attestations:write` and `id-token:write`.
It revalidates the candidate, attests all files, stages a **draft**, downloads the
exact staged bytes, compares/validates them and verifies their attestations. Only
then does it publish and set stable's latest marker (nightly never becomes latest).
It reads back the exact published release and checks identity, channel, complete
asset names/URLs/sizes and upload state. A failed upload/verification leaves a
noninstallable draft. Do not blindly rerun/reuse that identity: inspect and retain
failure evidence, remove the draft only through an explicitly authorized recovery,
or make a new workflow run/build identity. No automatic deletion or clobbering.

## Retention, rollback and remaining qualification

Retain published immutable nightlies for at least 30 days and any release referenced
by an open incident, plus the current/previous qualified stable releases, source
commits, locks, web input digests, workflow logs, attestations and migration notes.
The workflow artifact retention is 30 days; published releases are not automatically
deleted. Review retention before the resolver's 500-release bound is reached.

For revocation, withdraw the affected release from channel eligibility (draft/delete
under explicit incident authority), record affected tags/digests and notify users.
Existing installations are not remotely disabled. There is no signed revocation
or expiry feed in v1. A rollback/channel switch requires a known compatible state
schema, backups and explicit recovery approval; never automatically decrement a
build/schema high-water mark or open newer state with an older runtime. Prefer a
new qualified release/build with a fix or documented migration/recovery path.
Store-managed Android/iOS rollouts/withdrawals and app-signing identities follow
separate platform procedures and cannot be rolled back by changing this Linux feed.

**Not verified by these offline tests:** public staging publication/download/install,
real OIDC issuance and negative-signature verification against published candidates,
clean Linux consumption of an integrated source/web release, signed Android tester
installation/update, or iOS upload/processing/TestFlight installation. The delegated
implementation was told no repository secrets/environments were configured; store
and protected-promotion acceptance therefore remain blocked, not passed. Complete
these with authorized accounts/credentials and record each platform separately.

Verification traceability: `test_distribution_releases.py` covers selection,
validation, absent/outage distinction, origin/redirect policy, digest/size and stale
build refusal; `test_distribution_build.py` covers real Git fixtures, deterministic
archives, tracked/untracked secret boundaries, resource/path/link refusal and CLI;
`test_distribution_gate.py` covers exact-source CI and approval/dry-run rules;
`test_distribution_workflow.py` guards the unprivileged/privileged job boundary and
draft-first/read-back ordering. None is a substitute for the blocked live gates.
