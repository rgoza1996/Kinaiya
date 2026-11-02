#!/bin/sh
# Source-checkout convenience alias. The standalone, bundled bootstrap is ../install.sh.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$here/../install.sh" "$@"
