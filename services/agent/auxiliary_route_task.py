"""One task's frozen route authority and bounded lazy adapter ownership."""

from packages.contracts.models import AgentModelPin
from services.settings.provider_factory import open_captured_recipe


class CapturedAuxiliaryRoute:
    def __init__(
        self,
        *,
        purpose,
        harness_version,
        settings_revision,
        route_revision,
        candidates,
        captures,
        lease,
        resource_state,
    ):
        if not 1 <= len(captures) <= 3 or len(candidates) != len(captures):
            raise ValueError("Auxiliary capture exceeds its native bound")
        self.purpose = purpose
        self.harness_version = harness_version
        self.settings_revision = settings_revision
        self.route_revision = route_revision
        self.candidates = tuple(candidates)
        self.captures = tuple(captures)
        self.lease = lease
        self.resource_state = resource_state
        self.index = 0
        self.provider = None
        self.blocked = False
        self.running = False
        self.route_confirmed = False
        self.route_event_id = None
        self.pending_append = None
        self.calls = 0

    @property
    def info(self):
        # Accepted auxiliary_models remains the initial selection. Actual
        # attempt/winner pins are separately journaled and returned as evidence.
        return self.captures[0].recipe.info

    @property
    def reasoning_effort(self):
        return self.captures[0].recipe.reasoning

    def pin(self, ordinal=0):
        recipe = self.captures[ordinal].recipe
        info = recipe.info
        return AgentModelPin(
            profile_id=recipe.binding.id,
            provider=info.provider,
            model=info.model,
            local=info.local,
            reasoning=recipe.reasoning,
            settings_revision=self.settings_revision,
            catalog_binding=recipe.binding,
            catalog_revision=recipe.catalog_revision,
        )

    def provenance(self):
        return {
            "purpose": self.purpose,
            "harness_version": self.harness_version,
            "settings_revision": self.settings_revision,
            "route_revision": self.route_revision,
            "candidates": [
                {
                    "ordinal": i + 1,
                    "model_pin": self.pin(i).model_dump(mode="json"),
                    "cloud_scope": candidate.cloud_scope,
                    "credential": capture.recipe.config.get("credential"),
                }
                for i, (candidate, capture) in enumerate(
                    zip(self.candidates, self.captures, strict=True)
                )
            ],
        }

    def open(self):
        if self.blocked or self.index >= len(self.captures) or self.lease.cleanup is not None:
            raise ValueError("Auxiliary route is unavailable or requires cleanup")
        if self.provider is None:
            if self.lease.failed or len(self.lease.adapters) >= 6:
                raise ValueError("Auxiliary model ownership requires cleanup or capacity")
            provider = open_captured_recipe(
                self.captures[self.index],
                auxiliary=True,
                resource_state=self.resource_state,
                pre_generation_proofs=True,
            )
            self.lease.own(provider)
            self.provider = provider
        return self.provider, self.pin(self.index)

    async def advance(self):
        if self.provider is not None:
            await self.lease.retire(self.provider)
            self.provider = None
        self.index += 1

    def release(self):
        for capture in self.captures:
            capture.release()
        self.blocked = True
        self.provider = None
