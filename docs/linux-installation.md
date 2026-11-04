# Native Linux installation and recovery (issue #261)

This is a **candidate installer**, not a production qualification or a claim that
all #261 acceptance gates have passed. Releases remain experimental. The stable
channel requires an explicitly approved release; an absent stable release is an
error, never permission to install `main`, an alpha, or nightly. See
[release policy](releases.md) for publication/signing gates.

## First-use quickstart

Every command below exists on the current `main` CLI. Items marked *planned* are
in open pull requests and are **not shipped**; do not rely on them yet.

```sh
# 1. Bootstrap (prints the plan and asks before changing anything; never run as root):
curl -fsSL https://raw.githubusercontent.com/rgoza1996/Kinaiya/main/install.sh | sh -s -- --channel nightly
export PATH="$HOME/.local/bin:$PATH"

# 2. Configure (the bootstrap normally launches this wizard for you):
kinaiya setup

# 3. Start the loopback host (Session + Agent; Converse/Decisions only if enabled):
kinaiya serve host

# 4. In a second terminal: export the owner sign-in file, then serve the web UI.
kinaiya owner-login   # writes data/owner-login.txt with mode 0600; token is not printed
kinaiya web           # http://127.0.0.1:8780
```

- **Location.** The default layout is described in [Locations and PATH](#locations-and-path).
  Custom absolute `--workspace` / `--data-dir` paths (spaces allowed) must be
  disjoint and symlink-free. *Planned:* choosing the invoking directory as the
  install root (#383).
- **Provider and API key.** Choose a measured Agent adapter and an explicit model.
  For a cloud provider, setup records only an environment variable **name**
  (`--credential-env MY_PROVIDER_KEY`) and your disclosure consent; export the key
  from your protected secret mechanism before `serve host`. Never paste a key into
  a command line or prompt. *Planned:* masked key entry saved to a private local
  file (#389) and a searchable model picker (#390).
- **Configured is not tested.** Setup reports `ready: false` and
  `provider_connection: not_checked`; it does not contact the provider. Your first
  real request is the first live inference. *Planned:* an explicit bounded
  connection check (#391) and `kinaiya doctor` (#392).
- **Owner credentials.** Internal service tokens in `data/service-tokens.json`
  (mode 0600) are managed by Kinaiya; do not edit or share them. `owner-login`
  exports only the owner credential for the local web UI. *Planned:* automatic
  one-time local sign-in (#393, #396), owner-credential rotation (#394), and a
  single `kinaiya start` command (#395).

## Candidate baseline and prerequisites

- Native Ubuntu **22.04, 24.04, or 26.04**, `x86_64` or `aarch64`.
- Python **3.11–3.14**, with matching `venv` and `ensurepip`. Ubuntu 22.04's
  default Python 3.10 is insufficient; select an explicitly installed interpreter.
  Ubuntu 26.04's default Python 3.14 is accepted; if its venv support is missing,
  the installer plans `python3.14-venv` under the same no-upgrade policy below.
- `bubblewrap`, `libseccomp2`, at least **1 GiB** free for Agent-only staging,
  working GitHub/PyPI HTTPS access, and a writable private user home.
- The actual bubblewrap namespace probe must succeed. A container/VPS kernel may
  deny it even with bubblewrap installed. Fix the host policy with the host
  administrator, or use a supported host; **never disable isolation** to install.
- **Ubuntu 24.04/26.04** enables `kernel.apparmor_restrict_unprivileged_userns=1`
  by default. If the installed bubblewrap profile does not permit the probe, do
  not set that sysctl to `0` globally; have an
  administrator review and load the bwrap-only profile in
  [`docs/examples/apparmor-bwrap`](examples/apparmor-bwrap)
  (`sudo install -m 0644 docs/examples/apparmor-bwrap /etc/apparmor.d/kinaiya-bwrap`
  then `sudo apparmor_parser -r /etc/apparmor.d/kinaiya-bwrap`). It permits only
  `/usr/bin/bwrap` to create user namespaces; the restriction stays on for every
  other program.
- Windows, macOS, WSL, other distros, Python 3.15+, GPUs and optional voice/model
  backends are not qualified by this installer.

Ubuntu 26.04/Python 3.14 support requires a release containing this host-admission
change as well as an updated bootstrap. Older nightlies still reject the host
during the bundled runtime's preflight even when a newer `install.sh` accepts it.
Do not edit a downloaded release or bypass its checks; publish a new qualified
nightly through the existing release workflow. Accepting this candidate baseline
does not qualify optional voice/model wheels for Python 3.14.

The installer never runs `sudo apt-get update` or `sudo apt-get upgrade`, never
upgrades, downgrades, or removes existing system packages, never changes kernel
policy, and never starts inference, captures a microphone, or submits an Agent
task. It refuses to run as root. Because `apt-get --no-upgrade` alone only skips
upgrades for packages explicitly named on the command line (and can still upgrade
already-installed transitive dependencies), when required host packages
(`bubblewrap`, `libseccomp2`, or `python3.<minor>-venv`) are missing, `install.sh`
and `kinaiya install` first run a non-mutating `apt-get -s --no-upgrade --no-remove
-o APT::Get::Upgrade=false -o APT::Get::Remove=false
-o APT::Get::Allow-Downgrades=false -y install -- <missing>` simulation against the
`dpkg` status snapshot, verify that every `Inst` record is a newly installed
package with zero upgrades, removals, or downgrades, and bind the exact approved
`package=version` pairs to `sudo apt-get install`. If an existing system component
(`Ubuntu` release `< 22.04`, `Python < 3.11`, `bubblewrap < 0.6.1`,
`libseccomp2 < 2.5`, a transitive dependency requiring an upgrade/removal/downgrade,
or an out-of-date local package index) blocks installation, the CLI and setup
wizard stop before any mutating command and warn the user to update that specific
component first rather than updating the system automatically.

## Single-command install & shell entry point

Run the standalone root `install.sh` in a single command (it handles prerequisite
checks, solver-verified missing dependency installation with pinned
`package=version` pairs, verified release bundle download, isolated `.venv`
creation, hash-locked runtime wheels, the `~/.local/bin/kinaiya` launcher, and
interactive `kinaiya setup` via `/dev/tty`):

```sh
# Single-command install (experimental nightly channel):
curl -fsSL https://raw.githubusercontent.com/rgoza1996/Kinaiya/main/install.sh | sh -s -- --channel nightly

# Or from a downloaded/reviewed copy or release source bundle:
sh install.sh                         # stable, interactive consent + setup
sh install.sh --channel nightly       # explicit experimental nightly
sh install.sh --yes --no-setup        # unattended code install only
KINAIYA_PYTHON=python3.12 sh install.sh --channel nightly --yes --no-setup
# Optional initial paths must be absolute, disjoint, and free of symlink aliases:
sh install.sh --workspace "$HOME/My Work" --data-dir "$HOME/Private Kinaiya Data"
```

When `KINAIYA_PYTHON` is not set, `install.sh` automatically selects a supported
Python `3.11`–`3.14` interpreter (`python3`, `python3.14`, `python3.13`, `python3.12`, or
`python3.11`) if installed. The release source allowlist includes root
`install.sh`, `services/distribution/*`, and `packages/distribution/*`.

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

For a new configuration, choose one setup form:

Installer, CLI and setup confirmations accept `y`/`n` and `yes`/`no`, without
regard to case. Prompts show `(y/n) [n]`; Enter declines. Setup repeats an invalid
boolean answer and cancels on closed terminal input before saving configuration
or credentials. The installer and CLI retain the existing EOF exception only
when installation-location selection already recorded explicit consent.

```sh
kinaiya setup
# Or noninteractive Agent-only setup; credentials are VARIABLE NAMES, not keys:
kinaiya setup --yes --adapter lmstudio \
  --endpoint http://127.0.0.1:1234 --model explicit-local-agent
# Or a cloud Agent, with its own explicit cloud setting and credential reference:
kinaiya setup --yes --adapter meta \
  --endpoint https://agent.example.invalid/v1 --model explicit-cloud-agent \
  --allow-cloud --credential-env AGENT_API_KEY
# Or independent providers for optional Converse text services and Decisions:
kinaiya setup --yes --adapter lmstudio \
  --endpoint http://127.0.0.1:1234 --model explicit-local-agent \
  --converse-adapter compatible \
  --converse-endpoint https://converse.example.invalid/v1 \
  --converse-model explicit-converse --converse-credential-env CONVERSE_API_KEY \
  --converse-allow-cloud \
  --decisions-adapter compatible --decisions-endpoint http://127.0.0.1:8080/v1 \
  --decisions-model explicit-decisions
# After the chosen setup:
kinaiya serve host
```

Replace illustrative endpoints/model names with your selected provider settings.
The setup adapter choices are:

| Role | Adapter option | Supported values |
| --- | --- | --- |
| Agent | `--adapter` | `lmstudio`, `meta` |
| Converse | `--converse-adapter` | `lmstudio`, `compatible`, `meta` |
| Decisions | `--decisions-adapter` | `compatible`, `laya`, `native_d1`, `meta` |

Supplying any `--converse-*` option requires its own `--converse-endpoint` and
`--converse-model`; any `--decisions-*` option requires its own
`--decisions-endpoint` and `--decisions-model`. Their `--converse-credential-env` / `--decisions-credential-env`
and `--converse-allow-cloud` / `--decisions-allow-cloud` settings are independent
of Agent settings. Explicit role profiles do not inherit Agent cloud permission
or credential references. The wizard asks for each role separately, defaulting
to no cloud permission and no credential reference.

Bare `--converse` retains its existing implicit provider defaults. All effective
enabled profiles, including those defaults, are validated before directories,
private tokens or configuration are written. For example, a loopback WebSocket LM Studio Agent
endpoint needs an explicit HTTP(S) Decisions profile when its implicit
`compatible` profile would be invalid; refusal leaves a fresh setup untouched.

Setup reuses native `kinaiya init`, creates private service credentials/config,
and retains the native default policy: no general writes, shell, network or YOLO.
Existing config is preserved and never reinitialized silently. A credential value
must not be pasted into prompts/arguments; only an environment variable reference
is written to config. Load credential values through your protected host secret
mechanism, not into conversation or shell-history arguments.

Agent-only `serve host` starts **session + Agent**. `setup --converse`, an explicit
Converse profile, or affirmative wizard selection adds Converse text + Decisions
on the next `serve host`. A Decisions profile without a Converse profile or
`--converse` does not enable Converse.
No `voice`/`resources` declaration, model/voice download or license consent is
inferred. Optional model/voice installation is a separate explicit developer path
with its own license/download-size review; see [Converse setup](converse-setup.md).
The existing developer `serve all` behavior is unchanged; it is not the
Agent-only default.

Setup reports `ready: false` and `provider_connection: not_checked`. It does not
contact a provider or claim the selected model exists/is loaded. Validate the
chosen provider/model and local service readiness explicitly before authorizing
work. Saving a provider profile does not replace native content-scope and task
permission checks. No automatic test task is
submitted to make setup look successful.
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

## Troubleshooting and small recovery paths

Each path preserves your journal, configuration, workspace and owner credential.
None of them requires editing JSON or `.env` files by hand.

| Symptom | What to do |
| --- | --- |
| **Unsupported host or sandbox refused** (bootstrap/setup preflight stops; `bwrap` namespaces denied) | Use a qualified host from [prerequisites](#candidate-baseline-and-prerequisites). On Ubuntu 24.04, install the narrow AppArmor profile described there; do not disable AppArmor or run as root. Rerun the same `install.sh` command afterwards. |
| **Install destination inaccessible or protected** | Choose an absolute, user-owned, symlink-free path outside the Agent workspace and outside system directories, then rerun with `--workspace` / `--data-dir`. Nothing is written when preflight refuses. |
| **Model endpoint unavailable** | Start or fix the provider (for local LM Studio, load the model and confirm the loopback endpoint), then restart `kinaiya serve host`. Setup's saved configuration stays valid; to change the endpoint or model, rerun `kinaiya setup`. |
| **Invalid or missing API key** | Export the correct value into the environment variable named by `--credential-env` in the shell that runs `kinaiya serve host`, then restart it. To change which variable is used, rerun `kinaiya setup`. Never pass the key as an argument. |
| **Missing web assets** (`kinaiya web` refuses) | The matching release has no prebuilt UI. Install a release that includes it (`kinaiya update --plan`, then `kinaiya update --yes`), or follow the developer path in [Owner web client](#owner-web-client) in a separate checkout. Kinaiya never downloads Flutter for you. |
| **Interrupted install or update** | Stop managed services, then run `kinaiya recover --yes`. It reconciles the pending transaction without deleting the active generation or data. If a release is broken, `kinaiya rollback --generation <id> --yes` restores retained code/runtime only. |
| **Lost owner sign-in file** | Run `kinaiya owner-login --output <new-path>`. It creates a new 0600 file and refuses to overwrite an existing one; internal service tokens are unchanged. Delete stale exports you no longer need. |

Rerunning `install.sh` or `kinaiya setup` on an existing installation preserves
existing configuration and refuses silent reinitialization.

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
