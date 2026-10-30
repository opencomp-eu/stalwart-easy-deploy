#!/usr/bin/env python3
"""Give Stalwart the certificate Caddy already obtained.

Caddy owns ports 80 and 443, so Stalwart cannot answer an ACME challenge.
Bootstrap therefore leaves certificate management off. IMAPS, SMTPS and
STARTTLS are published by Stalwart itself. With no certificate loaded,
those ports present a self-signed certificate for localhost, and clients
that omit SNI — most of SMTP — have nothing else to use.

Caddy stores a publicly trusted certificate for the mail hostname. This
module copies that PEM into Stalwart, registers it, and selects it as the
default. A hourly timer repeats the copy after Caddy renews the files.
The Certificate object is replaced when the PEM changes: Stalwart records
the expiry as it loads the object and drops the certificate after that
date, so a reload of the same object is not enough.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".stalwart-easy-deploy"
STATE_PATH = STATE_DIR / "mail-tls.json"
MAIL_TLS_UNIT = "stalwart-easy-deploy-mail-tls"
CREATE_KEY = "caddy-mail"
STALWART_CONTAINER = "stalwart"
TLS_DIR = "/etc/stalwart/tls"

_HOSTNAME = re.compile(r"[a-z0-9.-]{1,253}")

# List every leaf Caddy has stored. The hostname is filtered in Python so a
# name cannot change the shell command. Busybox find/stat on the Caddy image
# both accept this form.
_LIST_SCRIPT = """
for base in /data/caddy/certificates /data/certificates; do
  [ -d "$base" ] || continue
  find "$base" -type f -name '*.crt' | while IFS= read -r file; do
    stat -c '%Y %n' "$file"
  done
done
"""

_WRITE_SCRIPT = """
set -eu
umask 077
mkdir -p "$(dirname "$1")"
cat > "$1"
chmod "$2" "$1"
chown 2000:2000 "$1" 2>/dev/null || true
"""


def require_hostname(hostname: str) -> str:
    host = hostname.strip().lower().rstrip(".")
    if (
        not host
        or not _HOSTNAME.fullmatch(host)
        or ".." in host
        or host.startswith(".")
        or host.endswith(".")
        or "/" in host
    ):
        raise ValueError(f"stalwart.hostname is not a DNS name: {hostname}")
    return host


def caddy_container_name(mode: str) -> str:
    if mode == "integrate":
        return "easydeploy_caddy"
    return "stalwart_caddy"


def certificate_paths(hostname: str) -> tuple[str, str]:
    host = require_hostname(hostname)
    return (f"{TLS_DIR}/{host}.crt", f"{TLS_DIR}/{host}.key")


def certificate_payload(hostname: str) -> dict[str, Any]:
    cert_path, key_path = certificate_paths(hostname)
    return {
        "certificate": {"@type": "File", "filePath": cert_path},
        "privateKey": {"@type": "File", "filePath": key_path},
    }


def normalize_pem(text: str) -> str:
    return text.replace("\r\n", "\n").strip() + "\n"


def pem_is_certificate(text: str) -> bool:
    return "-----BEGIN CERTIFICATE-----" in text and "-----END CERTIFICATE-----" in text


def pem_is_private_key(text: str) -> bool:
    markers = (
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
    )
    return any(marker in text for marker in markers)


def parse_cert_listing(text: str) -> list[tuple[int, str]]:
    """Lines of ``mtime path`` from the Caddy container."""
    entries: list[tuple[int, str]] = []
    for line in text.splitlines():
        mtime, separator, path = line.strip().partition(" ")
        if not separator or not path.startswith("/"):
            continue
        try:
            stamp = int(mtime)
        except ValueError:
            continue
        entries.append((stamp, path))
    return entries


def ordered_cert_paths(entries: list[tuple[int, str]], hostname: str) -> list[str]:
    """Newest production certificate first. Staging is used only as a last resort."""
    host = require_hostname(hostname)
    needle = f"/{host}.crt"
    matches = [(stamp, path) for stamp, path in entries if path.lower().endswith(needle)]
    production = [(stamp, path) for stamp, path in matches if "staging" not in path.lower()]
    pool = production or matches
    pool.sort()
    return [path for _, path in reversed(pool)]


def key_path_for(cert_path: str) -> str:
    if cert_path.endswith(".crt"):
        return cert_path[:-4] + ".key"
    return cert_path + ".key"


def load_caddy_material(
    listing: str, files: dict[str, str], hostname: str
) -> tuple[str, str] | None:
    for cert_path in ordered_cert_paths(parse_cert_listing(listing), hostname):
        cert = files.get(cert_path) or ""
        key = files.get(key_path_for(cert_path)) or ""
        if pem_is_certificate(cert) and pem_is_private_key(key):
            return normalize_pem(cert), normalize_pem(key)
    return None


def certificate_file_path(certificate: dict) -> str:
    raw = certificate.get("certificate")
    if isinstance(raw, dict):
        return str(raw.get("filePath") or "")
    return ""


def default_certificate_id(settings: dict) -> str:
    value = settings.get("defaultCertificateId")
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def matching_certificates(
    certificates: list[dict], hostname: str, remembered_id: str
) -> list[dict]:
    cert_path, _key_path = certificate_paths(hostname)
    matches = []
    for certificate in certificates:
        if not isinstance(certificate, dict) or not certificate.get("id"):
            continue
        path = certificate_file_path(certificate)
        if path == cert_path or (remembered_id and str(certificate.get("id")) == remembered_id):
            matches.append(certificate)
    return matches


def plan_mail_tls(
    hostname: str,
    *,
    certificates: list[dict],
    default_id: str,
    remembered_id: str,
    material_changed: bool,
    have_material: bool,
) -> dict[str, Any]:
    """JMAP and file actions. Empty when mail ports already use this certificate."""
    if not have_material:
        return {}
    matches = matching_certificates(certificates, hostname, remembered_id)
    match_ids = [str(item["id"]) for item in matches]
    keep = ""
    if not material_changed and match_ids:
        if default_id in match_ids:
            keep = default_id
        elif remembered_id in match_ids:
            keep = remembered_id
        else:
            keep = match_ids[0]
    extras = [item for item in match_ids if item != keep]
    changes: dict[str, Any] = {}
    if material_changed or not keep:
        changes["write"] = material_changed
        changes["create"] = certificate_payload(hostname)
        changes["destroy_ids"] = match_ids
        changes["set_default_to_created"] = True
        changes["reload_settings"] = True
        changes["reload_certificates"] = True
        return changes
    if extras or default_id != keep:
        changes["destroy_ids"] = extras
        changes["set_default"] = keep
        changes["reload_settings"] = True
        changes["reload_certificates"] = True
    return changes


def material_digest(cert_pem: str, key_pem: str) -> str:
    return hashlib.sha256((cert_pem + key_pem).encode()).hexdigest()


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(hostname: str, certificate_id: str, digest: str) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(
            {"hostname": hostname, "certificate_id": certificate_id, "sha256": digest}
        )
        + "\n"
    )


def _run(
    args: list[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(args, input=input_text, capture_output=True, text=True, check=False)


def container_exists(name: str, *, run: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    result = _run(["docker", "inspect", name], run=run)
    return result.returncode == 0


def _docker_text(
    args: list[str], *, run: Callable[..., subprocess.CompletedProcess[str]]
) -> str | None:
    result = _run(args, run=run)
    if result.returncode != 0:
        return None
    return result.stdout or ""


def fetch_caddy_material(
    hostname: str,
    container: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str] | None:
    listing = _docker_text(
        ["docker", "exec", container, "sh", "-c", _LIST_SCRIPT],
        run=run,
    )
    if listing is None:
        return None
    files: dict[str, str] = {}
    for cert_path in ordered_cert_paths(parse_cert_listing(listing), hostname):
        key_path = key_path_for(cert_path)
        cert = _docker_text(["docker", "exec", container, "cat", cert_path], run=run)
        key = _docker_text(["docker", "exec", container, "cat", key_path], run=run)
        if cert is not None:
            files[cert_path] = cert
        if key is not None:
            files[key_path] = key
    return load_caddy_material(listing, files, hostname)


def fetch_installed(
    hostname: str, *, run: Callable[..., subprocess.CompletedProcess[str]]
) -> tuple[str, str]:
    cert_path, key_path = certificate_paths(hostname)
    cert = _docker_text(["docker", "exec", STALWART_CONTAINER, "cat", cert_path], run=run) or ""
    key = _docker_text(["docker", "exec", STALWART_CONTAINER, "cat", key_path], run=run) or ""
    if pem_is_certificate(cert) and pem_is_private_key(key):
        return normalize_pem(cert), normalize_pem(key)
    return "", ""


def write_installed(
    hostname: str,
    cert_pem: str,
    key_pem: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    cert_path, key_path = certificate_paths(hostname)
    for path, content, mode in ((key_path, key_pem, "600"), (cert_path, cert_pem, "640")):
        result = _run(
            [
                "docker",
                "exec",
                "-i",
                STALWART_CONTAINER,
                "sh",
                "-c",
                _WRITE_SCRIPT,
                "sh",
                path,
                mode,
            ],
            run=run,
            input_text=content,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Could not install {path} in {STALWART_CONTAINER}: {detail[:300]}")


def _created_id(payload: dict, key: str) -> str:
    created = (payload.get("created") or {}).get(key) or {}
    if isinstance(created, dict) and created.get("id"):
        return str(created["id"])
    return key


def execute_mail_tls_plan(
    plan: dict[str, Any],
    call: Callable[[list], dict],
    ok: Callable[[dict, str], dict],
    restart: Callable[[], bool] | None = None,
) -> str:
    """Apply a plan. Returns the certificate id now selected as the default.

    ReloadSettings validates every stored object, so one unrelated invalid
    object (for example a spam rule newer than the server) refuses it. A
    restart loads the stored default certificate regardless, so when
    ``restart`` is given it replaces a refused reload.
    """
    if not plan:
        return ""
    restarted = False

    def run_action(action: str, call_id: str) -> None:
        nonlocal restarted
        if restarted:
            return
        result = ok(
            call([["x:Action/set", {"create": {call_id: {"@type": action}}}, call_id]]),
            call_id,
        )
        if not result.get("notCreated"):
            return
        if restart is not None:
            print(f"Mail TLS: Stalwart refused {action}; restarting it to load the certificate…")
            if restart():
                restarted = True
                return
        raise RuntimeError(f"Could not run {action}: {result['notCreated']}")

    new_id = ""
    if plan.get("create"):
        created = ok(
            call([["x:Certificate/set", {"create": {CREATE_KEY: plan["create"]}}, "c"]]),
            "c",
        )
        if created.get("notCreated"):
            raise RuntimeError(f"Could not register the mail certificate: {created['notCreated']}")
        new_id = _created_id(created, CREATE_KEY)
    default_id = new_id if plan.get("set_default_to_created") else str(plan.get("set_default") or "")
    if default_id:
        updated = ok(
            call(
                [
                    [
                        "x:SystemSettings/set",
                        {"update": {"singleton": {"defaultCertificateId": default_id}}},
                        "s",
                    ]
                ]
            ),
            "s",
        )
        if updated.get("notUpdated"):
            raise RuntimeError(
                f"Could not select the mail certificate as the default: {updated['notUpdated']}"
            )
    if plan.get("reload_settings"):
        run_action("ReloadSettings", "settings")
    if plan.get("reload_certificates"):
        run_action("ReloadTlsCertificates", "certs")
    destroy = [item for item in plan.get("destroy_ids") or [] if item and item != new_id]
    if destroy:
        destroyed = ok(call([["x:Certificate/set", {"destroy": destroy}, "d"]]), "d")
        if destroyed.get("notDestroyed"):
            raise RuntimeError(
                f"Could not remove the previous mail certificate: {destroyed['notDestroyed']}"
            )
        if plan.get("reload_certificates"):
            run_action("ReloadTlsCertificates", "certs-after")
    return default_id


def _settings_record(payload: dict) -> dict:
    listed = payload.get("list")
    if isinstance(listed, list):
        return listed[0] if listed and isinstance(listed[0], dict) else {}
    singleton = payload.get("singleton")
    if isinstance(singleton, dict):
        return singleton
    return payload if "defaultCertificateId" in payload else {}


def apply_mail_certificate(
    config: dict,
    secrets: dict,
    *,
    wait_seconds: int = 0,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Copy Caddy's certificate into Stalwart when it is available."""
    from scripts.apply import _jmap, _jmap_list, _jmap_ok, proxy_mode

    runner = run or subprocess.run
    hostname = require_hostname(str((config.get("stalwart") or {}).get("hostname") or ""))
    container = caddy_container_name(proxy_mode(config))
    if not container_exists(STALWART_CONTAINER, run=runner):
        message = "Mail TLS: stalwart is not running, so the mail certificate was not updated."
        print(message)
        return message
    if not container_exists(container, run=runner):
        message = (
            f"Mail TLS: {container} is not running, so mail ports still present "
            "Stalwart's self-signed certificate."
        )
        print(message)
        return message

    deadline = time.monotonic() + max(wait_seconds, 0)
    material: tuple[str, str] | None = None
    while True:
        material = fetch_caddy_material(hostname, container, run=runner)
        if material or time.monotonic() >= deadline:
            break
        time.sleep(5)
    if not material:
        message = (
            f"Mail TLS: Caddy has not issued a certificate for {hostname} yet. "
            "Mail ports still present Stalwart's self-signed certificate. "
            "The hourly sync retries after DNS is live."
        )
        print(message)
        return message

    installed = fetch_installed(hostname, run=runner)
    digest = material_digest(*material)
    state = load_state()
    remembered = str(state.get("certificate_id") or "") if state.get("hostname") == hostname else ""
    state_digest = str(state.get("sha256") or "") if remembered else ""
    # A copied PEM whose Certificate object was never registered still has the
    # previous expiry on file. Replace the object until the saved digest matches.
    changed = installed != material or state_digest != digest
    if installed != material:
        # Write before registering. Stalwart reads the files while loading
        # the Certificate object and ignores the object if they are missing.
        write_installed(hostname, material[0], material[1], run=runner)

    certificates = _jmap_list(config, secrets, "Certificate")
    settings = _settings_record(
        _jmap_ok(
            _jmap(config, secrets, [["x:SystemSettings/get", {"ids": ["singleton"]}, "g"]]),
            "g",
        )
    )
    plan = plan_mail_tls(
        hostname,
        certificates=certificates,
        default_id=default_certificate_id(settings),
        remembered_id=remembered,
        material_changed=changed,
        have_material=True,
    )
    if not plan:
        message = f"Mail TLS: {hostname} already uses the Caddy certificate."
        print(message)
        return message

    def call(method_calls: list) -> dict:
        return _jmap(config, secrets, method_calls)

    from scripts.apply import restart_stalwart_and_wait

    certificate_id = execute_mail_tls_plan(
        plan, call, _jmap_ok, restart=lambda: restart_stalwart_and_wait(config, secrets)
    )
    if not certificate_id:
        certificate_id = str(plan.get("set_default") or remembered)
    save_state(hostname, certificate_id, material_digest(*material))
    message = f"Mail TLS: installed the Caddy certificate for {hostname}."
    print(message)
    return message


def render_mail_tls_service(project_root: Path) -> str:
    from scripts.groupware import service_owner

    user, group = service_owner(project_root)
    python = project_root / ".venv" / "bin" / "python"
    if python.is_file():
        command = f"{python} -m scripts.mail_tls"
    else:
        uv = shutil.which("uv") or "uv"
        command = f"{uv} run --project {project_root} python -m scripts.mail_tls"
    lines = [
        "[Unit]",
        "Description=Copy Caddy's certificate into Stalwart for IMAP and SMTP",
        "Wants=docker.service",
        "After=docker.service",
        "",
        "[Service]",
        "Type=oneshot",
        f"User={user}",
        f"Group={group}",
    ]
    try:
        import grp

        grp.getgrnam("docker")
    except KeyError:
        pass
    else:
        lines.append("SupplementaryGroups=docker")
    lines.extend(
        [
            f"WorkingDirectory={project_root}",
            f"ExecStart={command}",
            "",
        ]
    )
    return "\n".join(lines)


def render_mail_tls_timer(unit_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Copy Caddy's certificate into Stalwart after renewal",
            "",
            "[Timer]",
            "OnCalendar=hourly",
            "Persistent=true",
            f"Unit={unit_name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def reconcile_mail_tls_timer(
    *,
    project_root: Path | None = None,
    unit_dir: Path | None = None,
    run_command=subprocess.run,
    unit_name: str = MAIL_TLS_UNIT,
) -> str:
    """Install the hourly copy. Renewal is the only reason it has to run again."""
    from scripts.groupware import (
        _requires_privilege,
        _run_systemctl,
        _systemd_available,
        _write_unit,
        systemd_unit_dir,
    )

    root = project_root or PROJECT_ROOT
    unit_dir = unit_dir or systemd_unit_dir()
    if not _systemd_available():
        return "Mail certificate renewal timer skipped because systemd is not available."
    service_path = unit_dir / f"{unit_name}.service"
    timer_path = unit_dir / f"{unit_name}.timer"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        privileged = _requires_privilege(unit_dir)
        service_changed = _write_unit(
            service_path, render_mail_tls_service(root), privileged=privileged
        )
        timer_changed = _write_unit(
            timer_path, render_mail_tls_timer(unit_name), privileged=privileged
        )
        _run_systemctl(run_command, ["daemon-reload"], privileged=privileged, check=True)
        _run_systemctl(
            run_command,
            ["enable", "--now", f"{unit_name}.timer"],
            privileged=privileged,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Failed to install the mail certificate timer: {exc}") from exc
    if service_changed or timer_changed:
        return "Mail certificate renewal timer installed or updated."
    return "Mail certificate renewal timer already up to date."


def reconcile_mail_tls_schedule() -> None:
    try:
        message = reconcile_mail_tls_timer()
    except (OSError, RuntimeError) as exc:
        print(f"Mail TLS: {exc}", file=sys.stderr)
        return
    print(f"Mail TLS: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy Caddy's certificate into Stalwart")
    parser.parse_args(argv)
    from scripts.apply import SECRETS_PATH, load_config, load_yaml

    config = load_config()
    if not SECRETS_PATH.is_file():
        print(f"Error: missing {SECRETS_PATH}. Run apply.sh first.", file=sys.stderr)
        return 1
    try:
        apply_mail_certificate(config, load_yaml(SECRETS_PATH))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
