"""Guard the Redis client/Kombu pairing used for reply publication."""

from importlib.metadata import version

import redis
from kombu.transport.redis import Channel


def test_kombu_redis_transport_accepts_pinned_client() -> None:
    assert version("celery") == "5.4.0"
    assert version("kombu") == "5.6.2"
    assert version("redis") == "5.1.1"

    # This is the transport guard reached by Celery publication before the
    # broker connection. It needs no live Redis or external side effects.
    channel = Channel.__new__(Channel)
    channel.global_keyprefix = ""
    assert channel._get_client() is redis.Redis
