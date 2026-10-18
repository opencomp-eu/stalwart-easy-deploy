"""mail-users is the mailbox cutover switch."""

from __future__ import annotations

from scripts.groupware import Person
from scripts.mailboxes import (
    kanidm_mailbox_filter,
    mailbox_filter_for,
    person_in_group,
    plan_mailboxes,
)


def test_mailbox_filter_requires_mail_users_and_keeps_an_operator_override():
    filt = kanidm_mailbox_filter("dc=idm,dc=example,dc=com", "idm.example.com")
    assert "memberof=spn=mail-users@idm.example.com,dc=idm,dc=example,dc=com" in filt
    assert "mail=?" in filt
    assert mailbox_filter_for("dc=idm,dc=example,dc=com", "") == filt
    assert mailbox_filter_for("dc=idm,dc=example,dc=com", "(mail=?)") == "(mail=?)"


def test_group_membership_accepts_spn_and_dn_forms():
    person = Person(
        uuid="1",
        name="ada",
        display_name="Ada",
        emails=("ada@example.com",),
        groups=("spn=mail-users@idm.example.com,dc=idm,dc=example,dc=com",),
    )
    other = Person(
        uuid="2",
        name="bea",
        display_name="Bea",
        emails=("bea@example.com",),
        groups=("opencloud-user@idm.example.com",),
    )
    assert person_in_group(person)
    assert not person_in_group(other)


def test_plan_creates_members_and_deletes_only_tracked_leavers():
    desired = {"ada@example.com": "ada"}
    existing = {"ada@example.com": "acc-ada", "shared@example.com": "acc-shared", "bea@example.com": "acc-bea"}
    to_create, to_delete, tracked = plan_mailboxes(
        desired,
        existing,
        {"bea@example.com"},
        reserved=set(),
    )
    assert to_create == {}
    assert to_delete == {"bea@example.com": "acc-bea"}
    assert "shared@example.com" not in to_delete
    assert "ada@example.com" in tracked
    assert "bea@example.com" not in tracked

    created, deleted, next_tracked = plan_mailboxes(
        {"new@example.com": "new"},
        {},
        set(),
        reserved={"admin"},
    )
    assert created == {"new@example.com": "new"}
    assert deleted == {}
    assert next_tracked == {"new@example.com"}

    skipped, _, _ = plan_mailboxes(
        {"admin@example.com": "admin"},
        {},
        set(),
        reserved={"admin"},
    )
    assert skipped == {}
