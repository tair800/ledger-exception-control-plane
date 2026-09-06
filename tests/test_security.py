"""M5.1 — the authentication boundary, with no database and no HTTP.

Every property here is about the registry and the role model, so none of it needs a server. The
routes that consume this are exercised in ``test_approval_api_postgres.py``; what is checked here is
the part an attacker actually touches.
"""

from __future__ import annotations

import json

import pytest

from ledger_exception_control_plane.security import (
    APPROVAL_ROLES,
    EDIT_ROLES,
    OPERATIONS_ROLES,
    Principal,
    PrincipalRegistry,
    Role,
    token_fingerprint,
)

ANALYST_TOKEN = "analyst-token-0123456789"
CONTROLLER_TOKEN = "controller-token-0123456789"
OPERATOR_TOKEN = "operator-token-0123456789"


def _registry_json() -> str:
    return json.dumps(
        {
            "analyst-a": {"role": "analyst", "token_sha256": token_fingerprint(ANALYST_TOKEN)},
            "controller-a": {
                "role": "controller",
                "token_sha256": token_fingerprint(CONTROLLER_TOKEN),
            },
            "operator-a": {"role": "operator", "token_sha256": token_fingerprint(OPERATOR_TOKEN)},
        }
    )


# ======================================================================================
# The registry holds hashes, not credentials
# ======================================================================================


def test_the_registry_never_contains_a_usable_token() -> None:
    """**The property that makes this configuration safe to ship in a file.**

    A deployment's principal registry ends up in an environment variable, a secret store, a crash
    dump and occasionally a support ticket. Storing the SHA-256 means every one of those carries a
    value that cannot be replayed.
    """
    raw = _registry_json()

    for token in (ANALYST_TOKEN, CONTROLLER_TOKEN, OPERATOR_TOKEN):
        assert token not in raw

    parsed = json.loads(raw)
    for entry in parsed.values():
        assert len(entry["token_sha256"]) == 64
        assert set(entry["token_sha256"]) <= set("0123456789abcdef")


def test_a_token_resolves_to_its_principal_and_role() -> None:
    registry = PrincipalRegistry.from_json(_registry_json())

    assert registry.authenticate(ANALYST_TOKEN) == Principal("analyst-a", Role.ANALYST)
    assert registry.authenticate(CONTROLLER_TOKEN) == Principal("controller-a", Role.CONTROLLER)
    assert registry.authenticate(OPERATOR_TOKEN) == Principal("operator-a", Role.OPERATOR)


@pytest.mark.parametrize(
    ("label", "token"),
    [
        ("an unknown token", "not-a-real-token"),
        ("an empty token", ""),
        ("a prefix of a real token", ANALYST_TOKEN[:-1]),
        ("a real token with a suffix", ANALYST_TOKEN + "x"),
        ("the fingerprint itself", token_fingerprint(ANALYST_TOKEN)),
    ],
)
def test_anything_that_is_not_a_configured_token_authenticates_as_nobody(
    label: str, token: str
) -> None:
    """Including the fingerprint: knowing the stored value must not be enough to present it.

    That last case is the one that matters if the registry ever leaks. If presenting the hash
    authenticated, storing the hash would have bought nothing.
    """
    assert PrincipalRegistry.from_json(_registry_json()).authenticate(token) is None


def test_an_empty_registry_authenticates_nobody() -> None:
    """A control plane with no configured humans fails closed.

    The alternative — treating "no principals configured" as "no authentication required" — is the
    single most common way an admin surface ends up open, and it fails open exactly when somebody
    forgot to configure it.
    """
    registry = PrincipalRegistry.from_json("")

    assert len(registry) == 0
    assert registry.authenticate(ANALYST_TOKEN) is None
    assert registry.authenticate("") is None


# ======================================================================================
# A malformed registry fails at startup, not at the first approval
# ======================================================================================


@pytest.mark.parametrize(
    ("label", "raw", "message"),
    [
        ("not JSON", "{not json", "not valid JSON"),
        ("not an object", '["analyst-a"]', "JSON object"),
        ("an entry that is not an object", '{"a": "analyst"}', "must map to an object"),
        (
            "an unknown role",
            '{"a": {"role": "admin", "token_sha256": "' + "0" * 64 + '"}}',
            "permitted roles are",
        ),
        ("no role at all", '{"a": {"token_sha256": "' + "0" * 64 + '"}}', "permitted roles are"),
        (
            "a token instead of a hash",
            '{"a": {"role": "analyst", "token_sha256": "hunter2"}}',
            "64 hex characters",
        ),
        ("a missing hash", '{"a": {"role": "analyst"}}', "64 hex characters"),
        (
            "a blank principal id",
            '{"  ": {"role": "analyst", "token_sha256": "' + "0" * 64 + '"}}',
            "may not be blank",
        ),
    ],
)
def test_a_malformed_registry_is_refused_at_construction(
    label: str, raw: str, message: str
) -> None:
    """Each refusal is a real failure mode, not defensive noise.

    An unknown role would grant whatever the code defaulted to; a plaintext token in the hash field
    means somebody pasted a credential into configuration; a blank id would produce audit rows
    attributing a financial decision to nobody.
    """
    with pytest.raises(ValueError, match=message):
        PrincipalRegistry.from_json(raw)


def test_two_principals_may_not_share_a_token() -> None:
    """**An audit trail that cannot tell two actors apart is not an audit trail.**

    §16 requires role separation and the countersignature rule; both are meaningless if two
    principals present the same credential, because the row would record whichever one the lookup
    happened to return.
    """
    shared = token_fingerprint("shared-token")
    raw = json.dumps(
        {
            "analyst-a": {"role": "analyst", "token_sha256": shared},
            "controller-a": {"role": "controller", "token_sha256": shared},
        }
    )

    with pytest.raises(ValueError, match="shares a token"):
        PrincipalRegistry.from_json(raw)


# ======================================================================================
# The role model
# ======================================================================================


def test_the_roles_are_exactly_the_three_the_specification_names() -> None:
    """§16: *"role separation between analyst, controller and operator"*. Three, not a hierarchy."""
    assert {role.value for role in Role} == {"analyst", "controller", "operator"}


def test_only_the_controller_may_authorise_an_edit() -> None:
    assert frozenset({Role.CONTROLLER}) == EDIT_ROLES
    assert Principal("c", Role.CONTROLLER).may_edit_treatment() is True
    assert Principal("a", Role.ANALYST).may_edit_treatment() is False
    assert Principal("o", Role.OPERATOR).may_edit_treatment() is False


def test_the_operator_holds_no_approval_right() -> None:
    """The separation the whole design rests on, asserted as a property rather than a comment."""
    assert Role.OPERATOR not in APPROVAL_ROLES
    assert Principal("o", Role.OPERATOR).may_approve() is False
    assert Principal("o", Role.OPERATOR).may_work_operations_queues() is True


def test_no_approval_role_may_work_the_operations_queues() -> None:
    """The other direction. A controller who could also replay a dead letter would be able to
    authorise a posting and then drive it, which is the same collapse from the other side."""
    assert APPROVAL_ROLES.isdisjoint(OPERATIONS_ROLES)
    assert Principal("c", Role.CONTROLLER).may_work_operations_queues() is False
    assert Principal("a", Role.ANALYST).may_work_operations_queues() is False


def test_a_principal_carries_exactly_one_role() -> None:
    """No permission sets, no role lists, no RBAC engine — the complexity the rules forbid."""
    import dataclasses

    fields = {field.name for field in dataclasses.fields(Principal)}
    assert fields == {"id", "role"}
