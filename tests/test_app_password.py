from __future__ import annotations

import pytest

from scripts.app_password import create_app_password, find_account


def test_find_account_matches_email_or_local_part():
    accounts = [
        {"id": "a", "name": "alice", "emailAddress": "alice@acme.test"},
        {"id": "b", "name": "bob", "emailAddress": "bob@acme.test"},
    ]
    assert find_account(accounts, "alice@acme.test")["id"] == "a"
    assert find_account(accounts, "alice")["id"] == "a"
    assert find_account(accounts, "BOB@acme.test")["id"] == "b"


def test_find_account_requires_a_unique_local_part():
    accounts = [
        {"id": "a", "name": "alice", "emailAddress": "alice@acme.test"},
        {"id": "b", "name": "alice", "emailAddress": "alice@other.test"},
    ]
    with pytest.raises(LookupError, match="Several mailboxes"):
        find_account(accounts, "alice")
    assert find_account(accounts, "alice@other.test")["id"] == "b"


def test_find_account_explains_a_missing_mailbox():
    with pytest.raises(LookupError, match="mail-users"):
        find_account([], "missing@acme.test")


def test_create_app_password_returns_the_secret_once():
    def jmap(calls, using=None):
        assert using is not None
        assert calls[0][0] == "x:AppPassword/set"
        assert calls[0][1]["accountId"] == "acc1"
        assert calls[0][1]["create"]["p1"]["description"] == "Phone"
        return {
            "methodResponses": [
                [
                    "x:AppPassword/set",
                    {"created": {"p1": {"id": "pw1", "secret": "app_secret"}}},
                    "0",
                ]
            ]
        }

    assert create_app_password(jmap, "acc1", description="Phone") == "app_secret"


def test_create_app_password_surfaces_stalwart_errors():
    def jmap(calls, using=None):
        return {
            "methodResponses": [
                [
                    "x:AppPassword/set",
                    {"notCreated": {"p1": {"type": "forbidden", "description": "nope"}}},
                    "0",
                ]
            ]
        }

    with pytest.raises(RuntimeError, match="nope"):
        create_app_password(jmap, "acc1", description="Phone")
