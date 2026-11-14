#!/bin/bash
# Privileged, explicit installation only. Review before sudo; never called by CI.
set -euo pipefail
test "$(id -u)" -eq 0
case "${1:-}:$(uname -m)" in
  oracle:aarch64) ci_root=/srv/kinaiya-ci ;;
  dell:x86_64) mountpoint -q /srv/storage; ci_root=/srv/storage/kinaiya-ci ;;
  *) echo 'Expected oracle/ARM64 or dell/x64' >&2; exit 1 ;;
esac
test "$(readlink -m "$ci_root")" = "$ci_root"
test ! -e "$ci_root"
! getent passwd kinaiya-ci >/dev/null
test ! -e /etc/kinaiya-ci
test ! -e /etc/systemd/system/kinaiya-ci.service
test ! -e /etc/apparmor.d/kinaiya-ci-runner
for command in python3 git curl tar bwrap apparmor_parser; do command -v "$command" >/dev/null; done
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
test -f "$source_dir/runner_job_policy.py"
test "$(df --output=avail -B1 "$(dirname "$ci_root")" | tail -1)" -gt 8000000000

useradd --system --user-group --home-dir "$ci_root/home" --shell /usr/sbin/nologin kinaiya-ci
install -d -m 0755 -o root -g root "$ci_root"
install -d -m 0700 -o kinaiya-ci -g kinaiya-ci "$ci_root/home" "$ci_root/runner"
install -d -m 0700 -o kinaiya-ci -g kinaiya-ci "$ci_root/home/tmp" "$ci_root/home/.local" "$ci_root/home/.local/bin"
# Dell's existing shared storage parent is world-writable. Protect only this
# new directory entry against rename; descendants remain writable for CI.
if test "$1" = dell; then chattr +i "$ci_root"; fi
install -d -m 0755 -o root -g root /etc/kinaiya-ci
install -m 0644 -o root -g root "$source_dir/runner_job_policy.py" /etc/kinaiya-ci/runner_job_policy.py
cat > /etc/kinaiya-ci/policy.json <<'JSON'
{
  "repository": "rgoza1996/Kinaiya",
  "repository_id": "1435085891",
  "actor_ids": ["97354241"],
  "workflows": ["ci.yml"],
  "reviewed_refs": {}
}
JSON
cat > /etc/kinaiya-ci/job-started.sh <<'SH'
#!/bin/bash
set -eu
exec /usr/bin/python3 -I /etc/kinaiya-ci/runner_job_policy.py
SH
chmod 0755 /etc/kinaiya-ci/job-started.sh

# Grant user namespaces to this service domain, without changing the host sysctl
# or granting the capability to other users' processes.
cat > /etc/apparmor.d/kinaiya-ci-runner <<'AA'
abi <abi/4.0>,
include <tunables/global>
profile kinaiya-ci-runner flags=(unconfined) {
  userns,
}
AA
apparmor_parser -r /etc/apparmor.d/kinaiya-ci-runner

cat > /etc/systemd/system/kinaiya-ci.service <<UNIT
[Unit]
Description=Kinaiya trusted GitHub Actions runner
Wants=network-online.target
After=network-online.target
RequiresMountsFor=$ci_root
ConditionPathExists=$ci_root/runner/.runner

[Service]
User=kinaiya-ci
Group=kinaiya-ci
WorkingDirectory=$ci_root/runner
ExecStart=$ci_root/runner/runsvc.sh
Environment=HOME=$ci_root/home
Environment=PATH=$ci_root/home/.local/bin:$ci_root/home/jdk17/bin:/usr/local/bin:/usr/bin:/bin
Environment=TMPDIR=/tmp/ci
Environment=UV_CACHE_DIR=$ci_root/home/.cache/uv
Environment=ANDROID_HOME=$ci_root/home/android-sdk
Environment=ANDROID_USER_HOME=$ci_root/home/.android
Environment=JAVA_HOME=$ci_root/home/jdk17
Environment=ACTIONS_RUNNER_HOOK_JOB_STARTED=/etc/kinaiya-ci/job-started.sh
UnsetEnvironment=BASH_ENV ENV PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH
Restart=always
RestartSec=10
TimeoutStopSec=120
KillMode=control-group
UMask=0077
Nice=10
CPUQuota=200%
MemoryHigh=6G
MemoryMax=8G
TasksMax=2048
IOWeight=20
OOMPolicy=stop
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
# NoNewPrivileges already prevents set-ID escalation. RestrictSUIDSGID blocks
# openat2, which breaks Ubuntu 26.04's tar action-archive extraction.
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
# Do not mask /proc subtrees: that prevents nested unprivileged bubblewrap from
# mounting its own proc filesystem. The account has no kernel-write privilege.
ProtectControlGroups=yes
# ProtectHostname also masks /proc/sys/kernel/{hostname,domainname}, which
# prevents a nested unprivileged proc mount. No host hostname privilege exists.
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
TemporaryFileSystem=/srv:ro
BindPaths=$ci_root
BindPaths=$ci_root/home/tmp:/tmp/ci
ReadWritePaths=$ci_root/home $ci_root/runner
InaccessiblePaths=-/run/docker.sock -/run/containerd -/var/lib/docker -/opt -/mnt -/media
AppArmorProfile=kinaiya-ci-runner
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable kinaiya-ci.service
echo "Prepared $ci_root; service will start only after non-root registration."
