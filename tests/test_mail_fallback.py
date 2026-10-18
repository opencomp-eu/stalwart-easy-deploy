"""Split delivery to Microsoft 365."""

from __future__ import annotations

from scripts.mail_fallback import (
    COEXISTENCE_SPF,
    MICROSOFT_ROUTE,
    STANDALONE_SPF,
    fallback_route_expression,
    fallback_settings,
    plan_fallback,
    spf_value,
)


def test_fallback_is_off_until_a_real_host_is_set():
    assert fallback_settings({}) is None
    assert fallback_settings({"mail": {"fallback": {"host": "example.com"}}}) is None
    assert fallback_settings({"mail": {"fallback": {"enabled": False, "host": "contoso.mail.protection.outlook.com"}}}) is None
    settings = fallback_settings({"mail": {"fallback": {"host": "Contoso.mail.protection.outlook.com"}}})
    assert settings is not None
    assert settings["host"] == "contoso.mail.protection.outlook.com"
    assert settings["port"] == 25
    assert "40.92.0.0/15" in settings["source_cidrs"]


def test_route_relays_unknown_recipients_except_mail_from_microsoft():
    route = fallback_route_expression(("40.92.0.0/15",))
    first, second, third = route["match"]["0"], route["match"]["1"], route["match"]["2"]
    assert first["then"] == "'local'"
    assert "is_local_address(rcpt)" in first["if"]
    assert f"'{MICROSOFT_ROUTE}'" == second["then"]
    assert "is_ip_in_cidr(received_from_ip, '40.92.0.0/15')" in second["if"]
    assert second["if"].startswith("is_local_domain(rcpt_domain) && !(")
    assert third == {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}
    assert route["else"] == "'mx'"


def test_plan_enables_relaying_and_a_microsoft_route():
    changes = plan_fallback(
        {
            "stalwart": {"domain": "example.com"},
            "mail": {"fallback": {"host": "example-com.mail.protection.outlook.com"}},
        },
        domains=[{"id": "dom-1", "name": "example.com", "allowRelaying": False}],
        routes=[],
    )
    assert changes["domain"] == {"id": "dom-1", "allowRelaying": True}
    assert changes["create_route"]["address"] == "example-com.mail.protection.outlook.com"
    assert changes["create_route"]["@type"] == "Relay"
    assert "route_expression" in changes


def test_plan_turns_fallback_off_without_touching_a_clean_server():
    assert (
        plan_fallback(
            {"stalwart": {"domain": "example.com"}},
            domains=[{"id": "dom-1", "name": "example.com", "allowRelaying": False}],
            routes=[],
        )
        == {}
    )
    changes = plan_fallback(
        {"stalwart": {"domain": "example.com"}},
        domains=[{"id": "dom-1", "name": "example.com", "allowRelaying": True}],
        routes=[{"id": "route-1", "name": MICROSOFT_ROUTE}],
    )
    assert changes["domain"]["allowRelaying"] is False
    assert changes["destroy_route_id"] == "route-1"
    assert changes["route_expression"]["else"] == "'mx'"
    assert MICROSOFT_ROUTE not in changes["route_expression"]["match"]["0"]["then"]


def test_spf_includes_microsoft_only_while_fallback_is_on():
    plain = {"stalwart": {"domain": "example.com"}}
    shared = {
        "stalwart": {"domain": "example.com"},
        "mail": {"fallback": {"host": "example-com.mail.protection.outlook.com"}},
    }
    assert spf_value(plain) == STANDALONE_SPF
    assert spf_value(shared) == COEXISTENCE_SPF
