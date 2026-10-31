"""D4 canary scheduler: plants synthetic canaries on a SimClock grid."""

from datetime import datetime, timedelta
from typing import Any

from fleetsre.clock import Clock
from fleetsre.contracts import Contract
from fleetsre.evaluator.engine import Evaluator


def _resolve(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return params[value[2:-1]]
    return value


class CanaryScheduler:
    def __init__(
        self,
        world_or_planter: Any,
        evaluator: Evaluator,
        contracts: list[Contract],
        clock: Clock,
    ) -> None:
        self._evaluator = evaluator
        self._clock = clock
        planter = world_or_planter
        if hasattr(planter, "plant_canary"):
            self._plant = planter.plant_canary
        else:
            self._plant = planter
        by_service = {c.service: c for c in contracts}
        armed_at = clock.now()
        self._due: dict[str, datetime] = {}
        self._every: dict[str, timedelta] = {}
        self._env: dict[str, str] = {}
        for deployment in evaluator.deployments:
            contract = by_service.get(deployment.service)
            if contract is None:
                continue
            every_minutes: int | None = None
            for tripwire in contract.tripwires:
                if tripwire.type == "synthetic":
                    every_minutes = int(
                        _resolve(
                            tripwire.spec["everyMinutes"],
                            deployment.parameters,
                        )
                    )
                    break
            if every_minutes is None:
                continue
            step = timedelta(minutes=every_minutes)
            self._due[deployment.id] = armed_at + step
            self._every[deployment.id] = step
            self._env[deployment.id] = deployment.environment

    def tick(self) -> None:
        now = self._clock.now()
        for dep_id in sorted(self._due):
            step = self._every[dep_id]
            while self._due[dep_id] <= now:
                provider_id = self._plant(self._env[dep_id])
                self._evaluator.canary_planted(dep_id, provider_id, now)
                self._due[dep_id] = self._due[dep_id] + step
