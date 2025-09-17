# Synapse Room Preview

This module allow authenticated users to read content of pre-configured state events of a room without being a member of said room.

## Endpoint Behavior

The module exposes a REST endpoint at `/_synapse/client/unstable/org.pangea/room_preview` that allows authenticated users to retrieve specific state events from Matrix rooms without being a member of those rooms.

### Endpoint Details

- **URL:** `/_synapse/client/unstable/org.pangea/room_preview`
- **Method:** GET
- **Authentication:** Required (valid Matrix access token)
- **Rate Limiting:** Configurable burst limit (default: 10 requests per 60 seconds per user)

### Query Parameters

- `rooms` (optional): Comma-delimited list of room IDs to fetch preview data for
  - Example: `?rooms=!room1:example.com,!room2:example.com`
  - If omitted, returns empty rooms object

### Response Format

```json
{
  "rooms": {
    "!room_id:example.com": {
      "event_type": {
        "state_key": {
          // Full event JSON content
        }
      }
    }
  }
}
```

### Response Structure

- **Success (200):** Returns room preview data in the format above
- **Rate Limited (429):** `{"error": "Rate limited"}` when user exceeds configured limits
- **Server Error (500):** `{"error": "Internal server error"}` for unexpected errors
- **Empty Response:** Returns `{"rooms": {}}` when no rooms parameter provided or no matching rooms found

### Caching

The module implements an in-memory cache with a 1-minute TTL to improve performance on repeated requests for the same room data.

### Usage Examples

#### Get preview data for a single room
```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
     "https://matrix.example.com/_synapse/client/unstable/org.pangea/room_preview?rooms=!room_id:example.com"
```

#### Get preview data for multiple rooms
```bash
curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
     "https://matrix.example.com/_synapse/client/unstable/org.pangea/room_preview?rooms=!room1:example.com,!room2:example.com"
```

#### Example response
```json
{
  "rooms": {
    "!room1:example.com": {
      "m.room.name": {
        "default": {
          "name": "Public Discussion Room"
        }
      },
      "p.room_summary": {
        "default": {
          "summary": "A place for general discussions",
          "participant_count": 42
        }
      }
    },
    "!room2:example.com": {}
  }
}
```

### Configuration

The endpoint behavior is controlled by the module configuration (see Installation section below).


## Installation

From the virtual environment that you use for Synapse, install this module with:
```shell
pip install path/to/synapse-room-preview
```
(If you run into issues, you may need to upgrade `pip` first, e.g. by running
`pip install --upgrade pip`)

Then alter your homeserver configuration, adding to your `modules` configuration:
```yaml
modules:
  - module: synapse_room_preview.SynapseRoomPreview
    config:
      # List of state event types that can be read through the preview endpoint
      room_preview_state_event_types:
        - "p.room_summary"        # Default state event type
        - "pangea.activity_plan"  # Custom event types
        - "pangea.activity_roles"
        - "m.room.name"          # Standard Matrix room name
        - "m.room.topic"         # Standard Matrix room topic
      
      # Rate limiting configuration (optional)
      burst_duration_seconds: 60    # Time window for rate limiting (default: 60)
      requests_per_burst: 10        # Max requests per time window (default: 10)
```

### Configuration Options

- **`room_preview_state_event_types`** (required): List of Matrix state event types that users can read through the preview endpoint. Only these event types will be returned in responses.

- **`burst_duration_seconds`** (optional, default: 60): The time window in seconds for rate limiting. Users can make up to `requests_per_burst` requests within this time window.

- **`requests_per_burst`** (optional, default: 10): Maximum number of requests a user can make within the `burst_duration_seconds` time window.


## Development

In a virtual environment with pip ≥ 21.1, run
```shell
pip install -e .[dev]
```

To run the unit tests, you can either use:
```shell
tox -e py
```
or
```shell
trial tests
```

To view test logs for debugging, use:
```shell
tail -f synapse.log
```

To run the linters and `mypy` type checker, use `./scripts-dev/lint.sh`.


## Releasing

The exact steps for releasing will vary; but this is an approach taken by the
Synapse developers (assuming a Unix-like shell):

 1. Set a shell variable to the version you are releasing (this just makes
    subsequent steps easier):
    ```shell
    version=X.Y.Z
    ```

 2. Update `setup.cfg` so that the `version` is correct.

 3. Stage the changed files and commit.
    ```shell
    git add -u
    git commit -m v$version -n
    ```

 4. Push your changes.
    ```shell
    git push
    ```

 5. When ready, create a signed tag for the release:
    ```shell
    git tag -s v$version
    ```
    Base the tag message on the changelog.

 6. Push the tag.
    ```shell
    git push origin tag v$version
    ```

 7. If applicable:
    Create a *release*, based on the tag you just pushed, on GitHub or GitLab.

 8. If applicable:
    Create a source distribution and upload it to PyPI:
    ```shell
    python -m build
    twine upload dist/synapse_room_preview-$version*
    ```
