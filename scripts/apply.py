#!/usr/bin/env python3
"""stalwart-easy-deploy configuration engine."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))
import hostfs  # noqa: E402

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
# Docker user-defined bridges (172.16-31) plus other container/private ranges.
DOCKER_LAN = ipaddress.ip_network("172.16.0.0/12")
ALLOWED_DOCKER_LAN = "172.16.0.0/12"
PRIVATE_UNBAN_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
# Stalwart scan-bans the reverse-proxy IP on the first matching exploit URL.
# Drop those paths in Caddy so internet scanners cannot lock Caddy out.
CADDY_SCAN_BAN_PATHS = (
    r"(?i)(\.php([/?]|$))|(\.cgi([/?]|$))|(\.asp([/?]|$))"
    r"|/wp-|/cgi-bin|xmlrpc|joomla|wordpress|drupal|\.\./"
)
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


def stalwart_https_upstream(config: dict) -> bool:
    """Caddy terminates TLS. Stalwart HTTP :8080 is the reverse-proxy upstream.

    Post-wizard HTTPS :443 inside the container often resets Caddy (self-signed
    listener, Proxy Protocol, or TLS-on-TLS). Keep :8080 as primary unless the
    operator explicitly sets stalwart.caddy_upstream: https.
    """
    override = str((config.get("stalwart") or {}).get("caddy_upstream") or "http").strip().lower()
    return override in {"https", "443"}


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
        hostfs.chown_path(path, uid, gid)
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
    root = hostfs.ensure_writable_directory(stalwart["data_dir"])
    etc_dir = root / "etc"
    data_dir = root / "data"
    for path in (etc_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)
        chown_path(path, STALWART_UID, STALWART_UID)

    if bulwark_enabled(config):
        bulwark_root = hostfs.ensure_writable_directory(config["bulwark"]["data_dir"])
        for name in ("settings", "admin", "admin-state", "telemetry"):
            (bulwark_root / name).mkdir(parents=True, exist_ok=True)
        chown_tree(bulwark_root, BULWARK_UID, BULWARK_GID)


def _proxy_headers() -> str:
    return "header_up Host {host}"


# Credentialed JMAP (Authorization) cannot use ACAO * or Expose-Headers *.
# Stalwart usePermissiveCors emits * and no Allow-Credentials.
CORS_ALLOW_HEADERS = (
    "Authorization, Content-Type, Accept, X-Requested-With, Origin, "
    "X-JMAP-Prefix, X-JMAP-Request-Id, X-JMAP-Session-State"
)
CORS_ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"


def _indent_block(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line.strip() else line for line in text.splitlines())


def _http_upstream(headers: str, proxy_cors: str) -> str:
    return f"""reverse_proxy stalwart:8080 {{
    {headers}{proxy_cors}
}}"""


def _https_upstream(hostname: str, headers: str, proxy_cors: str) -> str:
    return f"""reverse_proxy https://stalwart:443 {{
    {headers}{proxy_cors}
    transport http {{
        tls_insecure_skip_verify
        tls_server_name {hostname}
    }}
}}"""


def stalwart_caddy_block(
    hostname: str,
    cors_origin: str | None = None,
    *,
    https_upstream: bool = False,
) -> str:
    headers = _proxy_headers()
    cors = ""
    proxy_cors = ""
    if cors_origin:
        cors = f"""
    header {{
        Access-Control-Allow-Origin {cors_origin}
        Access-Control-Allow-Credentials true
        Access-Control-Allow-Methods "{CORS_ALLOW_METHODS}"
        Access-Control-Allow-Headers "{CORS_ALLOW_HEADERS}"
        Vary Origin
        defer
    }}
    @cors_preflight {{
        method OPTIONS
    }}
    handle @cors_preflight {{
        respond 204
    }}
"""
        # Strip Stalwart's ACAO: * so it cannot coexist with the deferred origin.
        # Do not delete Access-Control-Allow-Origin here after setting it —
        # Caddy's header_down delete wins over a later set of the same name.
        proxy_cors = """
    header_down -Access-Control-Allow-Origin
    header_down -Access-Control-Allow-Credentials
    header_down -Access-Control-Allow-Methods
    header_down -Access-Control-Allow-Headers
    header_down -Access-Control-Expose-Headers"""
    primary = (
        _https_upstream(hostname, headers, proxy_cors)
        if https_upstream
        else _http_upstream(headers, proxy_cors)
    )
    # One upstream only. Failing over 8080↔443 from Caddy looks like a port
    # scan and Stalwart bans the proxy IP (then /admin goes silent).
    proxy_indented = _indent_block(primary, 8)
    return f"""# stalwart-easy-deploy — mail admin + JMAP
{hostname} {{{cors}
    @scan_ban path_regexp {CADDY_SCAN_BAN_PATHS}
    handle @scan_ban {{
        respond 404
    }}
    handle {{
{proxy_indented}
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
    https_upstream = stalwart_https_upstream(config)
    if not bulwark_enabled(config):
        return stalwart_caddy_block(hostname, https_upstream=https_upstream)
    webmail = str(config["bulwark"]["domain"]).strip()
    cors_origin = f"https://{webmail}"
    return "\n\n".join(
        [
            stalwart_caddy_block(
                hostname, cors_origin=cors_origin, https_upstream=https_upstream
            ),
            bulwark_caddy_block(webmail),
        ]
    )


def render_caddyfile(config: dict) -> None:
    rendered = render_template(CADDY_TEMPLATE.read_text(), {"SITE_BLOCKS": site_blocks(config)})
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text(rendered + "\n")


def render_integration_fragment(config: dict) -> None:
    INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)
    INTEGRATION_CADDY_FRAGMENT.write_text(site_blocks(config) + "\n")


def bulwark_oauth_env_lines(config: dict) -> list[str]:
    """Kanidm OIDC for Bulwark. Password form is hidden; IMAP uses Stalwart app passwords."""
    identity = apply_engine_identity_sidecar(config)
    oidc = identity.get("oidc") if isinstance(identity.get("oidc"), dict) else {}
    issuer = str(oidc.get("issuer_url") or "").strip().rstrip("/")
    client_id = str(oidc.get("client_id") or "").strip()
    if not issuer or not client_id:
        return []
    return [
        "OAUTH_ENABLED=true",
        "OAUTH_ONLY=true",
        "AUTO_SSO_ENABLED=true",
        f"OAUTH_CLIENT_ID={client_id}",
        f"OAUTH_ISSUER_URL={issuer}",
        "OAUTH_SCOPES=openid profile email",
    ]


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
        lines.extend(bulwark_oauth_env_lines(config))
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


def address_is_docker_lan(value: str) -> bool:
    """True if an IP or CIDR is in Docker/private ranges we must never ban."""
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        try:
            ip = ipaddress.ip_address(raw.split("/")[0])
        except ValueError:
            return False
        net = ipaddress.ip_network(ip)
    return any(net.subnet_of(block) or net.overlaps(block) for block in PRIVATE_UNBAN_NETWORKS)


def _row_address(row: dict) -> str:
    """Best-effort IP/CIDR from a BlockedIp/AllowedIp JMAP object."""
    for key in ("address", "ip"):
        val = row.get(key)
        if isinstance(val, dict):
            val = val.get("address") or val.get("ip") or ""
        text = str(val or "").strip()
        if text:
            return text
    ident = str(row.get("id") or "").strip()
    try:
        ipaddress.ip_network(ident, strict=False)
        return ident
    except ValueError:
        return ""


def docker_allowlist_cidrs() -> list[str]:
    """172.16.0.0/12 covers 172.16–172.31 (so 172.19.0.0/16 is included)."""
    cidrs = [ALLOWED_DOCKER_LAN, "127.0.0.0/8"]
    listed = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
            "stalwart",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for name in listed.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        subnets = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "-f",
                "{{range .IPAM.Config}}{{.Subnet}}\n{{end}}",
                name,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in subnets.stdout.splitlines():
            line = line.strip()
            if line:
                cidrs.append(line)
    return list(dict.fromkeys(cidrs))


JMAP_USING = ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"]
JMAP_URLS = (
    "http://127.0.0.1:8080/jmap",
    "http://127.0.0.1:8080/api",
)


def _jmap_ok(response: dict, call_id: str) -> dict:
    for entry in response.get("methodResponses") or []:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        name, payload, cid = entry[0], entry[1], entry[2]
        if cid != call_id:
            continue
        if name == "error" or str(name).endswith("/error"):
            detail = payload if isinstance(payload, dict) else {"description": payload}
            raise RuntimeError(
                detail.get("description") or detail.get("type") or str(payload)
            )
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected JMAP payload for {call_id}: {payload!r}")
        return payload
    raise RuntimeError(f"no JMAP response for {call_id}")


def _jmap(config: dict, secrets: dict, method_calls: list, *, timeout: int = 15) -> dict:
    """POST JMAP via curl already in the stalwart image (localhost, no extra container)."""
    user = str((config.get("stalwart") or {}).get("recovery_admin_user") or "admin").strip() or "admin"
    password = str(secrets.get("RECOVERY_ADMIN_PASSWORD") or "")
    payload = json.dumps({"using": JMAP_USING, "methodCalls": method_calls})
    last_error = "JMAP call failed"
    for url in JMAP_URLS:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "stalwart",
                "curl",
                "-fsS",
                "--max-time",
                str(timeout),
                "-u",
                f"{user}:{password}",
                "-H",
                "Content-Type: application/json",
                "-H",
                "X-Forwarded-For: 127.0.0.1",
                "-d",
                payload,
                url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        body = (result.stdout or "").strip()
        if result.returncode == 0 and body:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                last_error = body[:400]
                continue
            if isinstance(data, dict):
                return data
            last_error = body[:400]
            continue
        last_error = ((result.stderr or result.stdout or "").strip() or last_error)[:400]
    raise RuntimeError(last_error)


def _jmap_list(config: dict, secrets: dict, type_name: str) -> list[dict]:
    prefix = type_name if type_name.startswith("x:") else f"x:{type_name}"
    queried = _jmap(config, secrets, [[f"{prefix}/query", {"limit": 500}, "q"]])
    ids = list(_jmap_ok(queried, "q").get("ids") or [])
    if not ids:
        return []
    got = _jmap(
        config,
        secrets,
        [[f"{prefix}/get", {"ids": ids}, "g"]],
    )
    return list(_jmap_ok(got, "g").get("list") or [])


def _is_bootstrap_error(exc: BaseException) -> bool:
    return "bootstrap mode" in str(exc).lower()


def bootstrap_update_fields(config: dict) -> dict:
    """Values for x:Bootstrap/set — Caddy owns TLS, Docker captures stdout."""
    stalwart = config.get("stalwart") or {}
    return {
        "serverHostname": str(stalwart.get("hostname") or "").strip(),
        "defaultDomain": str(stalwart.get("domain") or "").strip(),
        "requestTlsCertificate": False,
        "generateDkimKeys": True,
        "tracer": {
            "@type": "Stdout",
            "enable": True,
            "ansi": False,
            "buffered": True,
        },
    }


def complete_bootstrap(config: dict, secrets: dict) -> bool:
    """Finish first-boot setup without the WebUI (Caddy may already be banned)."""
    fields = bootstrap_update_fields(config)
    hostname = fields["serverHostname"]
    domain = fields["defaultDomain"]
    print(f"Stalwart is in bootstrap mode. Completing setup for {hostname} / {domain}…")
    try:
        payload = _jmap_ok(
            _jmap(
                config,
                secrets,
                [["x:Bootstrap/set", {"update": {"singleton": fields}}, "b"]],
                timeout=120,
            ),
            "b",
        )
    except RuntimeError as exc:
        print(f"Error: bootstrap failed: {exc}", file=sys.stderr)
        return False
    not_updated = payload.get("notUpdated") or {}
    if not_updated:
        print(f"Error: bootstrap rejected: {json.dumps(not_updated)[:600]}", file=sys.stderr)
        return False
    details = (payload.get("updated") or {}).get("singleton") or {}
    if isinstance(details, dict):
        user = str(details.get("username") or details.get("Username") or "").strip()
        secret = str(details.get("secret") or details.get("Secret") or "").strip()
        if secret:
            secrets["STALWART_ADMIN_USER"] = user or f"admin@{domain}"
            secrets["STALWART_ADMIN_PASSWORD"] = secret
            save_yaml(SECRETS_PATH, secrets)
            SECRETS_PATH.chmod(0o600)
            print(f"  administrator {secrets['STALWART_ADMIN_USER']} saved in {SECRETS_PATH}")
    print("  bootstrap written; restarting stalwart…")
    return True


def restart_stalwart_and_wait(config: dict, secrets: dict, *, attempts: int = 30) -> bool:
    subprocess.run(["docker", "restart", "stalwart"], check=False, capture_output=True)
    for _ in range(attempts):
        time.sleep(2)
        try:
            _jmap_list(config, secrets, "BlockedIp")
            print("  stalwart is up after restart")
            return True
        except RuntimeError as exc:
            if _is_bootstrap_error(exc):
                continue
            # Connection refused / curl errors while the process comes back.
            continue
    print(
        "Warning: stalwart did not become ready after bootstrap restart.",
        file=sys.stderr,
    )
    return False


def reload_stalwart_security(config: dict, secrets: dict) -> bool:
    """Reload settings and IP caches after API writes.

    Stalwart v0.16 persists registry changes without necessarily refreshing
    the listener's in-memory settings and blocklist. ReloadSettings activates
    the scan-ban/useXForwarded changes; ReloadBlockedIps clears stale bans and
    activates AllowedIp changes.
    """
    for action, label in (
        ("ReloadSettings", "settings"),
        ("ReloadBlockedIps", "blocked/allowed IP caches"),
    ):
        try:
            result = _jmap_ok(
                _jmap(
                    config,
                    secrets,
                    [
                        [
                            "x:Action/set",
                            {
                                "create": {
                                    f"reload-{action}": {
                                        "@type": action,
                                    }
                                }
                            },
                            "r",
                        ]
                    ],
                ),
                "r",
            )
            not_created = result.get("notCreated") or {}
            if not_created:
                raise RuntimeError(str(not_created))
            print(f"  reloaded {label}")
        except RuntimeError as exc:
            print(f"Warning: could not reload {label}: {exc}", file=sys.stderr)
            return False
    return True


def protect_caddy_from_autoban(config: dict, secrets: dict) -> None:
    """Unban Docker/Caddy IPs, allowlist 172.16.0.0/12, enable X-Forwarded-For.

    Uses curl inside the existing stalwart container against 127.0.0.1 so this
    still works while Caddy is banned. No extra container is started.
    """
    if subprocess.run(["docker", "inspect", "stalwart"], capture_output=True).returncode != 0:
        print("Skipping proxy allowlist: stalwart container is not running.", file=sys.stderr)
        return
    print("Allowlisting Docker networks in Stalwart so Caddy cannot be auto-banned…")
    try:
        blocked = _jmap_list(config, secrets, "BlockedIp")
    except RuntimeError as exc:
        if not _is_bootstrap_error(exc):
            print(
                f"Warning: could not query BlockedIp: {exc}. "
                "Re-run apply.sh --unlock-proxy once Stalwart is reachable.",
                file=sys.stderr,
            )
            return
        if not complete_bootstrap(config, secrets):
            return
        if not restart_stalwart_and_wait(config, secrets):
            return
        try:
            blocked = _jmap_list(config, secrets, "BlockedIp")
        except RuntimeError as exc2:
            print(f"Warning: could not query BlockedIp after bootstrap: {exc2}", file=sys.stderr)
            return
    if blocked:
        print(f"  BlockedIp entries: {len(blocked)}")
        for row in blocked:
            print(f"    {_row_address(row) or '?'} id={row.get('id')}")
    else:
        print("  BlockedIp entries: none returned by JMAP")
    banned_ids = []
    for row in blocked:
        addr = _row_address(row)
        row_id = str(row.get("id") or "")
        if row_id and address_is_docker_lan(addr or row_id):
            banned_ids.append(row_id)
            print(f"  removing BlockedIp {addr or row_id} ({row_id})")
    if banned_ids:
        try:
            destroyed = _jmap_ok(
                _jmap(
                    config,
                    secrets,
                    [["x:BlockedIp/set", {"destroy": banned_ids}, "d"]],
                ),
                "d",
            )
            not_destroyed = destroyed.get("notDestroyed") or {}
            if not_destroyed:
                print(f"Warning: failed to delete some BlockedIp entries: {not_destroyed}", file=sys.stderr)
        except RuntimeError as exc:
            print(f"Warning: failed to delete BlockedIp entries: {exc}", file=sys.stderr)
    try:
        allowed = _jmap_list(config, secrets, "AllowedIp")
    except RuntimeError as exc:
        print(f"Warning: could not query AllowedIp: {exc}", file=sys.stderr)
        allowed = []
    have_allow = {_row_address(row) for row in allowed}
    create_map: dict[str, dict] = {}
    for cidr in docker_allowlist_cidrs():
        if cidr in have_allow:
            print(f"  AllowedIp {cidr} already present")
            continue
        key = f"net{len(create_map)}"
        create_map[key] = {
            "address": cidr,
            "reason": "easydeploy docker networks (caddy)",
        }
    if create_map:
        try:
            created = _jmap_ok(
                _jmap(
                    config,
                    secrets,
                    [["x:AllowedIp/set", {"create": create_map}, "c"]],
                ),
                "c",
            )
            not_created = created.get("notCreated") or {}
            if not_created:
                print(f"Warning: failed to create AllowedIp: {not_created}", file=sys.stderr)
            else:
                for body in create_map.values():
                    print(f"  created AllowedIp {body['address']}")
        except RuntimeError as exc:
            print(f"Warning: failed to create AllowedIp: {exc}", file=sys.stderr)
    try:
        updated = _jmap_ok(
            _jmap(
                config,
                secrets,
                [["x:Http/set", {"update": {"singleton": {"useXForwarded": True}}}, "u"]],
            ),
            "u",
        )
        if updated.get("notUpdated"):
            raise RuntimeError(str(updated["notUpdated"]))
        print("  Http.useXForwarded=true")
    except RuntimeError as exc:
        print(f"Warning: could not set Http.useXForwarded=true: {exc}", file=sys.stderr)
    # AllowedIp does not stop auto-ban (Stalwart still writes BlockedIp). Disable
    # URL scan-bans so Caddy is not locked out by internet probes it forwards.
    try:
        updated = _jmap_ok(
            _jmap(
                config,
                secrets,
                [
                    [
                        "x:Security/set",
                        {
                            "update": {
                                "singleton": {
                                    "scanBanPaths": {},
                                    "scanBanRate": {"count": 1000000, "period": 86400000},
                                }
                            }
                        },
                        "s",
                    ]
                ],
            ),
            "s",
        )
        if updated.get("notUpdated"):
            raise RuntimeError(str(updated["notUpdated"]))
        print("  Security.scanBanPaths cleared (Caddy is not treated as a scanner)")
    except RuntimeError as exc:
        print(f"Warning: could not update Security scan-ban: {exc}", file=sys.stderr)
    if not reload_stalwart_security(config, secrets):
        print("  Hot reload failed; restarting stalwart to flush stale IP caches…")
        restart_stalwart_and_wait(config, secrets)


IDENTITY_SIDECAR = INTEGRATION_DIR / "identity-provider.yaml"
KANIDM_DIRECTORY_DESCRIPTION = "Kanidm"
KANIDM_OIDC_DIRECTORY_DESCRIPTION = "Kanidm OIDC"


def managed_is_false(section: dict | None) -> bool:
    value = (section or {}).get("managed")
    if value is False:
        return True
    return str(value or "").strip().lower() in {"false", "no", "0"}


def apply_engine_identity_sidecar(config: dict, sidecar_path: Path | None = None) -> dict:
    """Merge Kanidm identity settings from the engine sidecar. Operator deploy.yaml wins."""
    path = sidecar_path or IDENTITY_SIDECAR
    identity = config.get("identity")
    if not isinstance(identity, dict):
        identity = {}
        config["identity"] = identity
    if managed_is_false(identity):
        return identity
    existing_provider = str(identity.get("provider") or "").strip().lower()
    if existing_provider and existing_provider != "kanidm":
        return identity
    if path is None or not path.is_file():
        return identity
    sidecar = load_yaml(path)
    for key, value in sidecar.items():
        if key == "managed":
            continue
        if key in {"ldap", "oidc"} and isinstance(value, dict):
            section = identity.setdefault(key, {})
            if not isinstance(section, dict):
                identity[key] = dict(value)
                continue
            for nested_key, nested_value in value.items():
                if section.get(nested_key) in (None, ""):
                    section[nested_key] = nested_value
            continue
        if identity.get(key) in (None, ""):
            identity[key] = value
    identity.setdefault("provider", "kanidm")
    return identity


def _sibling_kanidm_ldap_token() -> str:
    secrets_path = PROJECT_ROOT.parent / "kanidm-easy-deploy" / ".kanidm-easy-deploy" / "secrets.yaml"
    if not secrets_path.is_file():
        return ""
    return str(load_yaml(secrets_path).get("LDAP_TOKEN") or "").strip()


def build_ldap_directory(identity: dict, secrets: dict) -> dict | None:
    ldap = identity.get("ldap") if isinstance(identity.get("ldap"), dict) else {}
    bind_secret = str(
        ldap.get("bind_secret") or secrets.get("LDAP_TOKEN") or _sibling_kanidm_ldap_token() or ""
    ).strip()
    base_dn = str(ldap.get("base_dn") or "").strip()
    if not bind_secret or not base_dn:
        return None
    return {
        "@type": "Ldap",
        "description": KANIDM_DIRECTORY_DESCRIPTION,
        "url": str(ldap.get("url") or "ldaps://kanidm:3636").strip(),
        "baseDn": base_dn,
        "bindDn": str(ldap.get("bind_dn") or "dn=token").strip(),
        "bindSecret": {"@type": "Value", "secret": bind_secret},
        "bindAuthentication": True,
        "allowInvalidCerts": True,
        "useTls": True,
        "filterLogin": str(
            ldap.get("filter_login")
            or "(&(|(objectclass=account)(objectclass=person))(|(name=?)(uid=?)(mail=?)(spn=?)))"
        ),
        "filterMailbox": str(
            ldap.get("filter_mailbox")
            or "(&(|(objectclass=account)(objectclass=person))(|(mail=?)(spn=?)))"
        ),
        "attrEmail": {str(ldap.get("attr_email") or "mail"): True, "emailprimary": True},
        "attrDescription": {str(ldap.get("attr_description") or "displayname"): True},
        "attrMemberOf": {str(ldap.get("attr_member_of") or "memberof"): True},
        "attrClass": {"objectclass": True},
        "groupClass": str(ldap.get("group_class") or "group"),
    }


def build_oidc_directory(identity: dict) -> dict | None:
    oidc = identity.get("oidc") if isinstance(identity.get("oidc"), dict) else {}
    issuer = str(oidc.get("issuer_url") or "").strip()
    if not issuer:
        return None
    client_id = str(oidc.get("client_id") or "stalwart-webui").strip() or "stalwart-webui"
    payload: dict[str, Any] = {
        "@type": "Oidc",
        "description": KANIDM_OIDC_DIRECTORY_DESCRIPTION,
        "issuerUrl": issuer,
        "requireAudience": str(oidc.get("require_audience") or client_id),
        "requireScopes": {"openid": True, "email": True, "profile": True},
        "claimUsername": str(oidc.get("claim_username") or "preferred_username"),
        "claimName": str(oidc.get("claim_name") or "name"),
        "claimGroups": str(oidc.get("claim_groups") or "groups"),
    }
    username_domain = str(oidc.get("username_domain") or "").strip()
    if username_domain:
        payload["usernameDomain"] = username_domain
    return payload


def _directory_matches(item: dict, payload: dict) -> bool:
    wanted = str(payload.get("description") or "").strip()
    have = str(item.get("description") or "").strip()
    if wanted and have == wanted:
        return True
    kind = str(payload.get("@type") or "").strip().lower()
    if kind == "ldap":
        url = str(item.get("url") or item.get("issuerUrl") or "")
        target = str(payload.get("url") or "")
        return bool(target) and (url == target or "kanidm:3636" in url)
    if kind == "oidc":
        issuer = str(item.get("issuerUrl") or item.get("url") or "")
        target = str(payload.get("issuerUrl") or "")
        return bool(target) and issuer == target
    return False


def _destroy_directories(config: dict, secrets: dict, ids: list[str]) -> None:
    leftover = [item for item in ids if item]
    if not leftover:
        return
    destroyed = _jmap_ok(
        _jmap(
            config,
            secrets,
            [["x:Directory/set", {"destroy": leftover}, "d"]],
        ),
        "d",
    )
    if destroyed.get("notDestroyed"):
        raise RuntimeError(str(destroyed["notDestroyed"]))


def _upsert_directory(
    config: dict,
    secrets: dict,
    directories: list[dict],
    description: str,
    payload: dict,
    create_key: str,
) -> str:
    matches = [item for item in directories if isinstance(item, dict) and _directory_matches(item, payload)]
    existing = next((item for item in matches if item.get("id")), None)
    extras = [str(item["id"]) for item in matches[1:] if item.get("id")]
    if existing and existing.get("id"):
        directory_id = str(existing["id"])
        updated = _jmap_ok(
            _jmap(
                config,
                secrets,
                [["x:Directory/set", {"update": {directory_id: payload}}, "d"]],
            ),
            "d",
        )
        if updated.get("notUpdated"):
            raise RuntimeError(str(updated["notUpdated"]))
        if extras:
            _destroy_directories(config, secrets, extras)
            print(f"  Removed {len(extras)} duplicate Stalwart director{'y' if len(extras) == 1 else 'ies'}")
        return directory_id
    created = _jmap_ok(
        _jmap(
            config,
            secrets,
            [["x:Directory/set", {"create": {create_key: payload}}, "d"]],
        ),
        "d",
    )
    if created.get("notCreated"):
        raise RuntimeError(str(created["notCreated"]))
    created_obj = (created.get("created") or {}).get(create_key) or {}
    directory_id = str(created_obj.get("id") or "")
    if not directory_id:
        raise RuntimeError("Directory/set did not return an id")
    if extras:
        _destroy_directories(config, secrets, extras)
    return directory_id


def apply_kanidm_directory(config: dict, secrets: dict) -> None:
    """Select Kanidm OIDC for browser SSO. LDAP stays registered; IMAP uses app passwords."""
    operator_identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    operator_prefer = str(operator_identity.get("auth_directory") or "").strip().lower()
    identity = apply_engine_identity_sidecar(config)
    if str(identity.get("provider") or "").strip().lower() != "kanidm":
        return
    if managed_is_false(identity):
        return
    ldap_payload = build_ldap_directory(identity, secrets)
    if ldap_payload is None:
        raise RuntimeError(
            "Kanidm identity is enabled but Stalwart has no LDAP bind secret or base DN. "
            "Re-apply kanidm-easy-deploy (so LDAP_TOKEN exists), then re-apply Stalwart."
        )
    oidc_payload = build_oidc_directory(identity)
    try:
        directories = _jmap_list(config, secrets, "Directory")
    except RuntimeError as exc:
        raise RuntimeError(f"Could not list Stalwart directories for Kanidm: {exc}") from exc
    try:
        ldap_id = _upsert_directory(
            config, secrets, directories, KANIDM_DIRECTORY_DESCRIPTION, ldap_payload, "kanidm"
        )
        directories = _jmap_list(config, secrets, "Directory")
        oidc_id = ""
        if oidc_payload:
            oidc_id = _upsert_directory(
                config,
                secrets,
                directories,
                KANIDM_OIDC_DIRECTORY_DESCRIPTION,
                oidc_payload,
                "kanidmOidc",
            )
        use_oidc = bool(oidc_id) and operator_prefer != "ldap"
        if use_oidc:
            directory_id = oidc_id
            kind = f"OIDC ({oidc_payload['issuerUrl']})"
        else:
            directory_id = ldap_id
            kind = f"LDAP ({ldap_payload['url']}, {ldap_payload['baseDn']})"
        auth = _jmap_ok(
            _jmap(
                config,
                secrets,
                [
                    [
                        "x:Authentication/set",
                        {"update": {"singleton": {"directoryId": directory_id}}},
                        "a",
                    ]
                ],
            ),
            "a",
        )
        if auth.get("notUpdated"):
            raise RuntimeError(str(auth["notUpdated"]))
    except RuntimeError as exc:
        raise RuntimeError(f"Could not apply Kanidm directory: {exc}") from exc
    secrets["KANIDM_DIRECTORY_ID"] = directory_id
    secrets["KANIDM_LDAP_DIRECTORY_ID"] = ldap_id
    if oidc_id:
        secrets["KANIDM_OIDC_DIRECTORY_ID"] = oidc_id
    save_yaml(SECRETS_PATH, secrets)
    print(f"  Stalwart authentication directory is Kanidm {kind}")
    if use_oidc:
        print("  Bulwark SSO uses Kanidm. IMAP/SMTP should use a Stalwart app password.")
    elif oidc_id:
        print("  Kanidm OIDC directory is registered but not selected (identity.auth_directory: ldap)")


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
    secrets = load_or_create_secrets(config)
    protect_caddy_from_autoban(config, secrets)
    apply_kanidm_directory(config, secrets)


def print_summary(config: dict, secrets: dict) -> None:
    apply_engine_identity_sidecar(config)
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
    if stalwart_https_upstream(config):
        print("Caddy upstream:  https://stalwart:443")
    else:
        print("Caddy upstream:  http://stalwart:8080 (Caddy terminates TLS)")
    print(f"Secrets file:    {SECRETS_PATH}")
    print(f"Recovery admin:  {recovery_user} / {secrets.get('RECOVERY_ADMIN_PASSWORD')}")
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    if str(identity.get("provider") or "").strip().lower() == "kanidm":
        if str(identity.get("auth_directory") or "oidc").strip().lower() == "ldap":
            print("Identity:        Kanidm LDAP (IMAP/SMTP/WebUI password bind)")
            print("                 Log in as the Kanidm username with a Kanidm *password*.")
        else:
            print("Identity:        Kanidm OIDC (Bulwark / portal SSO)")
            print("                 Webmail signs in through Kanidm (passkey or session).")
            print("                 IMAP/SMTP: create a Stalwart app password in the admin UI.")
            print("                 New mailboxes appear after the first successful SSO.")
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
    print("  2. apply.sh completes the Stalwart bootstrap (hostname, domain,")
    print("     console logs, no ACME — Caddy already terminates HTTPS).")
    print(f"  3. Open https://{hostname}/admin with the recovery admin above")
    print("     (or STALWART_ADMIN_PASSWORD in the secrets file if printed).")
    print("     In integrate mode also run engine apply.sh --skip-kits.")
    print("  4. Publish MX / SPF / DKIM / DMARC from the Stalwart WebUI DNS zone.")
    print("  5. Mail ports 25/465/587/993/4190 are bound on the host, not via Caddy.")
    print()


def apply_configuration(
    *, skip_runtime: bool = False, skip_pull: bool = False, unlock_proxy: bool = False
) -> None:
    config = load_config()
    validate_config(config)
    secrets = load_or_create_secrets(config)
    if unlock_proxy:
        render_runtime_artifacts(config, secrets)
        protect_caddy_from_autoban(config, secrets)
        if proxy_mode(config) == "integrate":
            print(
                "Caddy fragment updated. Reload shared Caddy with: "
                "easydeploy-engine apply.sh --skip-kits"
            )
        return
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
    parser.add_argument(
        "--unlock-proxy",
        action="store_true",
        help="Unban Docker/Caddy IPs and allowlist 172.16.0.0/12; do not re-apply compose",
    )
    args = parser.parse_args()
    try:
        apply_configuration(
            skip_runtime=args.skip_runtime,
            skip_pull=args.skip_pull,
            unlock_proxy=args.unlock_proxy,
        )
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
