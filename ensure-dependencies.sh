#!/usr/bin/env bash
# ensure-dependencies.sh — install and verify host dependencies
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"
# shellcheck source=scripts/deps_config.sh
source "${SCRIPT_DIR}/scripts/deps_config.sh"

ensure_git() {
	if command -v git &>/dev/null; then
		success "git present ($(git --version | head -1))"
		return
	fi
	info "Installing git…"
	local manager
	manager="$(detect_supported_package_manager)" || die "git is required but no supported package manager was found"
	install_missing_dependencies "$manager" git
	if command -v git &>/dev/null; then
		success "git installed"
	else
		die "git is required but not installed — install git and re-run"
	fi
}

ensure_submodules() {
	if [[ ! -d "${SCRIPT_DIR}/.git" ]]; then
		warn "Not a git checkout — skipping submodule update"
		if [[ ! -f "${SCRIPT_DIR}/easydeploy-lib/lib/init.sh" ]]; then
			die "easydeploy-lib is missing — clone the repo with submodules"
		fi
		return
	fi

	info "Initializing git submodule (easydeploy-lib)…"
	git -C "${SCRIPT_DIR}" submodule update --init --recursive
	success "Git submodules ready"
}

ensure_uv() {
	export PATH="${HOME}/.local/bin:${PATH}"
	if command -v uv &>/dev/null; then
		success "uv present ($(uv --version))"
		return
	fi
	info "Installing uv…"
	curl -LsSf https://astral.sh/uv/install.sh | sh
	export PATH="${HOME}/.local/bin:${PATH}"
	if ! command -v uv &>/dev/null; then
		die "uv install finished but uv is not on PATH — add ~/.local/bin to PATH"
	fi
	success "uv installed ($(uv --version))"
}

ensure_python_deps() {
	export PATH="${HOME}/.local/bin:${PATH}"
	info "Syncing Python dependencies (uv sync --dev)…"
	uv sync --dev --directory "${SCRIPT_DIR}"
	success "Python dependencies ready"
}

main() {
	echo
	echo -e "${BOLD}Stalwart Easy Deploy — ensure dependencies${RESET}"
	echo

	ensure_git
	ensure_submodules
	ensure_dependencies_installed
	ensure_uv
	ensure_python_deps

	echo
	success "Host is ready. Next: bash wizard.sh  or  bash apply.sh"
	echo
}

main "$@"
