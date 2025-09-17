from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from synapse_room_preview import SynapseRoomPreviewConfig

request_log: Dict[str, List[float]] = {}


def is_rate_limited(user_id: str, config: SynapseRoomPreviewConfig) -> bool:
    current_time = time.time()

    # Get the list of request timestamps for the user, or create an empty list if new user
    if user_id not in request_log:
        request_log[user_id] = []

    # Filter out requests that are older than the time window
    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= config.burst_duration_seconds
    ]

    # Check if the number of requests in the time window exceeds the max limit
    if len(request_log[user_id]) >= config.requests_per_burst:
        return True

    # If not rate-limited, record the new request timestamp
    request_log[user_id].append(current_time)

    return False
