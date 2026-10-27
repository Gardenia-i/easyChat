"""Independent random waits for each desktop action and each message."""

import random
import time
import logging
from collections.abc import Callable


logger = logging.getLogger("easychat.delay")


class DelayPolicy:
    def __init__(
        self,
        action_range: tuple[float, float] = (2.0, 10.0),
        message_range: tuple[float, float] = (2.0, 6.0),
        *,
        sampler: Callable[[float, float], float] = random.uniform,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        for label, bounds in (("action_range", action_range), ("message_range", message_range)):
            if len(bounds) != 2 or bounds[0] < 0 or bounds[0] > bounds[1]:
                raise ValueError(f"{label} 必须是非负的 (最小秒数, 最大秒数)")
        self.action_range = action_range
        self.message_range = message_range
        self._sampler = sampler
        self._sleeper = sleeper

    def action(self) -> float:
        seconds = self._sampler(*self.action_range)
        logger.debug("action_wait seconds=%.2f", seconds)
        self._sleeper(seconds)
        return seconds

    def before_message(self) -> float:
        seconds = self._sampler(*self.message_range)
        logger.debug("message_wait seconds=%.2f", seconds)
        self._sleeper(seconds)
        return seconds
