#!/usr/bin/env python3
"""stalwart-easy-deploy configuration engine."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = PROJECT_ROOT / "compose"
COMPOSE_PROJECT_NAME = "stalwart-easy-deploy"
STATE_DIR = PROJECT_ROOT / ".stalwart-easy-deploy"
SECRETS_PATH = STATE_DIR / "secrets.yaml"
COMPOSE_ENV_PATH = STATE_DIR / "compose.env"
DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"
CADDY_TEMPLATE = PROJECT_ROOT / "caddy" / "Caddyfile.template"
CADDYFILE = PROJECT_ROOT / "caddy" / "Caddyfile"
INTEGRATION_DIR = STATE_DIR / "integration"
INTEGRATION_CADDY_FRAGMENT = INTEGRATION_DIR / "caddy.caddy"
DEFAULT_INTEGRATE_NETWORK = "easydeploy-net"
STALWART_UID = 2000
# ghcr.io/bulwarkmail/webmail runs as USER nextjs (uid 1001, gid 1001)
BULWARK_UID = 1001
BULWARK_GID = 1001

PLACEHOLDER_HOSTS = frozenset({"mail.example.com", "example.com"})
PLACEHOLDER_WEBMAIL = frozenset({"webmail.example.com"})

SECRET_KEYS = (
    "RECOVERY_ADMIN_PASSWORD",
    "BULWARK_SESSION_SECRET",
)


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_yaml(path: Path) -> dict:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    if "{{" in rendered:
        missing = sorted({part.split("}")[0] for part in rendered.split("{{")[1:]})
        raise ValueError(f"Unresolved template placeholders: {', '.join(missing)}")
    return rendered


def load_config(path: Path = DEPLOY_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path.name}. Copy deploy.yaml.example to deploy.yaml or run wizard.sh."
        )
    return load_yaml(path)


def proxy_mode(config: dict) -> str:
    mode = str((config.get("proxy") or {}).get("mode") or "standalone").strip().lower()
    if mode not in {"standalone", "integrate"}:
        raise ValueError("proxy.mode must be 'standalone' or 'integrate'")
    return mode


def integrate_network_name(config: dict) -> str:
    integrate = (config.get("proxy") or {}).get("integrate") or {}
    name = str(integrate.get("network") or DEFAULT_INTEGRATE_NETWORK).strip()
    return name or DEFAULT_INTEGRATE_NETWORK


def bulwark_enabled(config: dict) -> bool:
    return to_bool((config.get("bulwark") or {}).get("enabled", True))


def public_url(config: dict) -> str:
    hostname = str((config.get("stalwart") or {}).get("hostname") or "").strip()
    return f"https://{hostname}"


def validate_config(config: dict) -> None:
    stalwart = config.get("stalwart") or {}
    if not isinstance(stalwart, dict):
        raise ValueError("stalwart section must be a mapping")

    hostname = str(stalwart.get("hostname") or "").strip()
    if not hostname or hostname in PLACEHOLDER_HOSTS:
        raise ValueError("stalwart.hostname must be set to your real mail hostname")

    domain = str(stalwart.get("domain") or "").strip()
    if not domain or domain in PLACEHOLDER_HOSTS:
        raise ValueError("stalwart.domain must be set to your real email domain")

    data_dir = str(stalwart.get("data_dir") or "").strip()
    if not data_dir:
        raise ValueError("stalwart.data_dir must be set")

    proxy_type = (config.get("proxy") or {}).get("type", "caddy")
    if proxy_type != "caddy":
        raise ValueError("proxy.type must be 'caddy' in v1")

    if bulwark_enabled(config):
        bulwark = config.get("bulwark") or {}
        webmail = str(bulwark.get("domain") or "").strip()
        if not webmail or webmail in PLACEHOLDER_WEBMAIL:
            raise ValueError("bulwark.domain must be set to your real webmail domain")
        if not str(bulwark.get("data_dir") or "").strip():
            raise ValueError("bulwark.data_dir must be set when bulwark is enabled")
        hostname = str(stalwart.get("hostname") or "").strip()
        if hostname.lower() == webmail.lower():
            raise ValueError(
                "bulwark.domain must differ from stalwart.hostname. "
                "Both apps serve /api and OAuth on the same paths, so they cannot "
                "share a host. Use e.g. webmail.example.com for Bulwark."
            )

    proxy_mode(config)


def derive_compose_files(config: dict) -> list[str]:
    files = ["docker-compose.yml"]
    if bulwark_enabled(config):
        files.append("bulwark.yml")
    if proxy_mode(config) == "integrate":
        files.append("integrate.yml")
        if bulwark_enabled(config):
            files.append("integrate-bulwark.yml")
    else:
        files.append("caddy.yml")
    return files


def compose_file_paths(config: dict) -> list[Path]:
    return [COMPOSE_DIR / name for name in derive_compose_files(config)]


def random_secret(length: int = 32) -> str:
    return secrets.token_urlsafe(length)[:length]


def load_or_create_secrets(config: dict | None = None) -> dict:
    if SECRETS_PATH.is_file():
        data = load_yaml(SECRETS_PATH)
    else:
        data = {}

    operator_password = ""
    if config is not None:
        operator_password = str(
            (config.get("stalwart") or {}).get("recovery_admin_password") or ""
        ).strip()

    if operator_password:
        data["RECOVERY_ADMIN_PASSWORD"] = operator_password
    elif not str(data.get("RECOVERY_ADMIN_PASSWORD") or "").strip():
        data["RECOVERY_ADMIN_PASSWORD"] = random_secret(16)

    if not str(data.get("BULWARK_SESSION_SECRET") or "").strip():
        data["BULWARK_SESSION_SECRET"] = secrets.token_urlsafe(32)

    save_yaml(SECRETS_PATH, data)
    SECRETS_PATH.chmod(0o600)
    return data


def chown_path(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except PermissionError:
        pass
    except OSError:
        pass


def chown_tree(path: Path, uid: int, gid: int) -> None:
    chown_path(path, uid, gid)
    for child in path.rglob("*"):
        chown_path(child, uid, gid)


def ensure_data_dirs(config: dict) -> None:
    stalwart = config["stalwart"]
    root = Path(str(stalwart["data_dir"]))
    etc_dir = root / "etc"
    data_dir = root / "data"
    for path in (etc_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)
        chown_path(path, STALWART_UID, STALWART_UID)

    if bulwark_enabled(config):
        bulwark_root = Path(str(config["bulwark"]["data_dir"]))
        bulwark_root.mkdir(parents=True, exist_ok=True)
        for name in ("settings", "admin", "admin-state", "telemetry"):
            (bulwark_root / name).mkdir(parents=True, exist_ok=True)
        chown_tree(bulwark_root, BULWARK_UID, BULWARK_GID)


def _proxy_headers() -> str:
    return "header_up Host {host}"


def stalwart_caddy_block(hostname: str, cors_origin: str | None = None) -> str:
    headers = _proxy_headers()
    cors = ""
    if cors_origin:
        cors = f"""
    @cors_preflight {{
        method OPTIONS
        header Origin {cors_origin}
    }}
    handle @cors_preflight {{
        header Access-Control-Allow-Origin {cors_origin}
        header Access-Control-Allow-Methods "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
        header Access-Control-Allow-Headers {{http.request.header.Access-Control-Request-Headers}}
        header Access-Control-Allow-Credentials true
        header Access-Control-Max-Age 86400
        respond 204
    }}
"""
        proxy_cors = f"""
        header_down -Access-Control-Allow-Origin
        header_down Access-Control-Allow-Origin {cors_origin}
        header_down Access-Control-Allow-Credentials true
        header_down Access-Control-Expose-Headers *"""
    else:
        proxy_cors = ""
    return f"""# stalwart-easy-deploy — mail admin + JMAP
{hostname} {{{cors}
    reverse_proxy stalwart:8080 {{
        {headers}{proxy_cors}
    }}
    encode gzip
    log
}}"""


def bulwark_caddy_block(domain: str) -> str:
    headers = _proxy_headers()
    return f"""# stalwart-easy-deploy — webmail
{domain} {{
    reverse_proxy bulwark:3000 {{
        {headers}
    }}
    encode gzip
    log
}}"""


def site_blocks(config: dict) -> str:
    hostname = str(config["stalwart"]["hostname"]).strip()
    if not bulwark_enabled(config):
        return stalwart_caddy_block(hostname)
    webmail = str(config["bulwark"]["domain"]).strip()
    cors_origin = f"https://{webmail}"
    return "\n\n".join(
        [stalwart_caddy_block(hostname, cors_origin=cors_origin), bulwark_caddy_block(webmail)]
    )


def render_caddyfile(config: dict) -> None:
    rendered = render_template(CADDY_TEMPLATE.read_text(), {"SITE_BLOCKS": site_blocks(config)})
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text(rendered + "\n")


def render_integration_fragment(config: dict) -> None:
    INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)
    INTEGRATION_CADDY_FRAGMENT.write_text(site_blocks(config) + "\n")


def write_compose_env(config: dict, secrets: dict) -> None:
    stalwart = config["stalwart"]
    image = f"{stalwart.get('image', 'docker.io/stalwartlabs/stalwart')}:{stalwart.get('tag', 'v0.16')}"
    data_root = Path(str(stalwart["data_dir"]))
    ports = config.get("ports") or {}
    recovery_user = str(stalwart.get("recovery_admin_user") or "admin").strip() or "admin"
    lines = [
        f"STALWART_IMAGE={image}",
        f"STALWART_ETC_DIR={data_root / 'etc'}",
        f"STALWART_DATA_DIR={data_root / 'data'}",
        f"STALWART_PUBLIC_URL={public_url(config)}",
        f"STALWART_RECOVERY_ADMIN={recovery_user}:{secrets['RECOVERY_ADMIN_PASSWORD']}",
        f"SMTP_PORT={ports.get('smtp', 25)}",
        f"SUBMISSION_PORT={ports.get('submission', 587)}",
        f"SUBMISSIONS_PORT={ports.get('submissions', 465)}",
        f"IMAPS_PORT={ports.get('imaps', 993)}",
        f"SIEVE_PORT={ports.get('sieve', 4190)}",
    ]
    if bulwark_enabled(config):
        bulwark = config["bulwark"]
        bulwark_image = (
            f"{bulwark.get('image', 'ghcr.io/bulwarkmail/webmail')}:"
            f"{bulwark.get('tag', '1.7.5')}"
        )
        lines.extend(
            [
                f"BULWARK_IMAGE={bulwark_image}",
                f"BULWARK_DATA_DIR={Path(str(bulwark['data_dir']))}",
                f"BULWARK_APP_NAME={bulwark.get('app_name') or 'Webmail'}",
                f"BULWARK_SESSION_SECRET={secrets['BULWARK_SESSION_SECRET']}",
                f"JMAP_SERVER_URL={public_url(config)}",
            ]
        )
    if proxy_mode(config) == "standalone":
        lines.append(f"SED_CADDYFILE={CADDYFILE.resolve()}")
    COMPOSE_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_ENV_PATH.write_text("\n".join(lines) + "\n")
    COMPOSE_ENV_PATH.chmod(0o600)


def render_runtime_artifacts(config: dict, secrets: dict) -> None:
    ensure_data_dirs(config)
    if proxy_mode(config) == "integrate":
        render_integration_fragment(config)
    else:
        render_caddyfile(config)
    write_compose_env(config, secrets)


def stop_standalone_caddy() -> None:
    if subprocess.run(["docker", "inspect", "stalwart_caddy"], capture_output=True).returncode == 0:
        print("Stopping standalone stalwart_caddy (integrate mode uses easydeploy-engine Caddy)…")
        subprocess.run(["docker", "stop", "stalwart_caddy"], check=False)
        subprocess.run(["docker", "rm", "stalwart_caddy"], check=False)


def ensure_docker_network(name: str) -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(["docker", "network", "create", name], check=True)


def docker_compose_cmd() -> list[str]:
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    compose = shutil.which("docker-compose")
    if compose:
        return [compose]
    raise RuntimeError("Docker Compose v2 is required (docker compose)")


def print_stalwart_logs() -> None:
    if subprocess.run(["docker", "inspect", "stalwart"], capture_output=True).returncode != 0:
        return
    print("\n--- stalwart container logs (last 80 lines) ---", file=sys.stderr)
    subprocess.run(["docker", "logs", "stalwart", "--tail", "80"], check=False)


def run_compose(*args: str) -> None:
    cmd = docker_compose_cmd()
    for compose_file in compose_file_paths(load_config()):
        cmd.extend(["-f", str(compose_file)])
    cmd.extend(args)

    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    if COMPOSE_ENV_PATH.is_file():
        for line in COMPOSE_ENV_PATH.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()

    try:
        subprocess.run(cmd, cwd=COMPOSE_DIR, check=True, env=env)
    except subprocess.CalledProcessError:
        print_stalwart_logs()
        raise


def reconcile_runtime(skip_pull: bool = False) -> None:
    config = load_config()
    mode = proxy_mode(config)
    ensure_docker_network("stalwart-net")
    if mode == "integrate":
        net = integrate_network_name(config)
        if net != DEFAULT_INTEGRATE_NETWORK:
            print(
                f"Warning: custom integrate network {net!r} is not yet supported in compose/integrate.yml; "
                f"using {DEFAULT_INTEGRATE_NETWORK}",
                file=sys.stderr,
            )
        ensure_docker_network(DEFAULT_INTEGRATE_NETWORK)
        stop_standalone_caddy()
    if not skip_pull:
        print("Pulling Stalwart stack images…")
        run_compose("pull")
    print("Starting Stalwart stack…")
    try:
        run_compose("up", "-d", "--wait", "--remove-orphans")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Docker Compose failed while starting the stack. "
            "If you see a network warning about stalwart-net, run: "
            "docker compose -p stalwart-easy-deploy -f compose/docker-compose.yml down "
            "&& docker network rm stalwart-net then re-run apply.sh"
        ) from exc


def print_summary(config: dict, secrets: dict) -> None:
    stalwart = config["stalwart"]
    hostname = stalwart["hostname"]
    domain = stalwart["domain"]
    data_dir = stalwart["data_dir"]
    recovery_user = str(stalwart.get("recovery_admin_user") or "admin")
    print()
    print("=== Deployment summary ===")
    print(f"Stalwart admin:  https://{hostname}/admin")
    print(f"JMAP:            https://{hostname}")
    print(f"Mail domain:     {domain}")
    print(f"Data directory:  {data_dir}")
    print(f"Secrets file:    {SECRETS_PATH}")
    print(f"Recovery admin:  {recovery_user} / {secrets.get('RECOVERY_ADMIN_PASSWORD')}")
    if bulwark_enabled(config):
        print(f"Webmail:         https://{config['bulwark']['domain']}")
    if proxy_mode(config) == "integrate":
        print(f"Proxy mode:      integrate (Caddy fragment: {INTEGRATION_CADDY_FRAGMENT})")
        print("                 Run easydeploy-engine apply.sh to refresh the shared Caddy.")
    else:
        print("Proxy mode:      standalone (local stalwart_caddy on :443)")
    print()
    print("First boot:")
    print(f"  1. Point DNS A/AAAA for {hostname} at this server.")
    if bulwark_enabled(config):
        print(f"     Also point {config['bulwark']['domain']} here.")
    print(f"  2. Open https://{hostname}/admin and complete the Stalwart wizard.")
    print("     Hostname and domain are already set above. Disable ACME HTTP-01")
    print("     (Caddy terminates HTTPS). Choose console logging.")
    print("  3. Publish MX / SPF / DKIM / DMARC from the Stalwart WebUI DNS zone.")
    print("  4. Mail ports 25/465/587/993/4190 are bound on the host, not via Caddy.")
    print()


def apply_configuration(*, skip_runtime: bool = False, skip_pull: bool = False) -> None:
    config = load_config()
    validate_config(config)
    secrets = load_or_create_secrets(config)
    render_runtime_artifacts(config, secrets)
    if not skip_runtime:
        reconcile_runtime(skip_pull=skip_pull)
    print_summary(config, secrets)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply stalwart-easy-deploy configuration")
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Render configs and secrets only; do not run docker compose",
    )
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        help="Skip docker compose pull before up",
    )
    args = parser.parse_args()
    try:
        apply_configuration(skip_runtime=args.skip_runtime, skip_pull=args.skip_pull)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
