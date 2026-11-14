#!/bin/bash
# Upgrade only the already approved CI unit's private temporary-directory alias.
set -euo pipefail
test "$(id -u)" = 0
case "$(uname -m)" in
  aarch64) ci_root=/srv/kinaiya-ci ;;
  x86_64) ci_root=/srv/storage/kinaiya-ci ;;
  *) exit 1 ;;
esac
if pgrep -u kinaiya-ci -f 'Runner[.]Worker' >/dev/null; then
  echo 'Runner is busy; leave its service unchanged.' >&2
  exit 1
fi
unit=/etc/systemd/system/kinaiya-ci.service
test -f "$unit"
test "$(readlink -f "$unit")" = "$unit"
test "$(stat -c '%u' "$unit")" = 0
grep -Fx "User=kinaiya-ci" "$unit" >/dev/null
grep -Fx "BindPaths=$ci_root" "$unit" >/dev/null
test -d "$ci_root/home/tmp"
systemctl stop kinaiya-ci.service
python3 -I - "$unit" "$ci_root" <<'PY'
from pathlib import Path
import sys

path, root = Path(sys.argv[1]), sys.argv[2]
raw = path.read_text()
old = f"Environment=TMPDIR={root}/home/tmp\n"
if old not in raw and "Environment=TMPDIR=/tmp/ci\n" not in raw:
    raise SystemExit("Unexpected CI temporary-directory configuration")
raw = raw.replace(old, "Environment=TMPDIR=/tmp/ci\n")
alias = f"BindPaths={root}/home/tmp:/tmp/ci\n"
if alias not in raw:
    raw = raw.replace(f"BindPaths={root}\n", f"BindPaths={root}\n" + alias)
path.write_text(raw)
PY
systemctl daemon-reload
systemctl start kinaiya-ci.service
systemctl is-active --quiet kinaiya-ci.service
