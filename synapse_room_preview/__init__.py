from typing import Any, Dict, List, Mapping, Optional, Tuple

import attr
from synapse.events import EventBase
from synapse.module_api import ModuleApi

from synapse_room_preview.get_room_preview import invalidate_room_cache
from synapse_room_preview.room_preview import RoomPreview


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomPreviewConfig:
    room_preview_state_event_types: List[str]
    burst_duration_seconds: int = 60
    requests_per_burst: int = 10

    _set_room_preview_state_event_types: Optional[set[str]] = None

    @property
    def set_room_preview_state_event_types(self) -> set[str]:
        if self._set_room_preview_state_event_types is not None:
            return self._set_room_preview_state_event_types
        return set(self.room_preview_state_event_types)


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

        # Register reactive cache invalidation callback
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._on_new_event,
        )

    async def _on_new_event(
        self,
        event: EventBase,
        _: Mapping[Tuple[str, str], EventBase],
    ) -> None:
        """
        Handle new events to reactively invalidate cache when relevant state events change.

        This callback is triggered for every new event in the homeserver.
        We only care about state events that match our configured preview types.
        """
        # Only process state events
        if not event.is_state():
            return

        # Only process events for types we care about
        if event.type not in self._config.set_room_preview_state_event_types:
            return

        room_id = event.room_id

        # Invalidate the entire cache for this room to force a fresh fetch
        # This is simpler and more reliable than trying to update specific data
        invalidate_room_cache(room_id)

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
