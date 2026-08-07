#!/bin/bash
# DEPRECATED compatibility wrapper.
#
# The former shell implementation was unsafe: it did not export LAST_TS or
# TABLES to its embedded Python process and its SELECT omitted recvid/recvnm.
# Keep one implementation only; callers should invoke olaradio-monitor.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "warning: olaradio-monitor.sh is deprecated; use olaradio-monitor.py" >&2
exec /usr/bin/python3 "$SCRIPT_DIR/olaradio-monitor.py" "$@"
