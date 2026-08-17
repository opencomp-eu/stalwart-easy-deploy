#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

IFS=' ' read -ra DOCKER_COMPOSE <<< "$(docker_compose_cmd)"
ENV_FILE="${SCRIPT_DIR}/.stalwart-easy-deploy/compose.env"
DEPLOY="${SCRIPT_DIR}/deploy.yaml"

COMPOSE_PROJECT_NAME="stalwart-easy-deploy"

compose_args=(-p "$COMPOSE_PROJECT_NAME" -f "${SCRIPT_DIR}/compose/docker-compose.yml")
integrate="false"
bulwark="true"
if [[ -f "$DEPLOY" ]] && grep -qE 'mode:\s*integrate' "$DEPLOY" 2>/dev/null; then
	integrate="true"
fi
if [[ -f "$DEPLOY" ]] && grep -A2 "^bulwark:" "$DEPLOY" | grep -q "enabled: false"; then
	bulwark="false"
fi
if [[ "$bulwark" == "true" ]]; then
	compose_args+=(-f "${SCRIPT_DIR}/compose/bulwark.yml")
fi
if [[ "$integrate" == "true" ]]; then
	compose_args+=(-f "${SCRIPT_DIR}/compose/integrate.yml")
	if [[ "$bulwark" == "true" ]]; then
		compose_args+=(-f "${SCRIPT_DIR}/compose/integrate-bulwark.yml")
	fi
else
	compose_args+=(-f "${SCRIPT_DIR}/compose/caddy.yml")
fi

env_args=()
if [[ -f "$ENV_FILE" ]]; then
	while IFS= read -r line || [[ -n "$line" ]]; do
		[[ -z "$line" || "$line" == \#* ]] && continue
		env_args+=("$line")
	done <"$ENV_FILE"
fi

info "Stopping Stalwart stack…"
(
	cd "${SCRIPT_DIR}/compose"
	if ((${#env_args[@]})); then
		env "${env_args[@]}" "${DOCKER_COMPOSE[@]}" "${compose_args[@]}" down --remove-orphans || true
	else
		"${DOCKER_COMPOSE[@]}" "${compose_args[@]}" down --remove-orphans || true
	fi
)
success "Stopped."
