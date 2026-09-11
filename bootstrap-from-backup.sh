#!/usr/bin/env bash
# bootstrap-from-backup.sh — fresh VPS: install dependencies, then restore a
# portable backup file (tar.gz, .age, or encrypted) and re-apply the stack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

usage() {
	cat <<EOF
Usage: bash bootstrap-from-backup.sh <portable-backup.tar.gz[.age|.enc]> [restore flags...]

Restores this deployment from a portable backup file produced by
'bash backup.sh --export PATH [--encrypt]'. Extra flags are passed to
restore.sh (e.g. --yes, --passphrase-file F, --keep-stopped).
EOF
}

(($# >= 1)) || {
	usage >&2
	die "A portable backup file is required."
}

case "$1" in
	-h | --help)
		usage
		exit 0
		;;
	-*) ;;

esac
if [[ "$1" == -* ]]; then
	usage >&2
	die "The first argument must be the portable backup file."
fi

file="$1"
shift

[[ -f "${file}" ]] || die "Portable backup not found: ${file}"

bash "${SCRIPT_DIR}/ensure-dependencies.sh"
exec bash "${SCRIPT_DIR}/restore.sh" --file "${file}" "$@"
