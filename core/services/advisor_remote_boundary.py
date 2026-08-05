"""Where a tool call crosses — or does not cross — the institutional boundary.

The agent loop runs the same six steps for every question. What changes between
a local model and an external provider is not the SHAPE of those steps but what
each one does, so this module supplies the steps and the loop keeps one body:

    exposure check
    forbidden-argument rejection
    issued-reference resolution
    scope authorisation
    local execution
    explicit projection

THE ORDER IS THE SECURITY PROPERTY. Every refusal above `registry.execute`
happens before any database read; the one below it happens before any egress.
That is why the loop names each step rather than calling one wrapper: a reviewer
must be able to see, without following an indirection, that authorisation
precedes execution and projection precedes transmission. A generic
`boundary.run(call)` would hide precisely the thing being reviewed.

TWO RESULTS, NEVER ONE

`local_result` is the complete executor output. It feeds the adviser's evidence
panel, the citation contract, the stored turn, the audit record — all internal
readers that are authorised to see names and ids and were built expecting them.

`provider_result` is what the projector allows out. It is the ONLY thing that
reaches an external provider.

Collapsing these into one value fails in whichever direction you collapse it. Use
the projection internally and the evidence panel loses the identities an adviser
is entitled to see; use the local result remotely and the boundary was decorative.
So they are produced together, cached together as `CachedToolExecution`, and
carried separately from there on.

REVERSIBILITY

`LocalToolBoundary` is the identity function at every step. With
`LLM_BACKEND=local` the loop behaves exactly as it did before this module
existed — same arguments, same result object, same message content. Returning to
local is a configuration change, not a code path that has to be maintained.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# Imported as a MODULE, not as names. Several methods below are deliberately
# named after the policy function they delegate to, and unqualified calls would
# then read as recursion to anyone skimming — and would become recursion the
# moment somebody adds a `self.`.
from core.services import llm_remote_privacy as privacy
from core.services.llm_backend import BACKEND_LOCAL, LLMPrivacyError
from core.services.llm_remote_privacy import RemoteIdentityMap

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedToolExecution:
    """One executed call, held as the pair it is.

    A typed pair rather than two dictionaries that happen to be updated together.
    The failure this prevents is not exotic: a retry path reaches for the cache,
    finds a dict, serialises it, and the unprojected result goes out — a leak
    introduced by code that never mentions the boundary at all.

    Both are stored because a projection cannot be redone later. It reads the
    CURRENT identity map, and by the time a retry path wants one the map may have
    issued different references or belong to a different answer entirely.
    Re-projecting would mint a reference the provider has never seen, or worse,
    reuse one that means somebody else.
    """

    local_result: dict[str, Any]
    provider_result: dict[str, Any]


#: Appended to a reused result so the model can tell a cache hit from a fresh
#: read. Applied to BOTH halves: the local evidence panel and the provider
#: transcript should agree about what was re-served.
DUPLICATE_NOTE = "duplicate call; reusing prior result"


class ToolBoundary(Protocol):
    """The six steps, plus the message-level work either side of the loop."""

    is_remote: bool

    def tool_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def assert_capability_allowed(self, tool_name: str) -> None: ...

    def reject_identity_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...

    def resolve_reference_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...

    def authorise_resolved_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None: ...

    def project_tool_result(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]: ...

    def project_context(self, context: dict[str, Any]) -> dict[str, Any]: ...

    def sanitise_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

    def refusal_result(self, tool_name: str) -> dict[str, Any]: ...


class LocalToolBoundary:
    """No boundary, because there is none to cross.

    A locally hosted model is inside the institution: it sees exactly what the
    application sees, under the same controls, and projecting a result before
    showing it to a process running on the same machine would remove evidence for
    no benefit. Every step is therefore the identity function, and this class
    exists so the loop does not need an `if remote:` in the middle of it.
    """

    is_remote = False

    def tool_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return schemas

    def assert_capability_allowed(self, tool_name: str) -> None:
        return None

    def reject_identity_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return arguments

    def resolve_reference_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return arguments

    def authorise_resolved_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        # The executor authorises every argument it receives, exactly as it did
        # before this module existed. Adding a second check here would look like
        # extra safety and would in fact be a second opinion about scope.
        return None

    def project_tool_result(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        return result

    def project_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return context

    def sanitise_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages

    def refusal_result(self, tool_name: str) -> dict[str, Any]:  # pragma: no cover - unreachable
        return dict(privacy.REFUSED_RESULT)


class RemoteToolBoundary:
    """An external provider is a data processor, and is treated as one.

    Holds the request-scoped identity map, so every reference issued during one
    answer is minted here and dies here. Holds the authoriser, so "may this
    principal see this student" is answered once per candidate rather than
    per call site.
    """

    is_remote = True

    def __init__(
        self,
        *,
        scope: dict[str, Any] | None,
        identities: RemoteIdentityMap | None = None,
        known_names: tuple[str, ...] = (),
        authorise_id: Callable[[int], bool] | None = None,
    ) -> None:
        self.scope = scope or {}
        # `is not None`, never `or`. A map that has issued nothing yet is the
        # normal state at construction, and `or` would silently swap the caller's
        # map for a fresh one — every reference the caller had already minted
        # would then fail to resolve, with no error to say why.
        self.identities = identities if identities is not None else RemoteIdentityMap()
        self.known_names = tuple(n for n in known_names if str(n or "").strip())
        self.authorise_id = authorise_id or privacy.authoriser_for_scope(self.scope)
        from core.services.rbac import ROLE_STUDENT

        # A student's session already names them; a `student_ref` parameter would
        # be a reference to the only person in the conversation. Staff genuinely
        # need to say which student they mean, so they get the substitution.
        self.allow_student_ref = str(self.scope.get("role") or "") != ROLE_STUDENT

    def tool_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return privacy.remote_tool_schemas(schemas, allow_student_ref=self.allow_student_ref)

    def assert_capability_allowed(self, tool_name: str) -> None:
        privacy.assert_remote_capability_allowed(tool_name, privacy.remote_exposure_for(tool_name))

    def reject_identity_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        arguments = privacy.reject_identity_arguments(tool_name, arguments)
        if (
            not self.allow_student_ref
            and isinstance(arguments, dict)
            and "student_ref" in arguments
        ):
            # Student mode never advertised the parameter. A model sending one
            # anyway is either confused or probing, and both deserve the same
            # answer as a forged `student_id`.
            raise LLMPrivacyError(f"{tool_name}: student_ref is not available in this session.")
        return arguments

    def resolve_reference_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return privacy.resolve_reference_arguments(tool_name, arguments, self.identities)

    def authorise_resolved_arguments(self, tool_name: str, arguments: dict[str, Any]) -> None:
        privacy.authorise_resolved_arguments(tool_name, arguments, self.scope)

    def project_tool_result(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        return privacy.project_tool_result_for_remote(tool_name, result, self.identities)

    def project_context(self, context: dict[str, Any]) -> dict[str, Any]:
        return privacy.project_verified_context_for_remote(context, self.identities)

    def sanitise_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return privacy.sanitise_messages_for_remote(
            messages,
            self.identities,
            known_names=self.known_names,
            authorise_id=self.authorise_id,
        )

    def refusal_result(self, tool_name: str) -> dict[str, Any]:
        """What the model is told when a step refuses.

        DENY may name itself — the model can already see which capabilities its
        schema list contains. Every other refusal is one indistinguishable
        message, so a transcript cannot be read backwards into "that student
        exists but you may not see them".
        """
        if privacy.remote_exposure_for(tool_name) is privacy.RemoteExposure.DENY:
            return dict(privacy.DENIED_RESULT)
        return dict(privacy.REFUSED_RESULT)


def boundary_for_scope(
    scope: dict[str, Any] | None,
    *,
    backend: str,
    known_names: tuple[str, ...] = (),
) -> LocalToolBoundary | RemoteToolBoundary:
    """One place decides, from the configured backend and nothing else.

    Not from a request flag, a feature toggle, or a per-user setting: an
    institution either does or does not send student data to a given processor,
    and that is a deployment decision. `LLM_BACKEND=local` restores the previous
    behaviour with no code change, which is the reversibility this feature was
    asked for.
    """
    if str(backend or "").strip().lower() == BACKEND_LOCAL:
        return LocalToolBoundary()
    return RemoteToolBoundary(scope=scope, known_names=known_names)


__all__ = [
    "DUPLICATE_NOTE",
    "CachedToolExecution",
    "LocalToolBoundary",
    "RemoteToolBoundary",
    "ToolBoundary",
    "boundary_for_scope",
]
