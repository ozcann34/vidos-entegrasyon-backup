import time
import threading
from functools import wraps
import logging

logger = logging.getLogger(__name__)

class RateLimiter:
    """
    Thread-safe rate limiter.
    Ensures that no more than `max_calls` are made within `period` seconds.
    """
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.tokens = max_calls
        self.last_refill = time.time()
        self.lock = threading.Lock()

    def wait(self):
        """Blocks until a token is available."""
        with self.lock:
            while True:
                now = time.time()
                elapsed = now - self.last_refill
                
                # Refill tokens based on time passed
                if elapsed > self.period:
                    self.tokens = self.max_calls
                    self.last_refill = now
                elif self.tokens < self.max_calls:
                     # Calculate if enough time has passed for at least 1 token refill
                     # (Simple version: just reset if period passed, or wait)
                     pass

                if self.tokens > 0:
                    self.tokens -= 1
                    return
                
                # Sleep until basic period reset
                sleep_time = self.period - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # Continue loop to re-check

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            self.wait()
            return func(*args, **kwargs)
        return wrapper

# Global instances for marketplaces
# Adjust limits as per API docs
# Trendyol: Generally high limits but safer to be polite. 
# N11: Known for strict limits.
# Pazarama: Unknown, use safe defaults.

trendyol_limiter = RateLimiter(max_calls=30, period=10) # 3 calls/sec roughly
n11_limiter = RateLimiter(max_calls=60, period=60)      # 1 call/sec
pazarama_limiter = RateLimiter(max_calls=20, period=10) # 2 calls/sec
hepsiburada_limiter = RateLimiter(max_calls=20, period=10) 
idefix_limiter = RateLimiter(max_calls=20, period=10)
