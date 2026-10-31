"""D4 canary scheduler stub."""

from typing import Any

from fleetsre.clock import Clock
from fleetsre.contracts import Contract
from fleetsre.evaluator.engine import Evaluator


class CanaryScheduler:
    def __init__(
        self,
        world_or_planter: Any,
        evaluator: Evaluator,
        contracts: list[Contract],
        clock: Clock,
    ) -> None:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError
