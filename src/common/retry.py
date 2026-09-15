"""异常重试：重试装饰器，支持自定义重试次数、间隔（指数退避）、异常类型。

用法::

    @retry(max_retries=3, delay=2, backoff=2.0, exceptions=(requests.RequestException,))
    def fetch():
        ...
"""
from __future__ import annotations

import functools
import random
import time
from typing import Callable, Optional, Tuple, Type, Union

from src.common.config import ConfigManager
from src.common.logger import get_logger

logger = get_logger("common.retry")

ExcTypes = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


def retry(
    max_retries: Optional[int] = None,
    delay: Optional[float] = None,
    backoff: float = 2.0,
    jitter: float = 0.5,
    exceptions: ExcTypes = (Exception,),
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> Callable:
    """指数退避重试装饰器。

    Args:
        max_retries: 最大重试次数（不含首次执行）；None 时读取配置默认值
        delay: 首次重试前等待秒数；None 时读取配置默认值
        backoff: 每次重试间隔乘数
        jitter: 随机抖动比例（0~1），避免请求同步
        exceptions: 捕获的异常类型
        on_retry: 每次重试前的回调（retry_count, exc）
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cfg = ConfigManager()
            retries = max_retries if max_retries is not None else int(
                cfg.get("retry.default_max_retries", 3))
            base_delay = delay if delay is not None else float(
                cfg.get("retry.default_delay", 2))

            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    attempt += 1
                    if attempt > retries:
                        raise
                    wait = base_delay * (backoff ** (attempt - 1))
                    if jitter > 0:
                        wait *= 1 + random.uniform(-jitter, jitter)
                    logger.warning(
                        "%s 第 %d/%d 次执行失败(%s)，%.2fs 后重试",
                        func.__name__, attempt, retries, exc, wait)
                    if on_retry:
                        on_retry(attempt, exc)
                    time.sleep(wait)
        return wrapper
    return decorator
