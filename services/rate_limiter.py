"""
Simple in-memory rate limiter for API endpoints.
Tracks requests per IP address with sliding window.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple in-memory rate limiter using sliding window."""

    def __init__(self):
        self._requests: Dict[str, List[datetime]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, ip: str, max_requests: int, window_minutes: int = 1) -> tuple[bool, int]:
        """
        Check if request from IP is allowed.

        Args:
            ip: IP address
            max_requests: Maximum requests allowed in window
            window_minutes: Time window in minutes

        Returns:
            Tuple of (is_allowed, requests_in_window)
        """
        now = datetime.now()
        cutoff = now - timedelta(minutes=window_minutes)

        with self._lock:
            # Clean up old requests
            self._requests[ip] = [
                req_time for req_time in self._requests[ip]
                if req_time > cutoff
            ]

            # Check if under limit
            current_count = len(self._requests[ip])
            is_allowed = current_count < max_requests

            if is_allowed:
                # Record this request
                self._requests[ip].append(now)
                current_count += 1

            return is_allowed, current_count

    def cleanup_old_entries(self, hours: int = 1):
        """Remove entries older than specified hours to prevent memory growth."""
        cutoff = datetime.now() - timedelta(hours=hours)

        with self._lock:
            # Remove IPs with no recent requests
            ips_to_remove = [
                ip for ip, times in self._requests.items()
                if not times or max(times) < cutoff
            ]
            for ip in ips_to_remove:
                del self._requests[ip]

            logger.debug(f"Rate limiter cleanup: removed {len(ips_to_remove)} stale IP entries")


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter
