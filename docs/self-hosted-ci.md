# Trusted self-hosted CI

Infrastructure tracking: #569. Regression qualification: #379. Workflow changes
are proposed through a PR; adding hosts does not enable any production feature.

## Inspected hosts

Read-only inspection on November 13, 2026 preceded installation. Both machines
already run Docker, Tailscale, remote desktop and application services.

| Host | Observed OS / CPU | Visible RAM | CI root | Work |
| --- | --- | --- | --- | --- |
| Oracle | Ubuntu 24.04.4, ARM64, four Neoverse-N1 CPUs | 23 GiB | `/srv/kinaiya-ci` | Native Python checks |
| Dell | Ubuntu 26.04, x64, Xeon E3-1271 v3, eight threads | 22 GiB | `/srv/storage/kinaiya-ci` | Native Python, Flutter/web and Android builds |
| GitHub macOS | Existing `macos-15` workflow | Hosted | Disposable | Existing iOS/Xcode builds |

Oracle had about 16 GB free on its root filesystem. Dell's root filesystem had
**zero bytes available to unprivileged users**; its separately mounted data
filesystem had about 1.2 TB free. CI uses that data filesystem. No existing data,
cache, container or application was removed. The Dell RAM observation does not
establish that all of the owner's expected 32 GB is available; hardware/firmware
investigation remains separate maintenance. Do not fill Oracle with Android SDKs.
Dell's existing storage parent is world-writable. The new CI root alone has
the ext4 immutable attribute, protecting its directory entry from rename while
its `home` and `runner` children remain writable. The storage parent's
permissions and existing data are unchanged.

## Admission and isolation

`ci.yml` keeps every `pull_request` job on GitHub-hosted machines. Trusted main
pushes use native ARM64/x64 Python matrices and Dell for Flutter/Android. Manual
`suite=smoke`, `media`, `android` and `full` dispatches qualify these paths. iOS
stays on macOS. The existing mobile emulator/installer/release workflows remain
hosted: they require distinct privileges, OS matrices or publishing credentials
and were not migrated to these persistent hosts.

**YAML conditions are not the security boundary.** A fork can edit workflow
labels. The systemd service always supplies a root-owned job-started hook from
`/etc/kinaiya-ci`; it never executes the policy copy in the checkout. The policy
uses the runner's GitHub context and original webhook, and requires:

- The exact repository ID/name, owner actor ID and matching webhook sender.
- `push` on main, or an explicitly reviewed `workflow_dispatch`.
- An allowlisted workflow filename and matching workflow/source commit.
- Main, or a temporary branch-to-exact-SHA entry installed by the administrator.
- No PR, PR-target, workflow-run, comment, schedule or other indirect event.

The hook fails closed on missing/malformed/oversized input or unsafe policy-file
ownership. On refusal it also kills its verified original `Runner.Worker`
parent, preventing `always()` or action pre/post entrypoints from running after
the refusal. An unexpected worker layout stops only its own exact service
process tree; systemd then restarts it. Refusals/admissions also record only the
public run ID in the host journal, since abruptly stopped workers may not
finish uploading GitHub job logs. Qualification must
include a real rejected PR job, not just tests of the Python predicate.

GitHub downloads/parses action metadata before this hook. This is a narrow
admission control on persistent runners, **not a disposable isolation boundary**
or protection against vulnerabilities in the runner itself. Only reviewed
repository code and pinned actions may be admitted. Do not grant access to
arbitrary PRs or reuse these identities for another repository. Automatic runner
security updates stay enabled. A compromised trusted job requires rebuilding
the account/runner and investigating host exposure; deleting its checkout alone
does not restore trust.

Both hosts use a new `kinaiya-ci` system account with a nologin shell and no
sudo/Docker supplementary groups. No GitHub PAT, application credential, SSH key
or Tailscale auth key is copied to it. Registration tokens travel only over SSH
stdin and are short lived. Runner credentials remain private files in its own
directory. Checkouts set `persist-credentials: false`; tokens are read-only.

The root-owned `kinaiya-ci.service` limits the process tree to two CPU
equivalents, 6 GiB soft/8 GiB hard memory and 2,048 tasks, with lower scheduling
and IO priority. It uses private temporary/device mounts, a read-only host
filesystem, hidden existing home directories, and only its own CI root exposed
under `/srv`. Docker/containerd sockets are inaccessible. It grants no KVM or
container privilege. A separate AppArmor service domain permits the user
namespaces required by bubblewrap without changing the global restriction.

## Installation and tools

Owner approval is required for `scripts/ci/prepare-host.sh`: it creates the
account, dedicated directories, `/etc/kinaiya-ci`, a new AppArmor profile and the
new systemd unit. It refuses pre-existing paths/accounts. It does not install or
upgrade system packages, edit networking, alter other services or prune data.

After preparation, run `install-runner.sh` as `kinaiya-ci` with a freshly minted
repository registration token on stdin. Never paste it into an issue, command
argument, transcript or commit. The script verifies the official architecture's
runner archive checksum, registers explicit labels, and prepares GitHub's
`runsvc.sh` wrapper for the custom systemd service. Start that unit only after
registration. Do not separately install a second `actions.runner.*` service.

Initial versions, verified against upstream downloads:

| Tool | Pin / location |
| --- | --- |
| Actions runner | 2.338.2; official archive SHA-256 in `install-runner.sh`; automatic updates enabled |
| uv | 0.11.26; architecture-specific official SHA-256 in `install-tooling.sh` |
| Python | Official `actions/python-versions` Ubuntu 24.04 builds for both architectures: 3.11.18 / 3.12.16 / 3.14.10, SHA-256 in `python-builds.json`; isolated per-job environments and hashed dependency lock |
| Flutter | Existing 3.47.8 tag, exact `737ba8f885dc774b53b442759999a312c9461e4e` commit |
| Dell JDK | Temurin 17.0.21+9, private `home/jdk17` |
| Dell Android | Command tools 15859902, platform/build-tools 36, NDK 28.2.13676358, private `home/android-sdk` |

`install-tooling.sh` writes only the new account's directories. Android SDK
license acceptance/install logs remain private there. No production signing
key is installed; deterministic Android jobs produce debug APKs. The Python
environment and Flutter SDK live in each job's temporary directory; caches
belong only to this account. ARM64 Android host tooling is not qualified.

Portable uv-managed CPython initially lacked the Linux sealing constants that
Agent requires (#571). `python-env.sh` instead verifies official upstream build
hashes and real kernel-enforced memfd seals before creating the environment.
Account-local patchelf 0.19.3, also checksum-pinned, relocates each executable's
library search path to its matching private libpython; an empty-environment
interpreter probe checks this. Initial direct relocation without that change
failed to load libpython on Dell and crashed during asyncio tests on Oracle.
Those Ubuntu 24.04 builds are also explicitly qualified on Dell's newer Ubuntu.
Dell's missing unzip is supplied by a checksum-verified distro package extracted
only into the CI account. Gradle uses a private 3 GiB heap, bounded metaspace,
two workers and no persistent daemon to fit the service budget.

## Operation, logging and updates

Use the existing Tailscale identities, `ubuntu@oracle-vps` and `dell@server`,
for administration. These are distinct from the unprivileged runner account.

```sh
systemctl is-active kinaiya-ci
systemctl is-enabled kinaiya-ci
sudo journalctl -u kinaiya-ci --since '1 hour ago' --no-pager
sudo systemctl show kinaiya-ci -p User -p CPUQuotaPerSecUSec -p MemoryMax -p TasksMax
```

The service is enabled at boot, waits for its filesystem and network, and
restarts on exit. Registration must remain present. The runner's `_diag` logs
and `_work` live inside its CI root. Logs are sensitive operational material;
do not upload entire logs or `.credentials*` files to issues. Inspect only
relevant redacted lines. System journal retention remains the host's existing
policy. Review disk usage weekly and retain useful failure logs before cleaning
**this account's** stale job files/caches while idle. Never prune the hosts'
existing user caches or Docker storage as runner maintenance.

```sh
gh api repos/rgoza1996/Kinaiya/actions/runners \
  --jq '.runners[] | {name,status,busy,labels:[.labels[].name]}'
gh workflow run ci.yml --ref main -f suite=smoke
gh workflow run ci.yml --ref main -f suite=media
gh workflow run ci.yml --ref main -f suite=android
```

Keep built-in automatic runner updates enabled; do not use `--disableupdate`.
Updates are performed by the dedicated account inside its writable runner
directory, under the same service limits. Review upstream security notices and
check the version/logs and smoke dispatch after an update. System packages,
AppArmor/kernel changes, JDK/SDK/Flutter pins and disk expansion need separate
reviewed maintenance; do not run unattended global upgrades from a CI job.

## Recovery and removal

1. Inspect GitHub job state, the service journal and free space. Do not interrupt
   a busy runner without considering the job's outcome; cancellation is not
   proof an external operation did not happen.
2. Stop/restart **only** `kinaiya-ci` when idle. Do not restart Docker, Tailscale,
   the model service or desktop services to repair CI.
3. Re-run smoke qualification. For dependency failure, recreate only the job's
   temporary Python environment from the existing lock; do not repair another
   user's Python installation or loosen lock hashes.
4. If credentials/registration are lost, stop the unit, remove the exact runner
   registration in GitHub, and use a fresh short-lived token to register again.
   Do not use `--replace` against an unexplained existing runner.
5. If the host or runner account may be compromised, stop the unit, remove its
   GitHub registration, revoke affected credentials, preserve evidence and
   rebuild from a trusted source. Review other services before restoring CI.
6. For deliberate decommissioning, disable/stop the unit and remove the matching
   repository runner registration. Archive any needed evidence, then remove
   only the dedicated unit/profile/policy/account/directories with explicit
   administrator approval. Never recursively delete `/srv/storage` or `/srv`.
   On Dell, an administrator must first run
   `sudo chattr -i /srv/storage/kinaiya-ci` to unlock that exact directory entry.

Temporary reviewed-ref entries must be removed after qualification. Main's PR
protection was already present and enforced for administrators; this setup does
not change it. Updating the host policy is an administrator action, separate
from checking out code. A new actor or workflow fails closed until explicitly
reviewed and installed.

## Qualification record and remaining limits

The PR and #569 contain exact commit/run evidence, including initial failed
setup attempts and subsequent corrections. Full CI, smoke admission, rejected
PRs, Android compilation, iOS compilation and actual native media integration
are separate claims. See [issue 379 qualification](issue-379-qualification.md).

Dell's full root filesystem still requires owner storage maintenance even
though CI uses its data volume. No reboot was performed to test boot startup;
unit enablement and controlled service restarts establish configuration only.
No production media/model, camera, GPU, physical phone, KVM emulator, release
signing or deployment is qualified by these tests.

Completed dispatch evidence before final combined qualification:

- [Smoke, both architectures](https://github.com/rgoza1996/Kinaiya/actions/runs/43555964971), source `db46c7795a0b53bfd2b2ba451aa4577982a89551`, attempt 4: success. Earlier attempts retain the `/proc` mount and tar/openat2 failures.
- [Initial focused media baseline](https://github.com/rgoza1996/Kinaiya/actions/runs/43556338868), same source, attempt 2: 114 passed on each architecture; first attempt records the private directory ownership failure.
- [Real PR rejection probe](https://github.com/rgoza1996/Kinaiya/actions/runs/43557294248), source `fa65cca0bd33ad71fc2c517ba40508af9f0bb054`: both jobs deliberately failed before any action/step, despite forged job-level GitHub variables and an action with `always()` pre/post hooks. Host journals record refusal; listeners record worker exit 137. Temporary probe files were removed from the final diff.
- [Python/runtime bootstrap failures](https://github.com/rgoza1996/Kinaiya/actions/runs/43557334404) retain the initial libpython load/crash evidence. The [original portable-Python/full failure](https://github.com/rgoza1996/Kinaiya/actions/runs/43556654007) and [missing-unzip failure](https://github.com/rgoza1996/Kinaiya/actions/runs/43556651649) remain linked to #571.

These checks cover different source cuts and are not a final-tree full-CI pass.
The final PR description links the later combined-tree checks and their actual
results, including any remaining failures. Do not add matrix counts as distinct
behavioral coverage.

References: [GitHub service setup](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
[job hooks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts),
[runner requirements](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).
