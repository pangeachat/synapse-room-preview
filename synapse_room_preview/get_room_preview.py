import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from synapse.storage.databases.main.room import RoomStore

if TYPE_CHECKING:
    from synapse_room_preview import SynapseRoomPreviewConfig

# In-memory cache for room preview data
# Structure: {room_id: (data, timestamp)}
_room_cache: Dict[str, Tuple[Dict[str, Dict[str, Any]], float]] = {}
_CACHE_TTL_SECONDS = 60  # 1 minute TTL


def _is_cache_valid(timestamp: float) -> bool:
    """Check if a cache entry is still valid based on TTL."""
    return time.time() - timestamp < _CACHE_TTL_SECONDS


def _get_cached_room(room_id: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Get cached room data if it exists and is still valid."""
    if room_id in _room_cache:
        data, timestamp = _room_cache[room_id]
        if _is_cache_valid(timestamp):
            return data
        else:
            # Remove expired entry
            del _room_cache[room_id]
    return None


def _cache_room_data(room_id: str, data: Dict[str, Dict[str, Any]]) -> None:
    """Cache room data with current timestamp."""
    _room_cache[room_id] = (data, time.time())


def _cleanup_expired_cache() -> None:
    """Remove expired entries from cache."""
    current_time = time.time()
    expired_keys = [
        room_id
        for room_id, (_, timestamp) in _room_cache.items()
        if current_time - timestamp >= _CACHE_TTL_SECONDS
    ]
    for room_id in expired_keys:
        del _room_cache[room_id]


async def get_room_preview(
    rooms: List[str], room_store: RoomStore, config: "SynapseRoomPreviewConfig"
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Get room preview data including state events for the specified rooms.

    Uses an in-memory cache with 1-minute TTL for individual room data to improve
    performance on repeated requests.

    Returns a dictionary with the structure:
    {
        [room_id]: {
            [state_event_type]: {
                [state_key]: JSON
            }
        }
    }

    Note: Empty matrix state key will be represented as "default" in the response.

    :param rooms: List of room IDs to get preview data for.
    :param room_store: The RoomStore instance to query the database.
    :param config: The configuration containing state event types to query.
    :return: A dictionary mapping room_id to state event data organized by
             event type and state key.
    """
    if not rooms or not config.room_preview_state_event_types:
        return {}

    # Clean up expired cache entries periodically
    _cleanup_expired_cache()

    # Check cache for each room and separate cached vs uncached rooms
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    rooms_to_fetch: List[str] = []

    for room_id in rooms:
        cached_data = _get_cached_room(room_id)
        if cached_data is not None:
            result[room_id] = cached_data
        else:
            rooms_to_fetch.append(room_id)

    # If all rooms were cached, return early
    if not rooms_to_fetch:
        return result

    # Fetch uncached rooms from database
    # Check which database backend we are using
    database_engine = room_store.db_pool.engine.module.__name__

    # Create placeholders for room IDs and event types
    room_placeholders = ",".join(
        ["?" if "sqlite" in database_engine else "%s"] * len(rooms_to_fetch)
    )
    event_type_placeholders = ",".join(
        ["?" if "sqlite" in database_engine else "%s"]
        * len(config.room_preview_state_event_types)
    )

    if "sqlite" in database_engine:
        # SQLite query
        query = f"""
            SELECT e.room_id, e.type, e.state_key, ej.json
            FROM events e
                JOIN state_events se ON e.event_id = se.event_id
                JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.room_id IN ({room_placeholders})
                AND e.type IN ({event_type_placeholders})
                AND se.room_id = e.room_id
                AND se.type = e.type
                AND (se.state_key = e.state_key OR (se.state_key IS NULL AND e.state_key IS NULL))
            ORDER BY e.room_id, e.type, e.state_key, e.origin_server_ts DESC
        """
        params = tuple(rooms_to_fetch + config.room_preview_state_event_types)

    else:
        # PostgreSQL query
        query = f"""
            SELECT DISTINCT ON (e.room_id, e.type, e.state_key)
                   e.room_id, e.type, e.state_key, ej.json
            FROM events e
            JOIN state_events se ON e.event_id = se.event_id
            JOIN event_json ej ON e.event_id = ej.event_id
            WHERE
                e.room_id IN ({room_placeholders})
                AND e.type IN ({event_type_placeholders})
                AND se.type = e.type
                AND (se.state_key = e.state_key OR (se.state_key IS NULL AND e.state_key IS NULL))
            ORDER BY e.room_id, e.type, e.state_key, e.origin_server_ts DESC
        """
        params = tuple(rooms_to_fetch + config.room_preview_state_event_types)

    rows = await room_store.db_pool.execute(
        "get_room_preview_state_events",
        query,
        *params,
    )

    # Initialize empty data for all rooms we're fetching
    fetched_room_data: Dict[str, Dict[str, Dict[str, Any]]] = {
        room_id: {} for room_id in rooms_to_fetch
    }

    # Process database results
    for row in rows:
        room_id, event_type, state_key, json_data = row
        if room_id not in fetched_room_data:
            fetched_room_data[room_id] = {}

        # Parse the JSON data if it's a string
        if isinstance(json_data, str):
            event_data = json.loads(json_data)
        else:
            event_data = json_data

        # Return the full Matrix event data (which contains "content" field)
        # Matrix events have a structure like: {"content": {...}, "type": "...", "state_key": "...", ...}
        # We return the complete event data

        # Store the event data, using state_key as a sub-key if present
        if event_type not in fetched_room_data[room_id]:
            fetched_room_data[room_id][event_type] = {}

        # Convert None or empty string state keys to "default"
        key = state_key if state_key is not None and state_key != "" else "default"
        fetched_room_data[room_id][event_type][key] = event_data

    # Cache each room's data individually and add to result
    for room_id, room_data in fetched_room_data.items():
        _cache_room_data(room_id, room_data)
        result[room_id] = room_data

    return result
