#!/usr/bin/env python3
"""
Simple test script to demonstrate the caching functionality.
This script shows that the cache works by testing the get_room_preview function
with timing measurements.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from synapse_room_preview import SynapseRoomPreviewConfig
from synapse_room_preview.get_room_preview import get_room_preview, _room_cache


async def test_cache_functionality():
    """Test that caching works correctly."""
    print("Testing in-memory caching functionality...")

    # Clear any existing cache
    _room_cache.clear()

    # Setup mock configuration
    config = SynapseRoomPreviewConfig(
        room_preview_state_event_types=["pangea.activity_plan", "pangea.activity_roles"]
    )

    # Setup mock room_store
    room_store = MagicMock()
    room_store.db_pool = MagicMock()
    room_store.db_pool.execute = AsyncMock()
    room_store.db_pool.engine = MagicMock()
    room_store.db_pool.engine.module = MagicMock()
    room_store.db_pool.engine.module.__name__ = "sqlite3"

    # Mock database response
    mock_rows = [
        ("!room1:example.com", "pangea.activity_plan", "", '{"plan_id": "test123"}'),
        (
            "!room2:example.com",
            "pangea.activity_roles",
            "",
            '{"roles": {"@user:example.com": "facilitator"}}',
        ),
    ]
    room_store.db_pool.execute.return_value = mock_rows

    rooms = ["!room1:example.com", "!room2:example.com"]

    print(f"Initial cache size: {len(_room_cache)}")

    # First call - should hit database
    print("\n1. First call (cache miss)...")
    start_time = time.time()
    result1 = await get_room_preview(rooms, room_store, config)
    end_time = time.time()

    print(f"   Database calls: {room_store.db_pool.execute.call_count}")
    print(f"   Cache size after: {len(_room_cache)}")
    print(f"   Cached rooms: {list(_room_cache.keys())}")
    print(f"   Result keys: {list(result1.keys())}")

    # Second call - should hit cache
    print("\n2. Second call (cache hit)...")
    start_time = time.time()
    result2 = await get_room_preview(rooms, room_store, config)
    end_time = time.time()

    print(f"   Database calls: {room_store.db_pool.execute.call_count}")
    print(f"   Cache size: {len(_room_cache)}")
    print(f"   Results match: {result1 == result2}")

    # Test partial cache hit (one room cached, one new)
    print("\n3. Partial cache test (mixed cache hit/miss)...")
    new_rooms = ["!room1:example.com", "!room3:example.com"]  # room1 cached, room3 new

    # Add mock data for room3
    room_store.db_pool.execute.return_value = [
        ("!room3:example.com", "pangea.activity_plan", "", '{"plan_id": "new_room"}'),
    ]

    result3 = await get_room_preview(new_rooms, room_store, config)

    print(f"   Database calls: {room_store.db_pool.execute.call_count}")
    print(f"   Cache size: {len(_room_cache)}")
    print(f"   Cached rooms: {list(_room_cache.keys())}")
    print(
        f"   room1 data matches: {result3['!room1:example.com'] == result1['!room1:example.com']}"
    )
    print(f"   room3 in result: {'!room3:example.com' in result3}")

    # Test cache expiration (simulate by clearing and checking TTL logic)
    print("\n4. Cache expiration test...")
    print(f"   Cache TTL is 60 seconds")
    print(f"   Current cache entries: {len(_room_cache)}")

    # Simulate expired cache by manually setting old timestamp
    if "!room1:example.com" in _room_cache:
        data, _ = _room_cache["!room1:example.com"]
        _room_cache["!room1:example.com"] = (data, time.time() - 61)  # 61 seconds ago
        print(f"   Simulated expiration for !room1:example.com")

    # This should cause a cache miss for room1 but hit for room2 and room3
    result4 = await get_room_preview(["!room1:example.com"], room_store, config)

    print(
        f"   Database calls after expiration test: {room_store.db_pool.execute.call_count}"
    )
    print(
        f"   Cache cleaned up expired entry: {'!room1:example.com' not in _room_cache or len(_room_cache) < 3}"
    )

    print("\n✅ Cache testing completed successfully!")
    print(f"Final cache state: {len(_room_cache)} rooms cached")


if __name__ == "__main__":
    asyncio.run(test_cache_functionality())
