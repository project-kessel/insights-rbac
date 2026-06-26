"""Redis-based caching of per-Principal per-app access policy."""

import contextlib
import json
import logging
import pickle
import threading

from django.conf import settings
from prometheus_client import Counter
from redis import BlockingConnectionPool, exceptions
from redis.client import Pipeline, Redis

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

_connection_pool = None
_connection_pool_lock = threading.Lock()


def _is_mock_redis():
    """Check if Redis mocking is enabled (similar to MOCK_KAFKA)."""
    return getattr(settings, "MOCK_REDIS", False)


def _get_connection_pool():
    """Lazily initialize the Redis connection pool (thread-safe, double-checked locking)."""
    global _connection_pool
    if _connection_pool is None and not _is_mock_redis():
        with _connection_pool_lock:
            if _connection_pool is None:
                _connection_pool = BlockingConnectionPool(**settings.REDIS_CACHE_CONNECTION_PARAMS)
    return _connection_pool


redis_enable_cache_get_total = Counter("redis_enable_cache_get_total", "Total amount of use_caching is true")
redis_disable_cache_get_total = Counter(
    "redis_disable_cache_get_total", "Total amount of times cache has been disabled"
)

workspace_cache_total = Counter(
    "rbac_workspace_cache_total",
    "Total workspace cache lookups",
    ["cache_layer", "result"],
)

BATCH_DELETE_SIZE = 1000


class BasicCache:
    """Basic cache class to be inherited.

    When MOCK_REDIS is enabled, all Redis operations become no-ops.
    The _redis_mocked flag centralizes this check so individual methods
    do not need to call _is_mock_redis() directly.
    """

    def __init__(self):
        """Init the class."""
        self._connection = None
        self._redis_mocked = _is_mock_redis()
        self.use_caching = not self._redis_mocked

    @property
    def connection(self):
        """Get Redis connection from the pool. Returns None when Redis is mocked."""
        if self._redis_mocked:
            return None
        if not self._connection:
            self._connection = Redis(connection_pool=_get_connection_pool(), ssl=settings.REDIS_SSL)
            try:
                self._connection.ping()
            except exceptions.RedisError:
                self._connection = None
                raise
        return self._connection

    def enable_caching(self):
        """Enable caching and increment prometheus metric that redis caching is enabled."""
        self.use_caching = True
        logger.info("Redis Cache Enabled")
        redis_enable_cache_get_total.inc()
        return self.use_caching

    def disable_caching(self):
        """Disable caching and increment prometheus metric that redis caching is disabled."""
        self.use_caching = False
        logger.info("Redis Cache Disabled")
        redis_disable_cache_get_total.inc()
        return self.use_caching

    def redis_health_check(self):
        """Check whether redis cache is reachable. If it is not reachable, then disable caching."""
        if self._redis_mocked:
            return False
        self._connection = Redis(connection_pool=_get_connection_pool(), ssl=settings.REDIS_SSL)
        try:
            response = self._connection.ping()
            if response:
                logger.info("Redis cache is reachable.")
                self.enable_caching()
                return True
            else:
                logger.info("Redis cache is not reachable.")
                self.disable_caching()
                return False
        except Exception as e:
            logger.exception(f"Error: {e}")

    @contextlib.contextmanager
    def delete_handler(self, err_msg):
        """Handle delete events."""
        try:
            yield
        except exceptions.RedisError:
            logger.exception(err_msg)

    def get_from_redis(self, key):
        """Get object from redis based on key."""
        raise NotImplementedError("Please override the get_from_redis method.")

    def get_cached(self, key, error_message):
        """Get cached object from redis, throw error if there is any."""
        if self._redis_mocked:
            return None
        try:
            if self.redis_health_check() is True:
                if self.use_caching:
                    return self.get_from_redis(key)
            else:
                # Retrieve data directly
                logger.info("Not Retrieving Data from Redis Cache")
                self.disable_caching()
                pass
        except exceptions.RedisError:
            logger.exception(error_message)
        return None

    def delete_cached(self, key, obj_name):
        """Delete cache from redis."""
        if self._redis_mocked:
            return
        err_msg = f"Error deleting {obj_name} for {key}"
        with self.delete_handler(err_msg):
            logger.info(f"Deleting {obj_name} cache for {key}")
            self.connection.delete(self.key_for(key))

    def set_cache(self, pipe, key, item):
        """Set cache to redis."""
        raise NotImplementedError("Please override the set_cache method.")

    def save(self, key, item, obj_name):
        """Save cache including exception handler."""
        if self._redis_mocked:
            return
        try:
            logger.info(f"Caching {obj_name} for {key}")
            with self.connection.pipeline() as pipe:
                self.set_cache(pipe, key, item)
        except exceptions.RedisError:
            logger.exception(f"Error writing {obj_name} for {key}")
        finally:
            try:
                pipe.reset()
            except:  # noqa: E722
                pass


class TenantCache(BasicCache):
    """Redis-based caching of tenant."""

    def key_for(self, key):
        """Redis key for a given tenant."""
        return f"rbac::tenant::tenant={key}"

    def get_from_redis(self, key):
        """Override the method to get tenant based on key."""
        obj = self.connection.get(self.key_for(key))
        if obj:
            return pickle.loads(obj)

    def get_tenant(self, key):
        """Get the tenant by tenant_name."""
        return super().get_cached(key, f"Error querying tenant {key}")

    def set_cache(self, pipe, key, item):
        """Override the method to set tenant to cache."""
        pipe.set(self.key_for(key), pickle.dumps(item))
        pipe.expire(self.key_for(key), settings.ACCESS_CACHE_LIFETIME)
        pipe.execute()

    def save_tenant(self, tenant):
        """Write the tenant for a request to Redis."""
        super().save(tenant.org_id, tenant, "tenant")

    def delete_tenant(self, key):
        """Purge the given tenant from the cache."""
        super().delete_cached(key, "tenant")


class AccessCache(BasicCache):
    """Redis-based caching of per-Principal per-app access policy."""  # noqa: D204

    def __init__(self, tenant: str):
        """
        tenant: The name of the database schema for this tenant.

        if tenant is *, then it is for all tenants.
        """
        if not tenant:
            raise ValueError("tenant must be provided")
        self.tenant = tenant
        super().__init__()

    def key_for(self, uuid):
        """Redis key for a given user policy."""
        return f"rbac::policy::tenant={self.tenant}::user={uuid}"

    def set_cache(self, pipe, args, item):
        """Set cache to redis."""
        pipe.hset(self.key_for(args[0]), args[1], json.dumps(item))
        pipe.expire(self.key_for(args[0]), settings.ACCESS_CACHE_LIFETIME)
        pipe.execute()

    def get_from_redis(self, args):
        """Get object from redis based on args."""
        obj = self.connection.hget(*(self.key_for(args[0]), args[1]))
        if obj:
            return json.loads(obj)

    def get_policy(self, uuid, sub_key):
        """Get the given user's policy for the given sub_key (application_offset_limit)."""
        if not settings.ACCESS_CACHE_ENABLED:
            return None
        return super().get_cached((uuid, sub_key), f"Error querying policy for uuid {uuid}")

    def delete_policy(self, uuid):
        """Purge the given user's policy from the cache."""
        super().delete_cached(uuid, "policy")

    def delete_all_policies_for_tenant(self):
        """Purge users' policies for a given tenant from the cache."""
        if not settings.ACCESS_CACHE_ENABLED or self._redis_mocked:
            return
        err_msg = f"Error deleting all policies for tenant {self.tenant}"
        with self.delete_handler(err_msg):
            logger.info(f"Deleting entire policy cache for tenant {self.tenant}")
            # Following piece is taken from https://github.com/jazzband/django-redis/pull/617
            count = 0
            pipeline = self.connection.pipeline()
            for key in self.connection.scan_iter(match=self.key_for("*"), count=BATCH_DELETE_SIZE):
                pipeline.delete(key)
                count += 1
            pipeline.execute()
            logger.info(f"Deleted {count} policies for tenant {self.tenant}")

    def save_policy(self, uuid, sub_key, policy):
        """Write the policy for a given user for a given sub_key (application_offset_limit) to Redis."""
        if not settings.ACCESS_CACHE_ENABLED:
            return
        super().save((uuid, sub_key), policy, "policy")


class JWKSCache(BasicCache):
    """Redis-based caching for the storage of JKWS certificates."""

    JWKS_CACHE_KEY = "rbac::jwks:response"

    def key_for(self):
        """Redis key for the JWKS certificate response."""
        return "rbac::jwks::response"

    def set_cache(self, pipe, key, item):
        """Set cache to redis."""
        pipe.hset(key=key, value=json.dumps(item), name="hi")
        pipe.expire(name=key, time=settings.IT_TOKEN_JKWS_CACHE_LIFETIME)
        pipe.execute()

    def get_from_redis(self, args):
        """Get object from redis based on args."""
        obj = self.connection.hget(*(self.JWKS_CACHE_KEY, args[1]))
        if obj:
            return json.loads(obj)

    def get_jwks_response(self):
        """Get the JWKS certificates' response from Redis."""
        return super().get_cached(self.JWKS_CACHE_KEY, "Unable to fetch the JWKS response from Redis")

    def set_jwks_response(self, response):
        """Save the JWKS certificates' response in Redis."""
        super().save(self.JWKS_CACHE_KEY, response, "JWKS response")


class JWTCache(BasicCache):
    """Redis-based caching for the storage of JWT token."""

    JWT_CACHE_KEY = "rbac::jwt::relations"

    def key_for(self):
        """Redis key for the JWT token response."""
        return "rbac::jwt::relations"

    def set_cache(self, pipe, key, item):
        """Set cache to redis."""
        pipe.set(name=key, value=item)
        pipe.expire(name=key, time=settings.IT_TOKEN_JKWS_CACHE_LIFETIME)
        pipe.execute()

    def get_from_redis(self, key):
        """Get object from redis based on key."""
        obj = self.connection.get(name=key)
        if obj:
            return obj.decode("utf-8") if isinstance(obj, bytes) else obj
        return None

    def get_jwt_response(self):
        """Get the JWT token response from Redis."""
        return super().get_cached(self.JWT_CACHE_KEY, "Unable to fetch the JWT response from Redis")

    def set_jwt_response(self, response):
        """Save the JWT token response in Redis."""
        super().save(self.JWT_CACHE_KEY, response, "JWT response")


class JWTCacheOptimized(JWTCache):
    """Optimized JWT cache for high-throughput consumers (Kafka).

    This cache skips redundant health checks for performance in message processing scenarios.
    Use this instead of JWTCache for consumers that process many messages per second.
    """

    def get_jwt_response(self):
        """Get the JWT token response from Redis without health check overhead."""
        if self._redis_mocked or not self.use_caching:
            return None

        try:
            return self.get_from_redis(self.JWT_CACHE_KEY)
        except exceptions.RedisError:
            logger.exception("Unable to fetch the JWT response from Redis")
            # Disable caching temporarily on error
            self.disable_caching()
        return None


class PrincipalCache(BasicCache):
    """Redis-based caching for storing the principals."""

    def key_for(self, org_id: str, principal_username: str) -> str:
        """Generate the cache key for Redis.

        :param org_id: The tenant of the principal.
        :param principal_username: The username of the principal.
        :returns: The key used in Redis to store principals.
        """
        return f"rbac::principal::{org_id}::{principal_username}"

    def set_cache(self, pipe: Pipeline, key: str, principal):
        """Set cache to redis."""
        pipe.set(name=key, value=pickle.dumps(principal))
        pipe.expire(name=key, time=settings.PRINCIPAL_CACHE_LIFETIME)
        pipe.execute()

    def get_from_redis(self, key: str):
        """Get principal from redis based on the tenant and the principal."""
        principal = self.connection.get(name=key)
        if principal:
            return pickle.loads(principal)
        else:
            return None

    def get_principal(self, org_id: str, principal_username: str):
        """Fetch the principal from the cache.

        :param org_id: The tenant of the principal.
        :param principal_username: The username of the principal to fetch.
        :returns: The principal itself or None.
        """
        return super().get_cached(
            self.key_for(org_id, principal_username),
            f'[org_id: "{org_id}"][principal_username: "{principal_username}"] Unable to fetch principal from cache',
        )

    def cache_principal(self, org_id: str, principal):
        """Cache the given principal.

        :param org_id: The tenant of the principal.
        :param principal: The principal object to cache.
        """
        super().save(
            key=self.key_for(org_id, principal.username),
            item=principal,
            obj_name="principal",
        )

    def delete_all_principals_for_tenant(self, org_id: str):
        """Purge all principals for a given tenant from the cache.

        :param org_id: The tenant org_id to clear principals for.
        """
        if self._redis_mocked:
            return
        err_msg = f"Error deleting all principals for tenant {org_id}"
        with self.delete_handler(err_msg):
            logger.info(f"Deleting entire principal cache for tenant {org_id}")
            count = 0
            pipeline = self.connection.pipeline()
            for key in self.connection.scan_iter(match=f"rbac::principal::{org_id}::*", count=BATCH_DELETE_SIZE):
                pipeline.delete(key)
                count += 1
            pipeline.execute()
            logger.info(f"Deleted {count} principals for tenant {org_id}")


class WorkspaceCache(BasicCache):
    """Redis-based caching for immutable root and default workspaces.

    Two key namespaces:
    - Model cache: stores pickled Workspace instances (TTL: PRINCIPAL_CACHE_LIFETIME, 3600s)
    - Response cache: stores serialized API responses (TTL: ACCESS_CACHE_LIFETIME, 600s)

    Only caches built-in (root/default) workspaces which are per-tenant immutable singletons.
    """

    def key_for(self, org_id: str, workspace_type: str) -> str:
        """Generate cache key for a workspace model."""
        return f"rbac::workspace::{org_id}::{workspace_type}"

    def response_key_for(self, org_id: str, cache_key: str) -> str:
        """Generate cache key for a cached API response."""
        return f"rbac::workspace::response::{org_id}::{cache_key}"

    def set_cache(self, pipe: Pipeline, key: str, item):
        """Set workspace model to cache."""
        pipe.set(name=key, value=pickle.dumps(item))
        pipe.expire(name=key, time=settings.PRINCIPAL_CACHE_LIFETIME)
        pipe.execute()

    def get_from_redis(self, key: str):
        """Get workspace from redis."""
        obj = self.connection.get(name=key)
        if obj:
            return pickle.loads(obj)
        return None

    def get_workspace(self, org_id: str, workspace_type: str):
        """Fetch a workspace from cache by org_id and type.

        :param org_id: The tenant's org_id.
        :param workspace_type: The workspace type (root or default).
        :returns: The Workspace instance or None.
        """
        if not settings.ACCESS_CACHE_ENABLED:
            return None
        result = super().get_cached(
            self.key_for(org_id, workspace_type),
            f"Unable to fetch workspace ({workspace_type}) from cache for org {org_id}",
        )
        workspace_cache_total.labels(cache_layer="model", result="hit" if result is not None else "miss").inc()
        return result

    def cache_workspace(self, org_id: str, workspace):
        """Cache a workspace instance.

        :param org_id: The tenant's org_id.
        :param workspace: The Workspace model instance to cache.
        """
        if not settings.ACCESS_CACHE_ENABLED:
            return
        super().save(
            key=self.key_for(org_id, workspace.type),
            item=workspace,
            obj_name="workspace",
        )

    def _get_json(self, key: str, err_msg: str):
        """Fetch a JSON-serialized value from Redis with health check.

        :param key: The full Redis key.
        :param err_msg: Error message for logging on failure.
        :returns: The deserialized data or None.
        """
        if not settings.ACCESS_CACHE_ENABLED or self._redis_mocked:
            return None
        try:
            if not self.redis_health_check():
                self.disable_caching()
                return None
            obj = self.connection.get(name=key)
            return json.loads(obj) if obj else None
        except exceptions.RedisError:
            logger.exception(err_msg)
            return None

    def _cache_json(self, key: str, data, ttl: int, log_msg: str, err_msg: str):
        """Write a JSON-serialized value to Redis with a TTL.

        :param key: The full Redis key.
        :param data: The data to serialize (must be JSON-serializable).
        :param ttl: Time-to-live in seconds.
        :param log_msg: Debug message logged on write.
        :param err_msg: Error message for logging on failure.
        """
        if not settings.ACCESS_CACHE_ENABLED or self._redis_mocked:
            return
        try:
            logger.debug(log_msg)
            with self.connection.pipeline() as pipe:
                pipe.set(name=key, value=json.dumps(data))
                pipe.expire(name=key, time=ttl)
                pipe.execute()
        except exceptions.RedisError:
            logger.exception(err_msg)

    def get_response(self, org_id: str, cache_key: str):
        """Fetch a cached API response.

        :param org_id: The tenant's org_id.
        :param cache_key: The cache key suffix (e.g. workspace type or workspace id).
        :returns: The cached response data (dict) or None.
        """
        if not settings.ACCESS_CACHE_ENABLED:
            return None
        result = self._get_json(
            self.response_key_for(org_id, cache_key),
            err_msg=f"Error fetching workspace response cache for org {org_id}",
        )
        workspace_cache_total.labels(cache_layer="response", result="hit" if result is not None else "miss").inc()
        return result

    def cache_response(self, org_id: str, cache_key: str, data):
        """Cache an API response.

        :param org_id: The tenant's org_id.
        :param cache_key: The cache key suffix.
        :param data: The response data (must be JSON-serializable).
        """
        self._cache_json(
            self.response_key_for(org_id, cache_key),
            data,
            ttl=settings.ACCESS_CACHE_LIFETIME,
            log_msg=f"Caching workspace response for org {org_id} key {cache_key}",
            err_msg=f"Error writing workspace response cache for org {org_id}",
        )

    def delete_workspaces_for_tenant(self, org_id: str):
        """Invalidate all workspace caches (model + response) for a tenant.

        :param org_id: The tenant's org_id.
        """
        if self._redis_mocked:
            return
        err_msg = f"Error deleting workspace cache for tenant {org_id}"
        with self.delete_handler(err_msg):
            logger.info(f"Deleting entire workspace cache for tenant {org_id}")
            count = 0
            pipeline = self.connection.pipeline()
            for key in self.connection.scan_iter(match=f"rbac::workspace::{org_id}::*", count=BATCH_DELETE_SIZE):
                pipeline.delete(key)
                count += 1
            for key in self.connection.scan_iter(
                match=f"rbac::workspace::response::{org_id}::*", count=BATCH_DELETE_SIZE
            ):
                pipeline.delete(key)
                count += 1
            pipeline.execute()
            logger.info(f"Deleted {count} workspace cache entries for tenant {org_id}")


WORKSPACE_CACHE = WorkspaceCache()


def skip_purging_cache_for_public_tenant(tenant):
    """Skip purging cache for public tenant."""
    # Cache is by tenant org_id and user_id, we don't have to purge cache for public tenant
    if tenant.tenant_name == "public":
        return True
