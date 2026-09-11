#!/usr/bin/env bash
# restore.sh — restore this kit from a Borg archive or a portable backup file.
# Flow (shared backup contract): confirm -> stop stack -> extract -> restore
# payload (config -> apply -> data) -> start, unless --keep-stopped.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
cd "${SCRIPT_DIR}"

# The lib shells out to this interpreter; prefer the kit venv (PyYAML) over system python3.
if [[ -z "${EASYDEPLOY_BACKUP_PYTHON:-}" ]]; then
	if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
		EASYDEPLOY_BACKUP_PYTHON="${SCRIPT_DIR}/.venv/bin/python"
	else
		EASYDEPLOY_BACKUP_PYTHON="python3"
	fi
	export EASYDEPLOY_BACKUP_PYTHON
fi

RESTORE_DIR="${SCRIPT_DIR}/.stalwart-easy-deploy/backup/restore"
PLAN_JSON=""

print_help() {
	cat <<EOF
Usage: bash restore.sh (--archive NAME | --latest | --file PATH | --list) [flags]

  --archive NAME       Restore the named Borg archive (full name or unique ID prefix)
  --latest             Restore the newest Borg archive
  --file PATH          Restore from a portable backup file (tar.gz, .age, or encrypted)
  --list               List archives in the backup repository
  --encrypt            Accepted for flag parity; encrypted inputs are detected
                       and decrypted automatically
  --passphrase-file F  Passphrase file for openssl-encrypted portable files
  --yes                Skip the confirmation prompts
  --keep-stopped       Do not start the stack after restoring (bash start.sh later)
  -h, --help           Show this help
EOF
}

usage_die() {
	print_help >&2
	die "$1"
}

require_command() {
	command -v "$1" &>/dev/null || die "Required command not found: $1"
}

load_plan_json() {
	PLAN_JSON="$(mktemp)"
	easydeploy_backup_py "${EASYDEPLOY_LIB}/python/backup_plan.py" \
		--project-root "${SCRIPT_DIR}" --emit-plan-json >"${PLAN_JSON}"
}

load_settings() {
	local exports
	exports="$(easydeploy_backup_settings_shell "${SCRIPT_DIR}/deploy.yaml")" ||
		die "Invalid backup configuration in deploy.yaml"
	eval "${exports}"
}

# Print the plan hook script for a key (stop | start | apply); empty when unset.
plan_hook() {
	"${EASYDEPLOY_BACKUP_PYTHON}" - "$1" "${PLAN_JSON}" <<'PY'
import json, sys
plan = json.load(open(sys.argv[2]))
print(plan.get("hooks", {}).get(sys.argv[1], ""))
PY
}

stack_running() {
	# Container names from compose/docker-compose.yml and compose/bulwark.yml.
	easydeploy_backup_container_running "stalwart" || easydeploy_backup_container_running "bulwark"
}

cleanup() {
	[[ -n "${PLAN_JSON}" ]] && rm -f "${PLAN_JSON}"
	rm -rf "${RESTORE_DIR}"
}

main() {
	local mode="" archive="" file_path="" passphrase_file=""
	local yes="false" keep_stopped="false"

	while (($#)); do
		case "$1" in
			--archive)
				[[ -n "${2:-}" ]] || usage_die "--archive requires a name"
				mode="archive"
				archive="$2"
				shift
				;;
			--latest) mode="latest" ;;
			--file)
				[[ -n "${2:-}" ]] || usage_die "--file requires a path"
				mode="file"
				file_path="$2"
				shift
				;;
			--list) mode="list" ;;
			--encrypt) : ;;
			--passphrase-file)
				[[ -n "${2:-}" ]] || usage_die "--passphrase-file requires a path"
				passphrase_file="$2"
				shift
				;;
			--yes) yes="true" ;;
			--keep-stopped) keep_stopped="true" ;;
			-h | --help)
				print_help
				return 0
				;;
			*) usage_die "Unknown flag: $1" ;;
		esac
		shift
	done
	[[ -n "${mode}" ]] || usage_die "Choose one source: --archive NAME, --latest, --file PATH, or --list"

	if [[ -n "${passphrase_file}" ]]; then
		[[ -f "${passphrase_file}" ]] || die "Passphrase file not found: ${passphrase_file}"
		export PASSPHRASE_FILE="${passphrase_file}"
	fi

	trap cleanup EXIT
	load_plan_json

	if [[ "${mode}" == "list" ]]; then
		load_settings
		require_command borg
		easydeploy_backup_repo_env "${SCRIPT_DIR}/.stalwart-easy-deploy/secrets.yaml"
		easydeploy_backup_list_archives "${BACKUP_REPO_URL}"
		return 0
	fi

	if [[ "${mode}" == "file" ]]; then
		[[ -f "${file_path}" ]] || die "Portable backup not found: ${file_path}"
	else
		load_settings
		require_command borg
		easydeploy_backup_repo_env "${SCRIPT_DIR}/.stalwart-easy-deploy/secrets.yaml"
	fi

	warn "Restoring overwrites deploy.yaml, secrets, and the Stalwart/Bulwark data directories."
	if [[ "${yes}" != "true" ]]; then
		[[ -t 0 ]] || die "Refusing to restore non-interactively without --yes."
		local confirm=""
		ask_yn confirm "Restore this backup now?" n
		[[ "${confirm}" == "y" ]] || die "Aborted."
		ask_yn confirm "Last chance — data directories will be replaced. Continue?" n
		[[ "${confirm}" == "y" ]] || die "Aborted."
	fi

	local stop_hook start_hook stopped="false"
	stop_hook="$(plan_hook stop)"
	start_hook="$(plan_hook start)"

	if [[ -n "${stop_hook}" ]]; then
		if [[ "${mode}" == "file" ]]; then
			# Stop only when the stack is running; a fresh VPS has nothing to stop.
			if stack_running; then
				info "Stopping the stack..."
				easydeploy_backup_run_hook "${SCRIPT_DIR}" "${stop_hook}"
				stopped="true"
			fi
		else
			# Borg restores may also run on a half-started host — stop unconditionally.
			info "Stopping the stack..."
			easydeploy_backup_run_hook "${SCRIPT_DIR}" "${stop_hook}"
			stopped="true"
		fi
	fi

	mkdir -p "${RESTORE_DIR}"
	if [[ "${mode}" == "file" ]]; then
		easydeploy_backup_extract_portable "${file_path}" "${RESTORE_DIR}"
	else
		local archive_name
		if [[ "${mode}" == "latest" ]]; then
			archive_name="$(borg list --short --last 1 "${BACKUP_REPO_URL}")"
			[[ -n "${archive_name}" ]] || die "No archives found in ${BACKUP_REPO_URL}."
		else
			archive_name="$(easydeploy_backup_resolve_archive "${BACKUP_REPO_URL}" "${archive}")"
		fi
		info "Extracting Borg archive '${archive_name}'..."
		(cd "${RESTORE_DIR}" && borg extract "${BACKUP_REPO_URL}::${archive_name}")
	fi

	[[ -d "${RESTORE_DIR}/payload" ]] || die "Backup does not contain a payload directory."

	easydeploy_backup_restore_payload "${SCRIPT_DIR}" "${RESTORE_DIR}/payload" "${PLAN_JSON}"
	rm -rf "${RESTORE_DIR}"

	if [[ "${keep_stopped}" == "true" ]]; then
		warn "Stack left stopped (--keep-stopped). Bring it back with: bash start.sh"
	elif [[ -n "${start_hook}" ]]; then
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "${start_hook}"
	fi

	success "Restore complete."
}

main "$@"
