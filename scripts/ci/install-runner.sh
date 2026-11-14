#!/bin/bash
# Run as the dedicated account, after privileged prepare-host.sh.
set -euo pipefail
test "$(id -un)" = kinaiya-ci
ci_root=$(dirname "$HOME")
cd "$ci_root/runner"
test ! -e .runner
case "$(uname -m)" in
  aarch64) arch=arm64; digest=670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945; name=kinaiya-oracle-arm64 ;;
  x86_64) arch=x64; digest=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40; name=kinaiya-dell-x64 ;;
  *) exit 1 ;;
esac
archive="$HOME/runner-2.338.2.tar.gz"
curl -fsSL --retry 3 "https://github.com/actions/runner/releases/download/v2.338.2/actions-runner-linux-$arch-2.338.2.tar.gz" -o "$archive"
printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
tar -xzf "$archive"
rm -- "$archive"
# The short-lived registration token arrives only on stdin. The administrator's
# GitHub credential is never copied to this account or interpolated into argv.
IFS= read -r ACTIONS_RUNNER_INPUT_TOKEN
ACTIONS_RUNNER_INPUT_TOKEN=${ACTIONS_RUNNER_INPUT_TOKEN//$'\r'/}
export ACTIONS_RUNNER_INPUT_TOKEN
./config.sh --unattended --url https://github.com/rgoza1996/Kinaiya \
  --name "$name" --labels "kinaiya-ci,kinaiya-$arch" --work _work
unset ACTIONS_RUNNER_INPUT_TOKEN
cp -- bin/runsvc.sh runsvc.sh
chmod 0755 runsvc.sh
printf '%s\n' kinaiya-ci.service > .service
printf '%s\n' "$HOME/.local/bin:$HOME/jdk17/bin:/usr/local/bin:/usr/bin:/bin" > .path
chmod 0600 .runner .credentials .credentials_rsaparams .env .path
./bin/Runner.Listener --version
