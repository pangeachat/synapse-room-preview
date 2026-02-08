You own the docs. Three sources of truth must agree: **docs**, **code**, and **prior user guidance**. When they don't, resolve it. Update `.github/instructions/` docs when your changes shift conventions. Fix obvious factual errors (paths, class names) without asking. Flag ambiguity when sources contradict.

Synapse module (Python 3.9+). Read state events without room membership (events defined in ansible config).
- **API**: `GET /_synapse/client/unstable/org.pangea/room_preview?rooms=room1,room2`