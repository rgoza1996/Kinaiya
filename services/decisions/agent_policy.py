"""Optional Agent policy inspection; independent of Converse's classifier owner."""

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from packages.contracts.decisions_consumers import (
    MAX_POLICY_CHANGE_BYTES,
    MAX_POLICY_CHANGES,
    MAX_POLICY_SETTINGS_BYTES,
    DecisionsAgentPolicy,
    DecisionsAgentPolicyChange,
    DecisionsAgentPolicyObservation,
    DecisionsAgentPolicySettingsSnapshot,
    DecisionsAgentPolicyState,
)
from packages.providers.disclosure import scope_allowed
from services.decisions.agent_policy_settings import (
    HOST_POLICY,
    AgentPolicySettingsConflict,
    AgentPolicySettingsUnavailable,
)
from services.settings.provider_factory import capture_recipes, inspect_recipe


class AgentPolicyUnavailable(RuntimeError):
    pass


def configured_agent_policy(config, *, selection=HOST_POLICY):
    """Absence stays unconfigured; never inherit a Converse or Agent work model."""
    if selection is None:
        return None
    if selection.source != "host":
        return selection.policy
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

    def __init__(self, config, catalog, *, settings=None, launch=HOST_POLICY):
        self._catalog = catalog
        self._settings = settings
        self._host_source = "unconfigured"
        self._host_policy = None
        try:
            policy = configured_agent_policy(config)
        except ValueError:
            self._host_source = "invalid_host"
        else:
            # Revalidation/canonical serialization detach every mutable host
            # dictionary. A later host-file edit takes effect only on restart.
            if policy is not None:
                self._host_policy = policy
                self._host_source = "host"
        self._launch = settings.startup_selection if settings is not None else launch
        self._source, self._policy = self._resolve(self._launch)
        self._digest = self._policy.fingerprint() if self._policy is not None else None

    def _resolve(self, selection):
        if selection is None:
            return "unavailable", None
        if selection.source == "host":
            return self._host_source, self._host_policy
        return selection.source, selection.policy

    def _inspect_catalog(self, policy):
        if policy is None:
            return "not_selected"
        try:
            recipe = inspect_recipe(
                self._catalog, policy.catalog_binding, policy.reasoning, role="agent"
            )
            if scope_allowed(recipe.info, policy.content_scope):
                return "eligible"
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            pass
        return "unavailable"

    @property
    def configured(self):
        return self._policy is not None

    def snapshot(self):
        """Preserve the legacy host-declaration projection."""
        return DecisionsAgentPolicyState(
            source=self._host_source,
            policy=self._host_policy,
            policy_sha256=self._host_policy.fingerprint()
            if self._host_policy is not None
            else None,
            catalog_status=self._inspect_catalog(self._host_policy),
        )

    def _observation(self, source, policy, *, host=None):
        return DecisionsAgentPolicyObservation(
            source=source,
            policy=policy,
            policy_sha256=policy.fingerprint() if policy is not None else None,
            catalog_status=(
                host.catalog_status
                if host is not None and policy == host.policy
                else self._inspect_catalog(policy)
            ),
        )

    def active(self):
        return self._observation(self._source, self._policy)

    def _store(self):
        if self._settings is None:
            raise AgentPolicySettingsUnavailable
        return self._settings

    def settings_snapshot(self):
        store = self._store()
        revision, desired = store.inspect()
        host = self.snapshot()
        active = self._observation(self._source, self._policy, host=host)
        next_launch = (
            active
            if desired == self._launch
            else self._observation(*self._resolve(desired), host=host)
        )
        return DecisionsAgentPolicySettingsSnapshot(
            backend_id=store.backend,
            instance_id=store.instance,
            revision=revision,
            selection_source=desired.source,
            host=host,
            active=active,
            next_launch=next_launch,
            writable=revision < MAX_POLICY_CHANGES,
        )

    def change(self, original):
        def check_policy(policy):
            if self._inspect_catalog(policy) != "eligible":
                raise ValueError("Select an eligible, explicitly scoped Core catalog generation")

        return self._store().change(original, check_policy=check_policy)

    def lookup(self, original):
        return self._store().lookup(original)

    def close(self):
        if self._settings is not None:
            self._settings.close()

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
    from services.decisions.agent_advice_api import (
        advice_envelope,
        advice_response,
        read_advice_body,
        refused,
    )

    native = auth.require("owner")
    routes = APIRouter(prefix="/v1/decisions/options", dependencies=[Depends(native)])

    @routes.get("/agent-policy")
    async def state():
        return JSONResponse(owner.snapshot().model_dump(), headers={"Cache-Control": "no-store"})

    def settings_response(method, *args):
        try:
            return advice_response(method(*args), maximum=MAX_POLICY_SETTINGS_BYTES)
        except AgentPolicySettingsConflict:
            raise refused(
                409, "The policy authority changed. Look up the exact original save."
            ) from None
        except AgentPolicySettingsUnavailable:
            raise refused(503, "Agent advice policy settings are unavailable.") from None
        except ValueError:
            raise refused(
                400, "Choose an eligible Core model with a compatible disclosure scope."
            ) from None

    @routes.get("/agent-advice-settings")
    async def inspect(request: Request):
        advice_envelope(request)
        return settings_response(owner.settings_snapshot)

    @routes.post("/agent-advice-settings")
    async def change(request: Request):
        original = await read_advice_body(
            request, native, DecisionsAgentPolicyChange, MAX_POLICY_CHANGE_BYTES
        )
        return settings_response(owner.change, original)

    @routes.post("/agent-advice-settings/lookup")
    async def lookup(request: Request):
        original = await read_advice_body(
            request, native, DecisionsAgentPolicyChange, MAX_POLICY_CHANGE_BYTES
        )
        return settings_response(owner.lookup, original)

    if operations is not None:

        @routes.get("/agent-advice-runtime")
        async def advice_runtime():
            return JSONResponse(
                operations.runtime_snapshot().model_dump(), headers={"Cache-Control": "no-store"}
            )

    return routes
