#!/usr/bin/env bash
# backup.sh — Borg backups via the shared easydeploy-lib machinery.
# The payload (deploy.yaml, .stalwart-easy-deploy, data dirs) is defined by
# scripts/backup_plan.py; see deploy.yaml.example for the backup: block.
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

BACKUP_STATE_DIR="${SCRIPT_DIR}/.stalwart-easy-deploy/backup"
BACKUP_STAGING_CURRENT="${BACKUP_STATE_DIR}/staging/current"
PLAN_JSON=""

print_help() {
	cat <<EOF
Usage: bash backup.sh [flags]

  (default)            Create a Borg archive of deploy.yaml, secrets, and data dirs
  --list               List archives in the backup repository
  --export PATH        Also write a portable tar.gz to PATH after a successful backup
  --export-only PATH   Stage a payload and write the portable file; skip Borg
  --export-from-archive NAME --export PATH
                       Export an existing Borg archive to a portable file
  --encrypt            Encrypt the portable export (age passphrase prompt, or
                       EASYDEPLOY_BACKUP_PASSPHRASE via openssl)
  --cold               Stop the stack before staging and start it again after —
                       the only fully consistent snapshot of the mail store
  --schedule           Install or remove the systemd timer from backup.schedule
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

# Print the plan hook script for a key (stop | start | apply); empty when unset.
plan_hook() {
	"${EASYDEPLOY_BACKUP_PYTHON}" - "$1" "${PLAN_JSON}" <<'PY'
import json, sys
plan = json.load(open(sys.argv[2]))
print(plan.get("hooks", {}).get(sys.argv[1], ""))
PY
}

cleanup() {
	[[ -n "${PLAN_JSON}" ]] && rm -f "${PLAN_JSON}"
	# COLD_STOPPED stays true only if we stopped the stack and never restarted it.
	if [[ "${COLD_STOPPED:-}" == "true" ]]; then
		warn "Backup failed — restarting the stack."
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "$(plan_hook start)"
	fi
}

load_settings() {
	local exports
	exports="$(easydeploy_backup_settings_shell "${SCRIPT_DIR}/deploy.yaml")" ||
		die "Invalid backup configuration in deploy.yaml"
	eval "${exports}"
}

main() {
	local list_only="false" export_path="" export_only="false"
	local export_from_archive="" encrypt="false" cold="false" schedule="false"
	COLD_STOPPED="false"

	while (($#)); do
		case "$1" in
			--list)
				list_only="true"
				;;
			--export)
				[[ -n "${2:-}" ]] || usage_die "--export requires a path"
				export_path="$2"
				shift
				;;
			--export-only)
				[[ -n "${2:-}" ]] || usage_die "--export-only requires a path"
				export_only="true"
				export_path="$2"
				shift
				;;
			--export-from-archive)
				[[ -n "${2:-}" ]] || usage_die "--export-from-archive requires an archive name"
				export_from_archive="$2"
				shift
				;;
			--encrypt) encrypt="true" ;;
			--cold) cold="true" ;;
			--schedule) schedule="true" ;;
			-h | --help)
				print_help
				return 0
				;;
			*) usage_die "Unknown flag: $1" ;;
		esac
		shift
	done
	[[ -z "${export_from_archive}" || -z "${export_path}" ]] ||
		usage_die "--export-from-archive requires --export PATH"

	trap cleanup EXIT
	load_plan_json
	load_settings

	if [[ "${schedule}" == "true" ]]; then
		local timer_name
		timer_name="$("${EASYDEPLOY_BACKUP_PYTHON}" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("timer_name", ""))' "${PLAN_JSON}")"
		"${EASYDEPLOY_BACKUP_PYTHON}" "${EASYDEPLOY_LIB}/python/backup_schedule.py" \
			--project-root "${SCRIPT_DIR}" \
			--deploy-yaml "${SCRIPT_DIR}/deploy.yaml" \
			--unit-name "${timer_name}"
		return 0
	fi

	local archive_prefix
	archive_prefix="$("${EASYDEPLOY_BACKUP_PYTHON}" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("archive_prefix", ""))' "${PLAN_JSON}")"

	if [[ "${list_only}" == "true" ]]; then
		require_command borg
		easydeploy_backup_repo_env "${SCRIPT_DIR}/.stalwart-easy-deploy/secrets.yaml"
		easydeploy_backup_list_archives "${BACKUP_REPO_URL}"
		return 0
	fi

	if [[ -n "${export_from_archive}" ]]; then
		require_command borg
		easydeploy_backup_repo_env "${SCRIPT_DIR}/.stalwart-easy-deploy/secrets.yaml"
		local resolved
		resolved="$(easydeploy_backup_resolve_archive "${BACKUP_REPO_URL}" "${export_from_archive}")"
		easydeploy_backup_export_from_archive "${BACKUP_REPO_URL}" "${resolved}" "${export_path}" "${encrypt}"
		return 0
	fi

	if [[ "${export_only}" == "true" ]]; then
		easydeploy_backup_stage_payload "${SCRIPT_DIR}" "${BACKUP_STAGING_CURRENT}" "${BACKUP_REPO_URL}" "${encrypt}"
		easydeploy_backup_export_portable "${export_path}" "${BACKUP_STAGING_CURRENT}" "${encrypt}"
		rm -rf "${BACKUP_STAGING_CURRENT}"
		return 0
	fi

	[[ -n "${BACKUP_REPO_URL}" ]] || die "backup.repository.path is empty — set it in deploy.yaml"
	require_command borg
	require_command borgmatic
	easydeploy_backup_repo_env "${SCRIPT_DIR}/.stalwart-easy-deploy/secrets.yaml"

	mkdir -p "${BACKUP_STATE_DIR}"
	local borgmatic_config="${BACKUP_STATE_DIR}/borgmatic.yaml"

	if [[ "${cold}" == "true" ]]; then
		info "Cold backup: stopping the stack for a consistent mail store..."
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "$(plan_hook stop)"
		COLD_STOPPED="true"
	fi

	easydeploy_backup_stage_payload "${SCRIPT_DIR}" "${BACKUP_STAGING_CURRENT}" "${BACKUP_REPO_URL}" "false"
	easydeploy_backup_write_borgmatic_config "${borgmatic_config}" "${BACKUP_REPO_URL}" "${BACKUP_STAGING_CURRENT}" "${archive_prefix}"
	easydeploy_backup_repo_create "${borgmatic_config}"
	info "Creating Borg archive (prefix: ${archive_prefix})..."
	borgmatic --config "${borgmatic_config}" create --stats
	borgmatic --config "${borgmatic_config}" prune --stats
	borgmatic --config "${borgmatic_config}" check

	if [[ "${COLD_STOPPED}" == "true" ]]; then
		easydeploy_backup_run_hook "${SCRIPT_DIR}" "$(plan_hook start)"
		COLD_STOPPED="false"
		success "Stack restarted."
	fi

	success "Backup complete (repository: ${BACKUP_REPO_URL})."

	if [[ -n "${export_path}" ]]; then
		easydeploy_backup_export_portable "${export_path}" "${BACKUP_STAGING_CURRENT}" "${encrypt}"
	fi
	rm -rf "${BACKUP_STAGING_CURRENT}"
}

main "$@"
