# Native Linux installation and recovery (issue #261)

This is a **candidate installer**, not a production qualification or a claim that
all #261 acceptance gates have passed. Releases remain experimental. The stable
channel requires an explicitly approved release; an absent stable release is an
error, never permission to install `main`, an alpha, or nightly. See
[release policy](releases.md) for publication/signing gates.

## Candidate baseline and prerequisites

- Native Ubuntu **22.04 or 24.04**, `x86_64` or `aarch64`.
- Python **3.11–3.13**, with matching `venv` and `ensurepip`. Ubuntu 22.04's
  default Python 3.10 is insufficient; select an explicitly installed interpreter.
- `bubblewrap`, `libseccomp2`, at least **1 GiB** free for Agent-only staging,
  working GitHub/PyPI HTTPS access, and a writable private user home.
- The actual bubblewrap namespace probe must succeed. A container/VPS kernel may
  deny it even with bubblewrap installed. Fix the host policy with the host
  administrator, or use a supported host; **never disable isolation** to install.
- Windows, macOS, WSL, other distros, Python 3.14, GPUs and optional voice/model
  backends are not qualified by this installer.

The installer never executes `sudo`, changes kernel policy, installs system
packages, starts inference, captures a microphone or submits an Agent task.
Install prerequisites separately with explicit administrator authorization, e.g.
`sudo apt-get install bubblewrap libseccomp2 python3.12-venv` on a suitable Ubuntu
24.04 host. It refuses to run as root.

## Shell entry point

Obtain the standalone root `install.sh` from an owner-reviewed immutable repository revision
or the matching release source bundle (`scripts/install.sh` is a source-checkout alias).
The release source allowlist already includes root `install.sh`, `services/distribution/*`
and `packages/distribution/*`; no helper is fetched from `main` at runtime. Review its contents before running it.
It is self-contained: it does not download Python installer code from a mutable
branch. Do not advertise a `curl main | sh` command as a verified installation.

```sh
sh install.sh                         # stable, interactive consent + setup
sh install.sh --channel nightly       # explicit experimental nightly
sh install.sh --yes --no-setup         # unattended code install only
KINAIYA_PYTHON=python3.12 sh install.sh --channel nightly --yes --no-setup
# Optional initial paths must be absolute, disjoint, and free of symlink aliases:
sh install.sh --workspace "$HOME/My Work" --data-dir "$HOME/Private Kinaiya Data"
```

Preflight occurs before feed resolution, cache creation or runtime publication.
The bootstrap resolves official release metadata, pins its exact 40-character
source SHA and immutable tag, checks asset origin/size/digest, and safely extracts
only bounded regular files/directories. Links, devices, sparse entries,
duplicates and path traversal are refused. The verified source then applies the
shared full release/compatibility contract and stages a managed environment.
No source branch checkout is a fallback.

**Trust limit:** HTTPS to the official GitHub repository plus the manifest's
SHA-256 protects transport/integrity within that publisher trust boundary. It is
not an independently verified publisher signature and does not survive compromise
of that repository/release authority. Stable publication is separately gated by
release qualification and configured signing; this installer does not manufacture
signing secrets or assert that a release has been published.

Locked Agent-only runtime wheels are installed with hash verification in a
per-generation `venv`; development, voice and model extras are excluded.
Preparation imports the native runtime to catch missing dependencies before the
switch. This is an import health check, **not** a live provider or service-readiness
qualification. Dependency failures preserve the previous generation.

## Locations and PATH

| Purpose | Default |
| --- | --- |
| Trusted code, runtime, receipts, update lock | `~/.local/share/kinaiya/install` |
| User data, service credentials, journals | `~/.local/share/kinaiya/data` |
| Configuration | `~/.config/kinaiya/config.json` |
| Downloads/cache | `~/.cache/kinaiya` |
| Authorized Agent workspace | `~/Kinaiya Workspace` |
| Stable launcher | `~/.local/bin/kinaiya` |

The managed launcher quotes paths with spaces and runs from its own source with
an explicit import root. Add `export PATH="$HOME/.local/bin:$PATH"` to your shell
configuration explicitly; the installer does not edit shell startup files.
Installed core, every retained generation, updater/trust metadata, launchers,
configuration, data and cache are native protected roots, not Agent workspace
write authority. Existing unrelated installs/launchers, aliased paths, changed
source/launcher files and incompatible saved layouts are refused rather than
silently overwritten. A local administrator can still alter installed software;
that remains outside the Agent trust boundary.

Initial data/workspace overrides must be repeated on a standalone bootstrap rerun.
Managed setup cannot relocate an already installed layout: that would leave a
stale launcher. Keep the saved layout, or use a separately reviewed relocation.

## Setup and launch

```sh
kinaiya setup
# Noninteractive: credential references are environment VARIABLE NAMES, not keys.
kinaiya setup --yes --endpoint http://127.0.0.1:8080/v1 --model explicit-local-model
# Cloud requires affirmative disclosure consent and HTTPS:
kinaiya setup --yes --endpoint https://provider.example/v1 --model explicit-model \
  --allow-cloud --credential-env MY_PROVIDER_KEY
kinaiya serve host
```

Setup reuses native `kinaiya init`, creates private service credentials/config,
and retains the native default policy: no general writes, shell, network or YOLO.
Existing config is preserved and never reinitialized silently. A credential value
must not be pasted into prompts/arguments; only an environment variable reference
is written to config. Load credential values through your protected host secret
mechanism, not into conversation or shell-history arguments.

Agent-only `serve host` starts **session + Agent**. `setup --converse` or affirmative
wizard selection adds Converse text + Decisions on the next `serve host`.
No `voice`/`resources` declaration, model/voice download or license consent is
inferred. Optional model/voice installation is a separate explicit developer path
with its own license/download-size review; see [Converse setup](converse-setup.md).
The existing developer `serve all` behavior is unchanged; it is not the
Agent-only default.

Setup reports `ready: false` and `provider_connection: not_checked`. It does not
contact a provider or claim the selected model exists/is loaded. Validate the
chosen provider/model and local service readiness explicitly before authorizing
work. No automatic test task is submitted to make setup look successful.
The embedded ASCII mascot has plain-text, narrow-terminal and noninteractive
fallbacks. It is an original text adaptation of
`assets/mascot/kinaiya-mascot.png` under the repository Apache-2.0 license, with
hair/crescent/star/robe/ripples retained. Separate provenance of the supplied image
has not been established by this change; owner artwork review remains a gate.

## Owner web client

```sh
kinaiya web          # matching release's prebuilt assets, 127.0.0.1:8780 only
kinaiya owner-login  # exports owner sign-in to a new mode-0600 local file
```

The server exposes only the embedded compiled web root, never source, directory
listings, credentials or logs containing request URLs. Enter the configured
loopback Session/Agent host endpoints in the owner UI and sign in using the
private owner credential file. Do not give this credential to companions or
mobile clients. General companion pairing/remote deployment is not implemented
by this installer; do not expose the loopback servers on a public interface.

If the matching release lacks prebuilt web files, `kinaiya web` refuses without
downloading Flutter. Explicit developer alternative: install a reviewed Flutter
SDK, run `flutter pub get` then `flutter build web` in `apps/kinaiya_flutter`, and
follow that directory's [guide](../apps/kinaiya_flutter/README.md). Do not modify a
managed generation in place: use a separate development checkout. Secure remote
hosting requires a separately configured authenticated TLS reverse proxy and
explicit allowed origins; this change does not qualify a remote-host recipe.

## Check, update and recovery

```sh
kinaiya version
kinaiya update --check                    # read-only metadata/provenance/plan
kinaiya update --plan --channel nightly   # also read-only; no artifact download
# Stop only your managed services explicitly (Ctrl-C in their terminal).
kinaiya update --yes
kinaiya update --yes --channel nightly
kinaiya recover --yes
kinaiya rollback --generation EXACT_32_HEX_GENERATION_ID --yes
```

Checks do not create an install/cache, install dependencies, restart services or
report unavailable/offline/invalid feeds as “current”. Switching channels is
explicit, with identical compatibility and anti-downgrade checks; selecting an
older channel target is refused. Identical build numbers must have identical
immutable manifests, not merely matching version strings.

Managed services and the web server hold a shared lifetime operation lock;
install/update/recovery/rollback/uninstall require its exclusive lock. Port probes
also refuse updates while unmanaged services occupy standard host ports.
No unrecognized process is killed. This conservative implementation **requires
explicit stop and restart**; it does not automate drain, service ownership/PID
management or live readiness/restart. Active tasks are never replayed.

A new generation is downloaded, verified, safely unpacked, dependency-prepared
and import-checked before an atomic Linux `current` symlink switch. Previous
code/runtime remain retained. Dirty source/launcher or unsupported
protocol/updater/state-schema changes are refused before preparation. A pending
journal prevents blind retries after interruption. `recover` reconciles whether
the stage was abandoned or the switch already completed; it does not delete an
active generation just because post-switch cleanup failed. Corrupt/unknown
journals are retained for manual review, not guessed away.

Rollback verifies a retained pristine generation, stops at the same lock/busy
boundary and requires identical declared state-schema compatibility. It restores
**code/runtime only**. It never restores journals or databases. Schema-changing
migrations and backup/restore are **not implemented and are refused**; retaining a
Git SHA is not a database rollback promise. Backup and service-readiness
qualification remain separate release acceptance gates.

## Uninstall

```sh
kinaiya uninstall --yes
# Separate destructive consent is mandatory for private data/config/cache:
kinaiya uninstall --yes --delete-data --confirm-data-deletion DELETE
```

Default uninstall removes managed generations/current launcher but retains data,
configuration, cache and authorized workspace. The workspace is never deleted,
even with data-deletion consent. A small saved layout/receipt and the same lock
inode remain to preserve serialization and safe reruns. Changed/unrelated
launchers are preserved by refusal. Stop services explicitly first.

## Local source smoke without a published release

These commands build a **local, unqualified nightly fixture** from an exact local
commit; they do not resolve/publish a stable release, contact a provider, or bypass
sandbox preflight. Use a new isolated root, never an existing installation:

```sh
python -m pytest tests/unit/test_linux*.py tests/unit/test_distribution*.py -q
python -c 'from pathlib import Path; from services.distribution.installation import Layout, preflight; print(preflight(Layout.for_home(Path.home()/".cache/kinaiya-distribution-260/home")))'
# Stop here if preflight refuses; no installer state has been written by that check.
python -m scripts.build_release --output "$HOME/.cache/kinaiya-distribution-260/bundle" \
  --channel nightly --version 0.1.0 --build-number 1 --source-sha "$(git rev-parse HEAD)"
python -m scripts.build_release --validate "$HOME/.cache/kinaiya-distribution-260/bundle"
python - <<'PY'
import json
from pathlib import Path
from services.distribution.installation import Layout, preflight, install_release, source_artifact
root = Path.home() / ".cache/kinaiya-distribution-260"
bundle = root / "bundle"
layout = Layout.for_home(root / "home")
preflight(layout)  # mandatory: do not replace with a fake/success lambda
manifest = json.loads((bundle / "release-manifest.json").read_text())
install_release(layout, manifest, bundle / source_artifact(manifest)["name"])
print("Installed local fixture, not a published/qualified release:", layout.bin / "kinaiya")
PY
"$HOME/.cache/kinaiya-distribution-260/home/.local/bin/kinaiya" version
```

Building/validating the bundle needs no network. The actual runtime preparation
installs locked wheels from HTTPS PyPI and therefore needs network unless a
separately controlled wheel cache is already available. This smoke does not
start services, inference or tasks. Oracle-style namespace denial is a legitimate
**refused installation**, not a reason to bypass preflight or claim clean launch.

## Verification and remaining qualification

Regression tests are `tests/unit/test_linux*.py`; shared release-contract tests
are `tests/unit/test_distribution*.py`. They cover staging/failure preservation,
unsafe archives/digests/metadata, dirty source, immutable build conflicts,
concurrency, recovery reconciliation, CLI parsing/check-only behavior, setup,
mascot fallbacks and real loopback static HTTP confinement.

Windows exercises platform-independent logic with explicitly documented junction
substitutes. Linux atomic symlink replacement and actual POSIX credential modes
are explicitly skipped there. Mocked preflight failures are not clean-machine
Linux evidence. A real Linux install/dependency build, actual denied sandbox
operations, optional Converse/UI/provider checks and target-by-target readiness
must be recorded separately by the release owner. No cross-architecture/GPU or
full-product qualification is inferred from these tests.
