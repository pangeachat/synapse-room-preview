from typing import Any, Dict

import attr
from synapse.module_api import ModuleApi

from synapse_room_preview.room_preview import RoomPreview


@attr.s(auto_attribs=True, frozen=True)
class SynapseRoomPreviewConfig:
    knock_with_code_burst_duration_seconds: int = 60
    knock_with_code_requests_per_burst: int = 10


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
        #
        # Example:
        #
        #     some_option = config.get("some_option")
        #     if some_option is None:
        #          raise ConfigError("Missing option 'some_option'")
        #      if not isinstance(some_option, str):
        #          raise ConfigError("Config option 'some_option' must be a string")
        #
        return SynapseRoomPreviewConfig()
