from typing import Any, Dict, List

import attr
from synapse.module_api import ModuleApi

from synapse_room_preview.room_preview import RoomPreview


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomPreviewConfig:
    room_preview_state_event_types: List[str]
    burst_duration_seconds: int = 60
    requests_per_burst: int = 10


class SynapseRoomPreview:
    def __init__(self, config: SynapseRoomPreviewConfig, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config

        # Initiate resources
        self.room_preview_resource = RoomPreview(api, config)

        # Register the HTTP endpoint for room_preview
        self._api.register_web_resource(
            path="/_synapse/client/unstable/org.pangea/room_preview",
            resource=self.room_preview_resource,
        )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> SynapseRoomPreviewConfig:
        # Parse the module's configuration here.
        # If there is an issue with the configuration, raise a
        # synapse.module_api.errors.ConfigError.

        # Parse room_preview_state_event_types, default to ["p.room_summary"]
        room_preview_state_event_types = config.get(
            "room_preview_state_event_types", ["p.room_summary"]
        )
        if not isinstance(room_preview_state_event_types, list):
            room_preview_state_event_types = ["p.room_summary"]

        # Parse other configuration options with defaults
        burst_duration_seconds = config.get("burst_duration_seconds", 60)
        requests_per_burst = config.get("requests_per_burst", 10)

        return SynapseRoomPreviewConfig(
            room_preview_state_event_types=room_preview_state_event_types,
            burst_duration_seconds=burst_duration_seconds,
            requests_per_burst=requests_per_burst,
        )
