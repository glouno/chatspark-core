"""Host-owned admission state shared by independently configured answer runtimes."""

import math
import time
from collections import deque
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Condition, Event


@dataclass(frozen=True, eq=False)
class AnswerCapacityV1:
    """One stream/preparation envelope, with FIFO bounded preparation waiting.

    Hosts explicitly pass the same instance to each runtime. No corpus, prompt,
    model, principal or request content is retained. Runtime shutdown cancels its
    own streams; it never closes capacity owned by another runtime or the host.
    """

    max_streams: int = 4
    preparation_slots: int = 2
    preparation_wait: float = 2.0
    _streams: BoundedSemaphore = field(init=False, repr=False)
    _preparations: BoundedSemaphore = field(init=False, repr=False)
    _condition: Condition = field(default_factory=Condition, init=False, repr=False)
    _waiting: deque = field(default_factory=deque, init=False, repr=False)

    def __post_init__(self):
        if (
            type(self.max_streams) is not int
            or self.max_streams < 1
            or type(self.preparation_slots) is not int
            or self.preparation_slots < 1
            or isinstance(self.preparation_wait, bool)
            or not isinstance(self.preparation_wait, (int, float))
            or not math.isfinite(self.preparation_wait)
            or not 0 <= self.preparation_wait <= 120
        ):
            raise ValueError("Invalid capacity configuration")
        object.__setattr__(self, "_streams", BoundedSemaphore(self.max_streams))
        object.__setattr__(self, "_preparations", BoundedSemaphore(self.preparation_slots))

    def acquire_stream(self) -> bool:
        return self._streams.acquire(blocking=False)

    def release_stream(self) -> None:
        self._streams.release()

    def acquire_preparation(self, cancelled: Event) -> bool:
        deadline = time.monotonic() + self.preparation_wait
        token = object()
        with self._condition:
            self._waiting.append(token)
            try:
                while not cancelled.is_set():
                    if self._waiting[0] is token and self._preparations.acquire(blocking=False):
                        self._waiting.popleft()
                        self._condition.notify_all()
                        return True
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(min(0.05, remaining))
                return False
            finally:
                if token in self._waiting:
                    self._waiting.remove(token)
                    self._condition.notify_all()

    def release_preparation(self) -> None:
        with self._condition:
            self._preparations.release()
            self._condition.notify_all()
