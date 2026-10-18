"""Split delivery between Stalwart and Microsoft 365.

When ``mail.fallback.host`` is set, this domain accepts mail for people who
are not local yet and relays it to Exchange Online — except mail that
Exchange Online itself just handed over. Relaying that back is a loop.
Those recipients are delivered locally instead, which bounces them.
"""

from __future__ import annotations

from typing import Any

# Exchange Online Protection SMTP ranges, endpoints.office.com worldwide id 10,
# fetched 2026-10-17. Override with mail.fallback.source_cidrs if Microsoft
# publishes a new set and a loop test starts failing.
DEFAULT_MICROSOFT_CIDRS = (
    "40.92.0.0/15",
    "40.107.0.0/16",
    "52.100.0.0/14",
    "104.47.0.0/17",
    "2a01:111:f400::/48",
    "2a01:111:f403::/48",
)
MICROSOFT_ROUTE = "microsoft"
COEXISTENCE_SPF = "v=spf1 mx include:spf.protection.outlook.com -all"
STANDALONE_SPF = "v=spf1 mx -all"


def _is_false(value: Any) -> bool:
    if value is False:
        return True
    return str(value or "").strip().lower() in {"false", "no", "0"}


def _cidrs(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_MICROSOFT_CIDRS
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("mail.fallback.source_cidrs must be a list of CIDR blocks")
    for item in items:
        if " " in item or "/" not in item:
            raise ValueError(f"mail.fallback.source_cidrs entry is not a CIDR: {item}")
    return tuple(items) or DEFAULT_MICROSOFT_CIDRS


def fallback_settings(config: dict) -> dict | None:
    """Enabled fallback, or None when split delivery is off."""
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    fallback = mail.get("fallback") if isinstance(mail.get("fallback"), dict) else None
    if not fallback or _is_false(fallback.get("enabled", True)):
        return None
    host = str(fallback.get("host") or "").strip().rstrip(".").lower()
    if not host or host in {"example.com", "mail.example.com"}:
        return None
    if "://" in host or " " in host or "@" in host:
        raise ValueError("mail.fallback.host must be a hostname, not a URL")
    port = int(fallback.get("port") or 25)
    if port < 1 or port > 65535:
        raise ValueError("mail.fallback.port must be between 1 and 65535")
    return {
        "host": host,
        "port": port,
        "source_cidrs": _cidrs(fallback.get("source_cidrs")),
    }


def spf_value(config: dict) -> str:
    if fallback_settings(config):
        return COEXISTENCE_SPF
    return STANDALONE_SPF


def _microsoft_source_expr(cidrs: tuple[str, ...]) -> str:
    checks = [f"is_ip_in_cidr(received_from_ip, '{cidr}')" for cidr in cidrs]
    if not checks:
        return "false"
    if len(checks) == 1:
        return checks[0]
    return "(" + " || ".join(checks) + ")"


def fallback_route_expression(cidrs: tuple[str, ...]) -> dict:
    """Local accounts stay here. Other same-domain recipients go to Microsoft,
    unless Microsoft is the host that just delivered them."""
    from_microsoft = _microsoft_source_expr(cidrs)
    return {
        "match": {
            "0": {"if": "is_local_address(rcpt)", "then": "'local'"},
            "1": {
                "if": f"is_local_domain(rcpt_domain) && !({from_microsoft})",
                "then": f"'{MICROSOFT_ROUTE}'",
            },
            "2": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"},
        },
        "else": "'mx'",
    }


def standalone_route_expression() -> dict:
    return {
        "match": {
            "0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"},
        },
        "else": "'mx'",
    }


def relay_route_payload(settings: dict) -> dict:
    return {
        "@type": "Relay",
        "name": MICROSOFT_ROUTE,
        "description": "OpenComp fallback to Microsoft 365",
        "address": settings["host"],
        "port": settings["port"],
        "protocol": "smtp",
        "implicitTls": False,
        "allowInvalidCerts": False,
        "authSecret": {"@type": "None"},
    }


def _domain_named(domains: list[dict], name: str) -> dict | None:
    wanted = name.strip().lower().rstrip(".")
    for domain in domains:
        if str(domain.get("name") or "").strip().lower().rstrip(".") == wanted:
            return domain
    return None


def _route_named(routes: list[dict], name: str) -> dict | None:
    for route in routes:
        if str(route.get("name") or "").strip() == name:
            return route
    return None


def plan_fallback(
    config: dict,
    *,
    domains: list[dict],
    routes: list[dict],
) -> dict:
    """JMAP updates for split delivery. Empty when nothing has to change."""
    stalwart = config.get("stalwart") if isinstance(config.get("stalwart"), dict) else {}
    domain_name = str(stalwart.get("domain") or "").strip()
    domain = _domain_named(domains, domain_name) if domain_name else None
    existing = _route_named(routes, MICROSOFT_ROUTE)
    settings = fallback_settings(config)
    changes: dict[str, Any] = {}
    if settings is None:
        if domain and domain.get("id") and domain.get("allowRelaying") is True:
            changes["domain"] = {"id": str(domain["id"]), "allowRelaying": False}
        if existing and existing.get("id"):
            changes["destroy_route_id"] = str(existing["id"])
            changes["route_expression"] = standalone_route_expression()
        return changes
    if domain is None or not domain.get("id"):
        raise RuntimeError(
            f"Stalwart has no domain {domain_name!r} yet, so Microsoft fallback cannot be applied"
        )
    changes["domain"] = {"id": str(domain["id"]), "allowRelaying": True}
    changes["route_expression"] = fallback_route_expression(settings["source_cidrs"])
    payload = relay_route_payload(settings)
    if existing and existing.get("id"):
        changes["update_route"] = {"id": str(existing["id"]), "payload": payload}
    else:
        changes["create_route"] = payload
    return changes
