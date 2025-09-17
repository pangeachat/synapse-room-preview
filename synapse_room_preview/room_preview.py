from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_room_preview.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_room_preview import SynapseRoomPreviewConfig

logger = logging.getLogger("synapse.module.synapse_room_preview.room_preview")


class RoomPreview(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: SynapseRoomPreviewConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()
            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited"},
                    send_cors=True,
                )
                return
            respond_with_json(
                request,
                200,
                {"rooms": {}},
                send_cors=True,
            )

        except Exception as e:
            logger.error("Error processing request: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
