"""Role and clearance enforcement.

Two orthogonal checks:

* **Role** — *may this user perform this action?* (`require_role`)
* **Clearance** — *may this user see this data?* (`clearance_for`, applied as a SQL filter)

Clearance is never enforced by filtering results after the fact, and never by asking the model
to ignore over-classified text. The caller's ceiling is bound into the `WHERE` clause, so
data above their clearance is never read out of the database at all (docs/DECISIONS.md #15).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from app.config import ROLE_CLEARANCE
from app.enums import CLASSIFICATION_RANK, Classification, Role
from app.exceptions import PermissionDeniedError
from app.models import User


def clearance_for(role: Role) -> Classification:
    """The highest classification a role may read (architecture §8)."""
    return ROLE_CLEARANCE[role]


def user_clearance(user: User) -> Classification:
    return clearance_for(user.role)


def can_read(user: User, classification: Classification) -> bool:
    return CLASSIFICATION_RANK[classification] <= CLASSIFICATION_RANK[user_clearance(user)]


def assert_can_read(user: User, classification: Classification) -> None:
    """Guard for single-object reads. Collection reads filter in SQL instead."""
    if not can_read(user, classification):
        # Deliberately does not reveal that the object exists.
        raise PermissionDeniedError(
            "You do not have the clearance required to access this resource.",
            detail={
                "required": classification.value,
                "held": user_clearance(user).value,
                "role": user.role.value,
            },
        )


def has_role(user: User, *roles: Role) -> bool:
    return user.role in roles


# Roles that may perform each class of action, expressed once so the API layer and the tests
# agree by construction.
ANALYST_AND_ABOVE: tuple[Role, ...] = (Role.ADMIN, Role.EXECUTIVE, Role.ANALYST)
EXECUTIVE_AND_ABOVE: tuple[Role, ...] = (Role.ADMIN, Role.EXECUTIVE)
ADMIN_ONLY: tuple[Role, ...] = (Role.ADMIN,)
ANY_ROLE: tuple[Role, ...] = (Role.ADMIN, Role.EXECUTIVE, Role.ANALYST, Role.VIEWER)


def assert_role(user: User, *roles: Role) -> None:
    if not has_role(user, *roles):
        raise PermissionDeniedError(
            "Your role does not permit this action.",
            detail={"required_any_of": [r.value for r in roles], "held": user.role.value},
        )


# Type alias for a FastAPI dependency returning the current user.
UserDependency = Callable[..., Coroutine[Any, Any, User]]
