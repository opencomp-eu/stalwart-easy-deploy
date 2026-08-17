#!/usr/bin/env bash
# scripts/deps_config.sh — Stalwart Easy Deploy extra dependency keys
# (easydeploy-lib already installs docker, compose, openssl, curl, python3,
# borg, borgmatic, and age.)

easydeploy_required_deps() {
	printf '%s\n' git
}
