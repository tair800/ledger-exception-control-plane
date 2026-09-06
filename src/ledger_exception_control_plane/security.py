"""Authenticated principals and role separation (increment 5.1).

`PROJECT_SPEC.md` §16: *"Authenticated principals for every approval; **role separation** between
analyst, controller and operator."* And: *"The approver cannot be the same principal as the
requester where an edit changed the treatment."*

**OPEN-8 is resolved here, and deliberately not with OAuth.** The open decision asked how principals
authenticate and how roles are assigned, with one constraint recorded against it: *"Deliberately
**not** OAuth — project 4 owns that territory, and duplicating it here would be redundant."* So this
is the smallest mechanism that satisfies §16 honestly:

- A **registry of principals**, each with an id and exactly one role, supplied as configuration.
- A **bearer token per principal**, presented as ``Authorization: Bearer <token>``.
- The registry stores **only the SHA-256 of each token**, never the token. A leaked configuration
  file or an accidental log line therefore yields a hash, not a credential.
- Comparison is constant-time, because a token check that returns early on the first wrong byte is a
  token check an attacker can walk one byte at a time.

That is not a production identity system and this module does not pretend otherwise: there is no
rotation, no expiry, no revocation list and no session. It is a demonstrable, testable control that
makes "which human authorised this financial write" a *verified* fact rather than a client-supplied
string — which is the property §16 actually needs and the property every later increment depends on.

**The roles, and why the split is where it is.**

- ``ANALYST`` reads the queue and may **reject** a proposal outright, because rejecting
  authorises no money to move. An analyst may also *request* a different treatment, but may not
  authorise one.
- ``CONTROLLER`` may approve, and is the only role that may authorise an **edited** treatment —
  subject to §16's countersignature rule: the controller authorising an edit may not be the
  principal who requested it.
- ``OPERATOR`` works the dead-letter queue and the recovery queue. It is deliberately **not** an
  approval role: the operator's job is to make a stalled dispatch move, and giving that job the
  power to authorise the amount it is unsticking would collapse the separation the design rests on.

A role is a single value, not a permission set. There is no RBAC engine here, and adding one would
be the enterprise complexity this project's rules forbid.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import hmac
import json
from typing import Final

__all__ = [
    "APPROVAL_ROLES",
    "EDIT_ROLES",
    "OPERATIONS_ROLES",
    "Principal",
    "PrincipalRegistry",
    "Role",
    "token_fingerprint",
]


class Role(enum.StrEnum):
    """The three roles §16 names. Closed, and stored as text like every other enum here."""

    #: Reads the queue; may reject; may request an edited treatment but never authorise one.
    ANALYST = "analyst"

    #: May approve, and is the only role that may authorise an edited treatment.
    CONTROLLER = "controller"

    #: Works the dead-letter and recovery queues. Deliberately holds no approval right.
    OPERATOR = "operator"


#: Roles permitted to record an approval decision at all.
APPROVAL_ROLES: Final[frozenset[Role]] = frozenset({Role.ANALYST, Role.CONTROLLER})

#: Roles permitted to authorise a treatment **different** from the one proposed.
#:
#: Only the controller. §16's countersignature rule is enforced on top of this: being a controller
#: is necessary but not sufficient — the controller must also differ from the requester.
EDIT_ROLES: Final[frozenset[Role]] = frozenset({Role.CONTROLLER})

#: Roles permitted to work the dead-letter queue and the recovery queue.
OPERATIONS_ROLES: Final[frozenset[Role]] = frozenset({Role.OPERATOR})


def token_fingerprint(token: str) -> str:
    """The SHA-256 of a bearer token, hex-encoded.

    The registry holds these rather than tokens, so the configuration a deployment ships — and any
    log line, error message or crash dump that happens to include it — carries no usable credential.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated human actor: who they are and what they may do.

    ``id`` is what lands in ``approval.principal`` and in every audit event, so it is the value an
    auditor reads to answer "who authorised this". It is deliberately a plain string rather than a
    foreign key: the identity provider is outside this system, and inventing a local user table
    would claim an authority this service does not have.
    """

    id: str
    role: Role

    def may_approve(self) -> bool:
        return self.role in APPROVAL_ROLES

    def may_edit_treatment(self) -> bool:
        return self.role in EDIT_ROLES

    def may_work_operations_queues(self) -> bool:
        return self.role in OPERATIONS_ROLES


class PrincipalRegistry:
    """The configured principals, looked up by bearer token in constant time.

    Built from a JSON object mapping principal id to ``{"role": ..., "token_sha256": ...}``. JSON
    rather than a bespoke format because it is already how a deployment supplies structured
    configuration, and because a malformed registry must fail at startup rather than at the first
    approval — an authentication table that silently parses to empty is an outage that looks like a
    permissions problem.
    """

    def __init__(self, principals: dict[str, Principal], fingerprints: dict[str, str]) -> None:
        self._principals = principals
        #: fingerprint -> principal id
        self._fingerprints = fingerprints

    @classmethod
    def from_json(cls, raw: str) -> PrincipalRegistry:
        """Parse the registry, refusing every shape that would weaken the control.

        Each refusal is a real failure mode rather than defensive noise: an unknown role would grant
        whatever the code happened to default to; a duplicate fingerprint would make two principals
        indistinguishable, so an audit trail could not say which of them acted; and a short or blank
        fingerprint is a sign somebody put a *token* here by mistake, which is the one thing this
        format exists to prevent.
        """
        if not raw.strip():
            return cls({}, {})

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"principal registry is not valid JSON: {error.msg}") from error

        if not isinstance(parsed, dict):
            raise ValueError("principal registry must be a JSON object keyed by principal id")

        principals: dict[str, Principal] = {}
        fingerprints: dict[str, str] = {}

        for principal_id, entry in parsed.items():
            if not isinstance(entry, dict):
                raise ValueError(f"principal {principal_id!r} must map to an object")
            if not principal_id.strip():
                raise ValueError("a principal id may not be blank")

            try:
                role = Role(entry.get("role", ""))
            except ValueError as error:
                raise ValueError(
                    f"principal {principal_id!r} has role {entry.get('role')!r}; "
                    f"permitted roles are {sorted(role.value for role in Role)}"
                ) from error

            fingerprint = str(entry.get("token_sha256", "")).strip().lower()
            if len(fingerprint) != 64 or not all(c in "0123456789abcdef" for c in fingerprint):
                raise ValueError(
                    f"principal {principal_id!r} needs token_sha256 as 64 hex characters; "
                    "store the SHA-256 of the bearer token, never the token itself"
                )
            if fingerprint in fingerprints:
                raise ValueError(
                    f"principal {principal_id!r} shares a token with "
                    f"{fingerprints[fingerprint]!r}; an audit trail could not tell them apart"
                )

            principals[principal_id] = Principal(id=principal_id, role=role)
            fingerprints[fingerprint] = principal_id

        return cls(principals, fingerprints)

    def authenticate(self, token: str) -> Principal | None:
        """Resolve a bearer token to a principal, or ``None``.

        **Constant time across the whole registry.** Every candidate is compared with
        :func:`hmac.compare_digest` and the loop does not stop at the first match, so the time taken
        reveals neither which principal matched nor how many bytes of a wrong token were right.
        """
        presented = token_fingerprint(token)
        matched: str | None = None
        for fingerprint, principal_id in self._fingerprints.items():
            if hmac.compare_digest(fingerprint, presented):
                matched = principal_id
        return self._principals[matched] if matched is not None else None

    def __len__(self) -> int:
        return len(self._principals)

    def ids(self) -> frozenset[str]:
        return frozenset(self._principals)
