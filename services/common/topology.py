"""Configured subsystem selection; Core is not a request to run every role."""

from dataclasses import dataclass

SUBSYSTEM_MODES = ("core", "agent", "decisions", "agent-decisions", "converse-decisions", "all")


@dataclass(frozen=True)
class SubsystemTopology:
    agent: bool
    converse: bool
    decisions: bool

    def __post_init__(self):
        if any(type(value) is not bool for value in (self.agent, self.converse, self.decisions)):
            raise ValueError("Subsystem selections must be booleans")
        if self.converse and not self.decisions:
            raise ValueError("Converse requires Decisions; enable Decisions or disable Converse")

    @classmethod
    def from_mode(cls, mode):
        modes = {
            "core": (False, False, False),
            "agent": (True, False, False),
            "decisions": (False, False, True),
            "agent-decisions": (True, False, True),
            "converse-decisions": (False, True, True),
            "all": (True, True, True),
        }
        if type(mode) is not str or mode not in modes:
            raise ValueError("Unsupported subsystem mode")
        return cls(*modes[mode])

    @classmethod
    def from_config(cls, config, *, developer_all=False):
        # Legacy developer `serve all` has no managed selection. Once a
        # distribution selection exists, both entry points must respect it.
        if "distribution" not in config:
            return cls(agent=True, converse=developer_all, decisions=developer_all)
        distribution = config["distribution"]
        if not isinstance(distribution, dict):
            raise ValueError("Subsystem distribution selection must be an object")
        converse = distribution.get("converse", False)
        return cls(
            agent=distribution.get("agent", True),
            converse=converse,
            decisions=distribution.get("decisions", converse),
        )

    def services(self, *, resources=False):
        # Spawn order alone is not a readiness check. Required-dependency
        # admission and runtime observation are separate lifecycle concerns.
        names = ["session"]
        if resources:
            names.append("resources")
        if self.agent:
            names.append("agent")
        if self.decisions:
            names.append("decisions")
        if self.converse:
            names.append("converse")
        return names

    @property
    def mode(self):
        return next(mode for mode in SUBSYSTEM_MODES if self == self.from_mode(mode))
