#!/usr/bin/env bash
# wizard.sh — interactive setup for stalwart-easy-deploy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

DEPLOY_YAML="${SCRIPT_DIR}/deploy.yaml"
NO_APPLY=0
PROXY_MODE=""

usage() {
	echo "Usage: bash wizard.sh [--from-engine] [--no-apply] [--proxy-mode standalone|integrate]"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--help|-h)
			usage
			exit 0
			;;
		--from-engine)
			NO_APPLY=1
			PROXY_MODE="integrate"
			shift
			;;
		--no-apply)
			NO_APPLY=1
			shift
			;;
		--proxy-mode)
			PROXY_MODE="${2:-}"
			shift 2
			;;
		--proxy-mode=*)
			PROXY_MODE="${1#*=}"
			shift
			;;
		*)
			die "Unknown option: $1"
			;;
	esac
done

print_banner() {
	echo
	echo -e "${BOLD}  Stalwart Easy Deploy — Setup Wizard${RESET}"
	echo -e "  ─────────────────────────────────────────────────────"
	echo
}

gather_config() {
	local hostname domain data_dir
	local bulwark_on bulwark_domain bulwark_data_dir
	local recovery_password proceed proxy_mode
	local base_domain

	print_banner
	echo -e "  Press Enter to accept a ${CYAN}[default]${RESET}.\n"

	ask hostname "Mail hostname (MX target, e.g. mail.example.com)" "mail.example.com"
	base_domain="$(base_domain_from_host "$hostname")"

	ask domain "Primary email domain (e.g. example.com)" "$base_domain"
	ask data_dir "Stalwart data directory" "/var/lib/stalwart"

	echo
	echo -e "${BOLD}  Webmail (Bulwark)${RESET}"
	ask_yn bulwark_on "Install Bulwark webmail?" "y"
	bulwark_domain="webmail.${domain}"
	bulwark_data_dir="/var/lib/bulwark"
	if [[ "$bulwark_on" == "y" ]]; then
		ask bulwark_domain "Webmail domain" "$bulwark_domain"
		ask bulwark_data_dir "Bulwark data directory" "$bulwark_data_dir"
		if [[ "${bulwark_domain,,}" == "${hostname,,}" ]]; then
			echo
			warn "Same host as mail: Stalwart admin uses /admin, /login, /auth; Bulwark uses /."
			warn "A separate subdomain (webmail.${domain}) is more reliable."
		fi
	fi

	echo
	echo -e "${BOLD}  Recovery admin${RESET}"
	echo "  Used for first-boot Stalwart setup at https://${hostname}/admin"
	ask_secret recovery_password "Password (leave empty to auto-generate on apply)"

	echo
	echo -e "${BOLD}  Reverse proxy${RESET}"
	if [[ -n "${PROXY_MODE}" ]]; then
		proxy_mode="${PROXY_MODE,,}"
		info "Proxy mode: ${proxy_mode} (set by easydeploy-engine)"
	else
		echo "  standalone — this kit runs Caddy on :443 (single-service VPS)"
		echo "  integrate  — shared Caddy via easydeploy-engine (multi-service VPS)"
		ask proxy_mode "Proxy mode: standalone or integrate" "standalone"
		proxy_mode="${proxy_mode,,}"
	fi
	if [[ "$proxy_mode" != "standalone" && "$proxy_mode" != "integrate" ]]; then
		die "proxy mode must be 'standalone' or 'integrate'"
	fi

	echo
	echo -e "${BOLD}  Summary${RESET}"
	echo "  Mail host:     ${hostname}"
	echo "  Email domain:  ${domain}"
	echo "  Stalwart data: ${data_dir}"
	if [[ "$bulwark_on" == "y" ]]; then
		echo "  Webmail:       https://${bulwark_domain}"
		echo "  Bulwark data:  ${bulwark_data_dir}"
	else
		echo "  Webmail:       disabled"
	fi
	echo "  Proxy mode:    ${proxy_mode}"
	echo
	echo "  Ensure DNS A/AAAA for ${hostname} points to this server before continuing."
	if [[ "$bulwark_on" == "y" ]]; then
		echo "  Also point ${bulwark_domain} here."
	fi
	echo

	if [[ "${NO_APPLY}" == "1" ]]; then
		ask_yn proceed "Write deploy.yaml?" "y"
	else
		ask_yn proceed "Write deploy.yaml and deploy now?" "y"
	fi
	[[ "$proceed" == "y" ]] || {
		info "Cancelled."
		exit 0
	}

	cd "${SCRIPT_DIR}"
	uv run python - <<PY
from scripts.config_edit import update_from_wizard
from pathlib import Path

update_from_wizard(
    hostname=${hostname@Q},
    domain=${domain@Q},
    data_dir=${data_dir@Q},
    bulwark_enabled=${bulwark_on@Q} == "y",
    bulwark_domain=${bulwark_domain@Q},
    bulwark_data_dir=${bulwark_data_dir@Q},
    recovery_admin_password=${recovery_password@Q} or None,
    proxy_mode=${proxy_mode@Q},
    path=Path(${DEPLOY_YAML@Q}),
)
PY

	success "Wrote ${DEPLOY_YAML}"
}

main() {
	bash "${SCRIPT_DIR}/ensure-dependencies.sh"
	cd "${SCRIPT_DIR}"
	gather_config
	if [[ "${NO_APPLY}" == "1" ]]; then
		info "Skipping apply (--no-apply / --from-engine). easydeploy-engine will apply."
		return 0
	fi
	bash "${SCRIPT_DIR}/apply.sh"
}

main "$@"
