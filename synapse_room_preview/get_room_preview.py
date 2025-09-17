import json
from typing import TYPE_CHECKING, Any, Dict, List

from synapse.storage.databases.main.room import RoomStore

if TYPE_CHECKING:
    from synapse_room_preview import SynapseRoomPreviewConfig


async def get_room_preview(
    rooms: List[str], room_store: RoomStore, config: "SynapseRoomPreviewConfig"
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Get room preview data including state events for the specified rooms.

    Returns a dictionary with the structure:
    {
        [room_id]: {
            [state_event_type]: {
                [state_key]: JSON
            }
        }
    }

    Note: Empty matrix state key will mean that the state event is queried
    with empty string state key.

    :param rooms: List of room IDs to get preview data for.
    :param room_store: The RoomStore instance to query the database.
    :param config: The configuration containing state event types to query.
    :return: A dictionary mapping room_id to state event data organized by
             event type and state key.
    """
    if not rooms or not config.room_preview_state_event_types:
        return {}

    # Check which database backend we are using
    database_engine = room_store.db_pool.engine.module.__name__

    # Create placeholders for room IDs and event types
    room_placeholders = ",".join(
        ["?" if "sqlite" in database_engine else "%s"] * len(rooms)
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
        params = tuple(rooms + config.room_preview_state_event_types)

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
        params = tuple(rooms + config.room_preview_state_event_types)

    rows = await room_store.db_pool.execute(
        "get_room_preview_state_events",
        query,
        *params,
    )

    # Organize results by room_id
    result: Dict[str, Dict[str, Dict[str, Any]]] = {room_id: {} for room_id in rooms}

    for row in rows:
        room_id, event_type, state_key, json_data = row
        if room_id not in result:
            result[room_id] = {}

        # Parse the JSON data if it's a string
        if isinstance(json_data, str):
            event_content = json.loads(json_data)
        else:
            event_content = json_data

        # Store the event data, using state_key as a sub-key if present
        if event_type not in result[room_id]:
            result[room_id][event_type] = {}

        key = state_key if state_key is not None else ""
        result[room_id][event_type][key] = event_content

    return result
