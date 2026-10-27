"""Shared implementation; source-specific entry points remain compatible."""
import time
import logging
logger = logging.getLogger(__name__)

def enforce_rate_limit(self) -> None:
    """
    强制执行速率限制

    策略：
    1. 检查距离上次请求的时间间隔
    2. 如果间隔不足，补充休眠时间
    3. 然后再执行随机 jitter 休眠
    """
    if self._last_request_time is not None:
        elapsed = time.time() - self._last_request_time
        min_interval = self.sleep_min
        if elapsed < min_interval:
            additional_sleep = min_interval - elapsed
            logger.debug(f"补充休眠 {additional_sleep:.2f} 秒")
            time.sleep(additional_sleep)

    # 执行随机 jitter 休眠
    self.random_sleep(self.sleep_min, self.sleep_max)
    self._last_request_time = time.time()
