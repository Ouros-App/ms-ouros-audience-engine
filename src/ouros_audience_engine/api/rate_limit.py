import hashlib
import logging

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_LIMIT = 60
_WINDOW_SECONDS = 60
_INCREMENT_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


def enforce_internal_rate_limit(request: Request) -> None:
    client = request.client
    client_host = client.host if client is not None else "unknown"
    client_hash = hashlib.sha256(client_host.encode("utf-8")).hexdigest()
    key = f"ouros:rate-limit:internal:{client_hash}"

    try:
        count, retry_after = request.app.state.runtime.redis_client.eval(
            _INCREMENT_WINDOW, 1, key, _WINDOW_SECONDS
        )
        count = int(count)
        retry_after = max(1, int(retry_after))
    except (RedisError, IndexError, TypeError, ValueError) as exc:
        logger.exception("internal API rate-limit check failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="request rate limiter is unavailable",
        ) from exc

    if count > _LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="request rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
