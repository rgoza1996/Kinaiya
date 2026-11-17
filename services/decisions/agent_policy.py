"""Optional Agent policy inspection; independent of Converse's classifier owner."""

import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from packages.contracts.decisions_consumers import DecisionsAgentPolicy, DecisionsAgentPolicyState
from packages.providers.disclosure import scope_allowed
from services.settings.provider_factory import capture_recipes, inspect_recipe


class AgentPolicyUnavailable(RuntimeError):
    pass


def configured_agent_policy(config):
    """Absence stays unconfigured; never inherit a Converse or Agent work model."""
    raw = config.get("decisions_agent_policy")
    try:
        return None if raw is None else DecisionsAgentPolicy.model_validate(raw)
    except ValidationError:
        raise ValueError("Invalid owner-configured Agent decision policy") from None


class AgentPolicyOwner:
    """Capture one host policy at launch without copying Core profiles or secrets.

    The existing Converse prompt/model owner and its SQLite receipts are outside
    this object's ownership. Catalog inspection cannot construct a provider,
    resolve a credential, activate a subsystem or admit an inference request.
    """

    def __init__(self, config, catalog):
        self._catalog = catalog
        self._source = "unconfigured"
        self._policy = None
        self._digest = None
        try:
            policy = configured_agent_policy(config)
        except ValueError:
            self._source = "invalid_host"
            return
        if policy is not None:
            # Revalidation/canonical serialization detach every mutable host
            # dictionary. A later host-file edit takes effect only on restart.
            self._policy = policy
            self._digest = policy.fingerprint()
            self._source = "host"

    def snapshot(self):
        status = "not_selected"
        if self._policy is not None:
            status = "unavailable"
            try:
                recipe = inspect_recipe(
                    self._catalog,
                    self._policy.catalog_binding,
                    self._policy.reasoning,
                    role="agent",
                )
                if scope_allowed(recipe.info, self._policy.content_scope):
                    status = "eligible"
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                pass
        return DecisionsAgentPolicyState(
            source=self._source,
            policy=self._policy,
            policy_sha256=self._digest,
            catalog_status=status,
        )

    def capture(self, expected_digest):
        """Capture secrets only for an explicitly enrolled original operation."""
        if self._policy is None or self._digest != expected_digest:
            raise AgentPolicyUnavailable("Independent Agent advice policy is unavailable")
        captured = None
        try:
            captured = capture_recipes(
                self._catalog,
                [(self._policy.catalog_binding, self._policy.reasoning)],
                role="agent",
            )[0]
            if not scope_allowed(captured.recipe.info, self._policy.content_scope):
                raise ValueError("Agent advice instructions cannot reach this provider")
            return self._policy, captured
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            if captured is not None:
                captured.release()
            raise AgentPolicyUnavailable("Independent Agent advice policy is unavailable") from None


def router(owner, auth, *, operations=None):
    routes = APIRouter()

    @routes.get("/v1/decisions/options/agent-policy", dependencies=[Depends(auth.require("owner"))])
    async def state():
        return JSONResponse(owner.snapshot().model_dump(), headers={"Cache-Control": "no-store"})

    if operations is not None:

        @routes.get(
            "/v1/decisions/options/agent-advice-runtime",
            dependencies=[Depends(auth.require("owner"))],
        )
        async def advice_runtime():
            return JSONResponse(
                operations.runtime_snapshot().model_dump(), headers={"Cache-Control": "no-store"}
            )

    return routes
