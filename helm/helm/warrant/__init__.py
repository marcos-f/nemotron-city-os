"""warrant — dataset-scoped ownership, delegation and break-glass.

feature-area://9. The identity provider authenticates; warrant authorizes.
Every authorization change is an effect on throughline's hash chain, and the
permission graph is a projection of that chain rather than a table anyone
edits.
"""

from helm.warrant.model import (
    GLOBAL_STEWARD_ROLE,
    RANK,
    ROLES,
    RULES,
    Authority,
    Refused,
    Unknown,
)
from helm.warrant.projection import Snapshot, build
from helm.warrant.service import Actor, Warrant

__all__ = [
    "Actor",
    "Authority",
    "GLOBAL_STEWARD_ROLE",
    "RANK",
    "ROLES",
    "RULES",
    "Refused",
    "Snapshot",
    "Unknown",
    "Warrant",
    "build",
]
