# -*- coding: utf-8 -*-
"""
Redis data cache layer for Market Signal Lab [V18]
"""
import redis
import pickle
import logging
import hashlib
import json
from core.config import Config

logger = logging.getLogger("StockAnalyzer.DataCache")

class RedisCache:
    """Redis-based caching layer for DataFrame and dict objects."""

    # TTL Definitions (in seconds)
    TTL_REALTIME = 10       # Realtime quotes: 10 seconds
    TTL_AUCTION = 20        # Auction snapshot: seconds-level
    TTL_MINUTE = 60         # Minute bars: keep short to avoid huge cache
    TTL_LIMIT = 300         # Limit list / limit stats: short TTL
    TTL_LHB = 7200          # Dragon Tiger list: 2 hours
    TTL_LHB_RECENT = 7200   # Dragon Tiger per-stock recent: 2 hours
    TTL_CAL = 21600         # Trade calendar: 6 hours
    TTL_MONEYFLOW = 1800    # Intraday money flow: 30 minutes
    TTL_DAILY = 300         # Daily snapshots: 5 minutes (intra-day)
    TTL_HISTORY = 3600      # Historical K-lines: 1 hour
    TTL_INDEX = 3600        # Index data: 1 hour
    TTL_BASIC = 86400       # Stock basics: 24 hours

    def __init__(self):
        self.enabled = False
        self.prefix = "sa:" # Namespace prefix
        try:
            self.client = redis.Redis(
                host=Config.REDIS_HOST,
                port=Config.REDIS_PORT,
                password=Config.REDIS_PASS,
                db=Config.REDIS_DB,
                decode_responses=False, # We need binary for pickle
                socket_timeout=Config.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=Config.REDIS_SOCKET_CONNECT_TIMEOUT,
                health_check_interval=Config.REDIS_HEALTH_CHECK_INTERVAL,
                retry_on_timeout=True,
                max_connections=Config.REDIS_MAX_CONNECTIONS,
            )
            # Fast ping to verify connection
            self.client.ping()
            self.enabled = True
            logger.info(f"Redis cache initialized at {Config.REDIS_HOST}:{Config.REDIS_PORT}")
        except Exception as e:
            logger.warning(f"Redis cache connection failed: {e}. Data Provider will run without cache.")

    def _make_key(self, cache_type, identifier):
        """Generate a consistent cache key."""
        if isinstance(identifier, list):
            # For lists of codes, sort and hash to avoid long keys
            id_str = "_".join(sorted(identifier))
            if len(id_str) > 50:
                id_str = hashlib.md5(id_str.encode()).hexdigest()
        elif isinstance(identifier, dict):
            id_str = hashlib.md5(json.dumps(identifier, sort_keys=True).encode()).hexdigest()
        else:
            id_str = str(identifier)

        return f"{self.prefix}{cache_type}:{id_str}"

    def get(self, cache_type, identifier):
        """Retrieve and deserialize data from Redis."""
        if not self.enabled:
            return None

        key = self._make_key(cache_type, identifier)
        try:
            data = self.client.get(key)
            if data:
                return pickle.loads(data)
            return None
        except Exception as e:
            logger.error(f"Redis GET error for {key}: {e}")
            return None

    def set(self, cache_type, identifier, value, ttl=300):
        """Serialize and store data in Redis with TTL."""
        if not self.enabled or value is None:
            return False

        key = self._make_key(cache_type, identifier)
        pickled_data = None
        try:
            pickled_data = pickle.dumps(value)
        except Exception as e:
            logger.error(f"Redis pickle error for {key}: {e}")
            return False

        max_value_bytes = getattr(Config, "REDIS_MAX_VALUE_BYTES", 0)
        if max_value_bytes and len(pickled_data) > max_value_bytes:
            size_mb = len(pickled_data) / 1024 / 1024
            limit_mb = max_value_bytes / 1024 / 1024
            logger.info(
                f"Redis SET skipped for {key}: payload {size_mb:.2f}MB exceeds "
                f"REDIS_MAX_VALUE_BYTES {limit_mb:.2f}MB"
            )
            return False

        for attempt in range(2):
            try:
                self.client.setex(key, ttl, pickled_data)
                return True
            except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as e:
                if attempt == 0:
                    logger.warning(f"Redis SET retry for {key}: {e}")
                    continue
                logger.error(f"Redis SET error for {key}: {e}")
                return False
            except Exception as e:
                logger.error(f"Redis SET error for {key}: {e}")
                return False

    def clear_namespace(self):
        """Clear all keys under the app namespace (useful for testing)."""
        if not self.enabled: return
        try:
            cursor = '0'
            while cursor != 0:
                cursor, keys = self.client.scan(cursor=cursor, match=f"{self.prefix}*", count=100)
                if keys:
                    self.client.delete(*keys)
            logger.info("Cleared Redis cache namespace.")
        except Exception as e:
            logger.error(f"Redis Clear error: {e}")
