"""
Rate Limiter - API 호출 제한 관리

슬라이딩 윈도우 방식 Rate Limiter + 적응형 Rate Limiter.
원래 상위 패키지의 자립형 구현 (역방향 의존성 제거).
"""

import time
import logging
from collections import deque
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["RateLimiter", "AdaptiveRateLimiter"]


class RateLimiter:
    """
    슬라이딩 윈도우 방식 Rate Limiter

    API 호출 전 대기하여 Rate Limit을 준수합니다.
    """

    def __init__(
        self,
        calls_per_second: float = 1.0,
        calls_per_minute: Optional[int] = None,
        burst_size: int = 1
    ):
        self.calls_per_second = calls_per_second
        self.calls_per_minute = calls_per_minute
        self.burst_size = burst_size
        self.second_timestamps: deque = deque()
        self.minute_timestamps: deque = deque() if calls_per_minute else None
        self.lock = Lock()
        self._min_interval = 1.0 / calls_per_second if calls_per_second > 0 else 0

    def wait(self) -> float:
        with self.lock:
            now = time.time()
            wait_time = 0.0

            while self.second_timestamps and now - self.second_timestamps[0] > 1.0:
                self.second_timestamps.popleft()

            if len(self.second_timestamps) >= self.calls_per_second:
                oldest = self.second_timestamps[0]
                wait_time = max(wait_time, 1.0 - (now - oldest))

            if self.minute_timestamps is not None and self.calls_per_minute:
                while self.minute_timestamps and now - self.minute_timestamps[0] > 60.0:
                    self.minute_timestamps.popleft()
                if len(self.minute_timestamps) >= self.calls_per_minute:
                    oldest = self.minute_timestamps[0]
                    wait_time = max(wait_time, 60.0 - (now - oldest))

            if wait_time > 0:
                logger.debug(f"Rate limit: waiting {wait_time:.2f}s")
                time.sleep(wait_time)
                now = time.time()

            self.second_timestamps.append(now)
            if self.minute_timestamps is not None:
                self.minute_timestamps.append(now)

            return wait_time

    def __enter__(self):
        self.wait()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def reset(self):
        with self.lock:
            self.second_timestamps.clear()
            if self.minute_timestamps is not None:
                self.minute_timestamps.clear()

    @property
    def current_usage(self) -> dict:
        with self.lock:
            now = time.time()
            while self.second_timestamps and now - self.second_timestamps[0] > 1.0:
                self.second_timestamps.popleft()
            usage = {
                "calls_last_second": len(self.second_timestamps),
                "limit_per_second": self.calls_per_second,
            }
            if self.minute_timestamps is not None:
                while self.minute_timestamps and now - self.minute_timestamps[0] > 60.0:
                    self.minute_timestamps.popleft()
                usage["calls_last_minute"] = len(self.minute_timestamps)
                usage["limit_per_minute"] = self.calls_per_minute
            return usage


class AdaptiveRateLimiter(RateLimiter):
    """
    적응형 Rate Limiter

    429 에러 발생 시 자동으로 속도를 낮춥니다.
    """

    def __init__(
        self,
        initial_calls_per_second: float = 1.0,
        min_calls_per_second: float = 0.1,
        backoff_factor: float = 0.5,
        recovery_factor: float = 1.1,
        recovery_threshold: int = 10
    ):
        super().__init__(calls_per_second=initial_calls_per_second)
        self.initial_calls_per_second = initial_calls_per_second
        self.min_calls_per_second = min_calls_per_second
        self.backoff_factor = backoff_factor
        self.recovery_factor = recovery_factor
        self.recovery_threshold = recovery_threshold
        self._consecutive_successes = 0

    def on_success(self):
        with self.lock:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.recovery_threshold:
                new_rate = min(
                    self.calls_per_second * self.recovery_factor,
                    self.initial_calls_per_second
                )
                if new_rate > self.calls_per_second:
                    logger.info(f"Rate limit recovery: {self.calls_per_second:.2f} -> {new_rate:.2f} req/s")
                    self.calls_per_second = new_rate
                    self._min_interval = 1.0 / self.calls_per_second
                self._consecutive_successes = 0

    def on_rate_limit_error(self):
        with self.lock:
            self._consecutive_successes = 0
            new_rate = max(
                self.calls_per_second * self.backoff_factor,
                self.min_calls_per_second
            )
            logger.warning(f"Rate limit hit: {self.calls_per_second:.2f} -> {new_rate:.2f} req/s")
            self.calls_per_second = new_rate
            self._min_interval = 1.0 / self.calls_per_second

    def reset_to_initial(self):
        with self.lock:
            self.calls_per_second = self.initial_calls_per_second
            self._min_interval = 1.0 / self.calls_per_second
            self._consecutive_successes = 0
