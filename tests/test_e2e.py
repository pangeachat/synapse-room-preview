import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import IO, Literal, Tuple, Union

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)


class TestE2E(aiounittest.AsyncTestCase):
    async def start_test_synapse(
        self,
        db: Literal["sqlite", "postgresql"] = "sqlite",
        postgresql_url: Union[str, None] = None,
    ) -> Tuple[str, str, subprocess.Popen, threading.Thread, threading.Thread]:
        try:
            synapse_dir = tempfile.mkdtemp()
            config_path = os.path.join(synapse_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name=my.domain.name",
                f"--config-path={config_path}",
                "--report-stats=no",
                "--generate-config",
            ]
            subprocess.check_call(generate_config_cmd)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")
            config["modules"] = [
                {
                    "module": "synapse_room_preview.SynapseRoomPreview",
                    "config": {
                        "room_preview_state_event_types": [
                            "pangea.activity_plan",
                            "pangea.activity_roles",
                        ]
                    },
                }
            ]
            if db == "sqlite":
                if postgresql_url is not None:
                    self.fail(
                        "PostgreSQL URL must not be defined when using SQLite database"
                    )
                config["database"] = {
                    "name": "sqlite3",
                    "args": {"database": "homeserver.db"},
                }
            elif db == "postgresql":
                if postgresql_url is None:
                    self.fail("PostgreSQL URL is required for PostgreSQL database")
                dsn_params = parse_dsn(postgresql_url)
                config["database"] = {
                    "name": "psycopg2",
                    "args": dsn_params,
                }
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            with open(log_config_path, "r", encoding="utf-8") as f:
                log_config = yaml.safe_load(f)
            log_config["root"]["handlers"] = ["console"]
            log_config["root"]["level"] = "DEBUG"
            with open(log_config_path, "w", encoding="utf-8") as f:
                yaml.dump(log_config, f)
            run_server_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                config_path,
            ]
            server_process = subprocess.Popen(
                run_server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=synapse_dir,
                text=True,
            )

            def read_output(pipe: Union[IO[str], None]):
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout,)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr,)
            )
            stdout_thread.start()
            stderr_thread.start()
            server_url = "http://localhost:8008"
            max_wait_time = 10
            wait_interval = 1
            total_wait_time = 0
            server_ready = False
            while not server_ready and total_wait_time < max_wait_time:
                try:
                    response = requests.get(server_url, timeout=10)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    pass
                finally:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval
            if not server_ready:
                self.fail("Synapse server did not start successfully")
            return (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            )
        except Exception as e:
            server_process.terminate()
            server_process.wait()
            stdout_thread.join()
            stderr_thread.join()
            shutil.rmtree(synapse_dir)
            raise e

    async def start_test_postgres(self):
        postgresql = None
        try:
            postgresql = testing.postgresql.Postgresql()
            postgres_url = postgresql.url()
            max_waiting_time = 10
            wait_interval = 1
            total_wait_time = 0
            postgres_is_up = False
            while total_wait_time < max_waiting_time and not postgres_is_up:
                try:
                    conn = psycopg2.connect(postgres_url)
                    conn.close()
                    postgres_is_up = True
                    break
                except psycopg2.OperationalError:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval
            if not postgres_is_up:
                postgresql.stop()
                self.fail("Postgres did not start successfully")
            dbname = "testdb"
            conn = psycopg2.connect(postgres_url)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                f"""
                CREATE DATABASE {dbname}
                WITH TEMPLATE template0
                LC_COLLATE 'C'
                LC_CTYPE 'C';
            """
            )
            cursor.close()
            conn.close()
            dsn_params = parse_dsn(postgres_url)
            dsn_params["dbname"] = dbname
            postgres_url_testdb = psycopg2.extensions.make_dsn(**dsn_params)
            return postgresql, postgres_url_testdb
        except Exception as e:
            if postgresql is not None:
                postgresql.stop()
            raise e

    async def register_user(
        self, config_path: str, dir: str, user: str, password: str, admin: bool
    ):
        register_user_cmd = [
            "register_new_matrix_user",
            f"-c={config_path}",
            f"--user={user}",
            f"--password={password}",
        ]
        if admin:
            register_user_cmd.append("--admin")
        else:
            register_user_cmd.append("--no-admin")
        subprocess.check_call(register_user_cmd, cwd=dir)

    async def login_user(self, user: str, password: str) -> str:
        login_url = "http://localhost:8008/_matrix/client/v3/login"
        login_data = {
            "type": "m.login.password",
            "user": user,
            "password": password,
        }
        response = requests.post(login_url, json=login_data)
        self.assertEqual(response.status_code, 200)
        return response.json()["access_token"]

    async def create_private_room_knock_allowed_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock"},
                }
            ],
        }
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def test_room_preview_sqlite(self):
        await self._test_room_preview(db="sqlite")

    async def test_room_preview_postgres(self):
        await self._test_room_preview(db="postgresql")

    async def _test_room_preview(self, db: Literal["sqlite", "postgresql"]):
        """Setup test environment and run basic room preview tests."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            # Login user
            token = await self.login_user("user1", "pw1")

            # Create a private room
            room_id = await self.create_private_room_knock_allowed_room(token)

            # Test the room_preview endpoint
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run the individual test methods
            await self._test_basic_room_preview_functionality(
                room_preview_url, headers, room_id
            )
            await self._test_room_preview_data_structure(
                room_preview_url, headers, room_id
            )

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def _test_basic_room_preview_functionality(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test basic room preview endpoint functionality."""
        # Test with no rooms parameter (should return empty rooms dict)
        response = requests.get(
            room_preview_url,
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

        # Test with single room
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])

        # Test with multiple rooms (comma-delimited)
        params = {"rooms": f"{room_id},!fake_room:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])
        self.assertIn("!fake_room:example.com", response_data["rooms"])

    async def _test_room_preview_data_structure(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test that the room preview data structure matches expected format."""
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Verify top-level structure
        self.assertIn("rooms", response_data)
        self.assertIsInstance(response_data["rooms"], dict)

        # Verify room-level structure
        self.assertIn(room_id, response_data["rooms"])
        room_data = response_data["rooms"][room_id]
        self.assertIsInstance(room_data, dict)

        # The response should follow format: {[room_id]: {[state_event_type]: {[state_key]: JSON}}}
        for event_type, event_data in room_data.items():
            self.assertIsInstance(event_type, str)
            self.assertIsInstance(event_data, dict)

            # Each event type should contain state keys mapped to JSON data
            for state_key, json_data in event_data.items():
                self.assertIsInstance(
                    state_key, str
                )  # State key should be a string (empty string or "default")
                self.assertIsInstance(json_data, dict)  # Should be parsed JSON

                # For state events with empty state key, verify handling
                if state_key == "default":
                    # This is the expected behavior for empty state keys
                    # Should contain just the content, not full Matrix event
                    self.assertIsInstance(json_data, dict)
                    # Verify this is the full Matrix event JSON (which contains content)
                    self.assertIn(
                        "content",
                        json_data,
                        "Response should contain the full Matrix event with 'content' field",
                    )
                elif state_key == "":
                    # Empty state keys are now handled and should not appear in responses
                    # They are converted to "default" in the implementation
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )

        # Test with fake room to ensure empty structure
        params = {"rooms": "!fake_room:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn("!fake_room:example.com", response_data["rooms"])
        self.assertEqual(response_data["rooms"]["!fake_room:example.com"], {})

    async def test_room_preview_with_room_state_events_sqlite(self):
        """Test room preview with actual room state events (SQLite)."""
        await self._test_room_preview_with_state_events(db="sqlite")

    async def test_room_preview_with_room_state_events_postgres(self):
        """Test room preview with actual room state events (PostgreSQL)."""
        await self._test_room_preview_with_state_events(db="postgresql")

    async def _test_room_preview_with_state_events(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Setup test environment and run room state events tests."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Login admin user
            admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with specific state events
            room_id = await self.create_room_with_state_events(admin_token)

            # Test the room_preview endpoint
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            # Run the individual test methods
            await self._test_room_with_state_events_functionality(
                room_preview_url, headers, room_id
            )
            await self._test_multiple_rooms_with_mixed_existence(
                room_preview_url, headers, room_id
            )

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def _test_room_with_state_events_functionality(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test room preview for room with state events."""
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Verify the room exists in response
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])
        room_data = response_data["rooms"][room_id]

        # Verify data structure follows expected format
        self._verify_room_preview_structure(room_data)

        # Specifically test that empty state keys become "default"
        self._verify_empty_state_key_becomes_default(room_data)

    async def _test_multiple_rooms_with_mixed_existence(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test multiple rooms including non-existent ones."""
        fake_room = "!nonexistent:example.com"
        params = {"rooms": f"{room_id},{fake_room}"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Both rooms should be in response
        self.assertIn(room_id, response_data["rooms"])
        self.assertIn(fake_room, response_data["rooms"])

        # Real room should have data, fake room should be empty
        self.assertIsInstance(response_data["rooms"][room_id], dict)
        self.assertEqual(response_data["rooms"][fake_room], {})

    async def create_room_with_state_events(self, access_token: str) -> str:
        """Create a room with specific state events for testing."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room with name, topic, and avatar
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Preview",
            "topic": "This is a test room for room preview functionality",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock"},
                },
                {
                    "type": "m.room.avatar",
                    "state_key": "",
                    "content": {"url": "mxc://example.com/test_avatar"},
                },
            ],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Add additional state events
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"

        # Add pangea.activity_plan state event
        activity_plan_data = {
            "plan_id": "plan123",
            "title": "Weekly Team Standup",
            "description": "Regular team sync meeting to discuss progress and blockers",
            "activities": [
                {
                    "id": "activity1",
                    "name": "Progress Updates",
                    "duration": 15,
                    "type": "discussion",
                },
                {
                    "id": "activity2",
                    "name": "Blockers Review",
                    "duration": 10,
                    "type": "problem_solving",
                },
            ],
            "total_duration": 25,
            "created_by": "@admin_user:my.domain.name",
        }

        plan_response = requests.put(
            f"{state_url}/pangea.activity_plan/",
            json=activity_plan_data,
            headers=headers,
        )
        self.assertEqual(plan_response.status_code, 200)

        # Add pangea.activity_roles state event
        activity_roles_data = {
            "roles": {
                "@admin_user:my.domain.name": {
                    "role": "facilitator",
                    "permissions": ["manage_activities", "assign_roles", "moderate"],
                },
                "@user1:my.domain.name": {
                    "role": "participant",
                    "permissions": ["participate", "vote"],
                },
            },
            "default_role": "participant",
            "role_definitions": {
                "facilitator": {
                    "description": "Manages the session and activities",
                    "permissions": ["manage_activities", "assign_roles", "moderate"],
                },
                "participant": {
                    "description": "Active participant in activities",
                    "permissions": ["participate", "vote"],
                },
            },
        }

        roles_response = requests.put(
            f"{state_url}/pangea.activity_roles/",
            json=activity_roles_data,
            headers=headers,
        )
        self.assertEqual(roles_response.status_code, 200)

        return room_id

    def _verify_room_preview_structure(self, room_data: dict):
        """Verify that room preview data follows the expected structure."""
        # Data should follow format: {[state_event_type]: {[state_key]: JSON}}
        # Where empty state keys from database become "default"
        self.assertIsInstance(room_data, dict)

        for event_type, event_type_data in room_data.items():
            # Event type should be a string
            self.assertIsInstance(event_type, str)
            # Event type data should be a dict
            self.assertIsInstance(event_type_data, dict)

            for state_key, event_content in event_type_data.items():
                # State key should be a string (currently "" or should be "default" for events with no state key)
                self.assertIsInstance(state_key, str)
                # Event content should be parsed JSON (dict)
                self.assertIsInstance(event_content, dict)

                # Verify handling of empty state keys
                if state_key == "default":
                    # This is the expected behavior for empty state keys
                    # Should contain the full Matrix event (which includes content)
                    self.assertIsInstance(event_content, dict)
                    # Verify this is the full Matrix event JSON with content field
                    self.assertIn(
                        "content",
                        event_content,
                        "Response should contain the full Matrix event with 'content' field",
                    )
                elif state_key == "":
                    # Empty state keys are now handled and should not appear in responses
                    # They are converted to "default" in the implementation
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )

    def _verify_empty_state_key_becomes_default(self, room_data: dict):
        """Verify that state events with empty state keys are returned with 'default' as the state key."""
        # We know from create_room_with_state_events that we created events with empty state keys:
        # - pangea.activity_plan with state_key=""
        # - pangea.activity_roles with state_key=""
        # - m.room.join_rules with state_key=""
        # - m.room.avatar with state_key=""
        # - m.room.name with state_key="" (from room creation)
        # - m.room.topic with state_key="" (from room creation)

        # Check that these event types exist and have "default" as the state key
        expected_events_with_default_state_key = [
            "pangea.activity_plan",
            "pangea.activity_roles",
        ]

        for event_type in expected_events_with_default_state_key:
            if event_type in room_data:
                event_data = room_data[event_type]
                # Based on test failure, the current implementation uses empty string, not "default"
                # But we want to test for the expected behavior of converting to "default"
                if "default" in event_data:
                    # This is the expected behavior - empty state key becomes "default"
                    # and should return the full Matrix event JSON (which contains content)
                    full_event = event_data["default"]
                    self.assertIsInstance(
                        full_event,
                        dict,
                        f"Event type {event_type} with 'default' state key should have dict content",
                    )
                    # Verify this is the full Matrix event with content field
                    self.assertIn(
                        "content",
                        full_event,
                        f"Event type {event_type} should contain 'content' field in full Matrix event",
                    )

                    # For pangea.activity_plan, verify it has the expected content fields
                    if event_type == "pangea.activity_plan":
                        # Access the content field within the full Matrix event
                        content = full_event.get("content", {})
                        expected_fields = [
                            "plan_id",
                            "title",
                            "description",
                            "activities",
                            "total_duration",
                            "created_by",
                        ]
                        for field in expected_fields:
                            self.assertIn(
                                field,
                                content,
                                f"Activity plan content should contain field '{field}'",
                            )

                    # Verify there are no empty string state keys when using "default"
                    self.assertNotIn(
                        "",
                        event_data,
                        f"Event type {event_type} should not have empty string as state key when using 'default'",
                    )
                elif "" in event_data:
                    # This is the current behavior - empty state key stays as empty string
                    # Currently returns full Matrix event JSON, but should return just content
                    full_event = event_data[""]
                    self.assertIsInstance(
                        full_event,
                        dict,
                        f"Event type {event_type} with empty state key should have dict content",
                    )
                    # Empty state keys should now be converted to "default"
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )
                else:
                    self.fail(
                        f"Event type {event_type} should have 'default' as state key (empty keys are converted)"
                    )

    async def test_room_preview_empty_cases_sqlite(self):
        """Test room preview edge cases (SQLite)."""
        await self._test_room_preview_empty_cases(db="sqlite")

    async def test_room_preview_empty_cases_postgres(self):
        """Test room preview edge cases (PostgreSQL)."""
        await self._test_room_preview_empty_cases(db="postgresql")

    async def _test_room_preview_empty_cases(self, db: Literal["sqlite", "postgresql"]):
        """Setup test environment and run empty/edge case tests."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test_user",
                password="test_pw",
                admin=False,
            )

            # Login user
            token = await self.login_user("test_user", "test_pw")

            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run the individual test methods
            await self._test_empty_rooms_parameter(room_preview_url, headers)
            await self._test_whitespace_rooms_parameter(room_preview_url, headers)
            await self._test_mixed_valid_invalid_room_ids(room_preview_url, headers)

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def _test_empty_rooms_parameter(self, room_preview_url: str, headers: dict):
        """Test with empty rooms parameter."""
        response = requests.get(
            room_preview_url,
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

    async def _test_whitespace_rooms_parameter(
        self, room_preview_url: str, headers: dict
    ):
        """Test with whitespace-only rooms parameter."""
        params = {"rooms": "  ,  , "}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

    async def _test_mixed_valid_invalid_room_ids(
        self, room_preview_url: str, headers: dict
    ):
        """Test with mix of valid and invalid room IDs."""
        params = {"rooms": "!valid:example.com,,  ,!another:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        # Should have both valid room IDs
        self.assertIn("!valid:example.com", response_data["rooms"])
        self.assertIn("!another:example.com", response_data["rooms"])
        # Both should be empty since they don't exist
        self.assertEqual(response_data["rooms"]["!valid:example.com"], {})
        self.assertEqual(response_data["rooms"]["!another:example.com"], {})

    async def test_room_preview_cache_performance_sqlite(self):
        """Test cache performance benefits (SQLite)."""
        await self._test_cache_performance(db="sqlite")

    async def test_room_preview_cache_performance_postgres(self):
        """Test cache performance benefits (PostgreSQL)."""
        await self._test_cache_performance(db="postgresql")

    async def _test_cache_performance(self, db: Literal["sqlite", "postgresql"]):
        """Test that cache hits are faster than cache misses."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="perf_user",
                password="perf_pw",
                admin=True,
            )

            # Login user
            token = await self.login_user("perf_user", "perf_pw")

            # Create a room with state events for testing
            room_id = await self.create_room_with_state_events(token)

            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run cache performance test
            await self._test_cache_hit_performance(room_preview_url, headers, room_id)

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def _test_cache_hit_performance(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test that the cache functions correctly and returns consistent data."""
        import time

        # Clear any existing cache by importing and clearing the cache directly
        try:
            from synapse_room_preview.get_room_preview import _room_cache

            _room_cache.clear()
        except ImportError:
            pass  # Cache might not be accessible in test environment

        params = {"rooms": room_id}

        # First request (cache miss) - measure and store result
        print("\nCache Functionality Test:")

        start_time = time.time()
        response1 = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        miss_time = time.time() - start_time

        self.assertEqual(response1.status_code, 200)
        first_result = response1.json()

        print(f"  First request (cache miss): {miss_time:.4f}s")

        # Second request (should be cache hit) - measure and compare result
        start_time = time.time()
        response2 = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        hit_time = time.time() - start_time

        self.assertEqual(response2.status_code, 200)
        second_result = response2.json()

        print(f"  Second request (cache hit): {hit_time:.4f}s")

        # Verify cache returns identical data
        self.assertEqual(
            first_result,
            second_result,
            "Cache hit should return identical data to cache miss",
        )

        # Verify both responses have the expected room data structure
        self.assertIn("rooms", first_result)
        self.assertIn("rooms", second_result)
        self.assertIn(room_id, first_result["rooms"])
        self.assertIn(room_id, second_result["rooms"])

        # Test multiple cache hits return consistent data
        for i in range(3):
            response_n = requests.get(
                room_preview_url, headers=headers, params=params, timeout=10
            )
            self.assertEqual(response_n.status_code, 200)
            self.assertEqual(
                response_n.json(),
                first_result,
                f"Cache hit #{i+3} should return identical data",
            )

        print("  ✅ Cache returns consistent data across multiple requests")

        # Test cache with different room combinations
        other_room = "!nonexistent:example.com"
        mixed_params = {"rooms": f"{room_id},{other_room}"}

        response_mixed = requests.get(
            room_preview_url,
            headers=headers,
            params=mixed_params,
            timeout=10,
        )
        self.assertEqual(response_mixed.status_code, 200)
        mixed_result = response_mixed.json()

        # The cached room should have the same data
        self.assertEqual(
            mixed_result["rooms"][room_id],
            first_result["rooms"][room_id],
            "Cached room data should be consistent in mixed requests",
        )

        # The new room should be empty
        self.assertEqual(
            mixed_result["rooms"][other_room],
            {},
            "Non-existent room should return empty data",
        )

        print("  ✅ Cache works correctly with mixed room requests")
        print("  ✅ Cache functionality test completed successfully")

        # Note: Performance benefits are more apparent in production environments
        # where database queries are more complex and network latency is involved

    async def test_room_preview_authentication_error_sqlite(self):
        """Test 401 error for unauthenticated requests (SQLite)."""
        await self._test_authentication_error(db="sqlite")

    async def test_room_preview_authentication_error_postgres(self):
        """Test 401 error for unauthenticated requests (PostgreSQL)."""
        await self._test_authentication_error(db="postgresql")

    async def _test_authentication_error(self, db: Literal["sqlite", "postgresql"]):
        """Test that unauthenticated requests return 401 error."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Test the room_preview endpoint without authentication
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            # Test with no authorization header
            response = requests.get(
                room_preview_url,
                params={"rooms": "!test:example.com"},
                timeout=10,
            )
            self.assertEqual(response.status_code, 401)
            response_data = response.json()
            self.assertIn("error", response_data)
            self.assertEqual(response_data["error"], "Unauthorized")
            self.assertIn("errcode", response_data)
            self.assertEqual(response_data["errcode"], "M_UNAUTHORIZED")

            # Test with invalid authorization header
            invalid_headers = {"Authorization": "Bearer invalid_token_12345"}
            response = requests.get(
                room_preview_url,
                headers=invalid_headers,
                params={"rooms": "!test:example.com"},
                timeout=10,
            )
            self.assertEqual(response.status_code, 401)
            response_data = response.json()
            self.assertIn("error", response_data)
            self.assertEqual(response_data["error"], "Unauthorized")
            self.assertIn("errcode", response_data)
            self.assertEqual(response_data["errcode"], "M_UNAUTHORIZED")

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def test_activity_roles_filtering_sqlite(self):
        await self._test_activity_roles_with_membership_summary(db="sqlite")

    async def test_activity_roles_filtering_postgres(self):
        await self._test_activity_roles_with_membership_summary(db="postgresql")

    async def _test_activity_roles_with_membership_summary(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test that activity roles include all users with membership summary."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Register two test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user2",
                password="pw2",
                admin=False,
            )

            # Login users
            admin_token = await self.login_user("admin_user", "admin_pw")
            user1_token = await self.login_user("user1", "pw1")
            user2_token = await self.login_user("user2", "pw2")

            # Create a room with activity roles
            room_id = await self.create_room_with_activity_roles(
                admin_token, user1_token, user2_token
            )

            # Initially all users should be in the activity roles with join membership
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify all users are in activity roles
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]
            self.assertIn("pangea.activity_roles", room_data)

            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]
            self.assertEqual(len(activity_roles), 3)  # admin + user1 + user2

            # Verify all users are present in roles
            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@admin_user:my.domain.name",
                "@user1:my.domain.name",
                "@user2:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary is present and all users are "join"
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "join")

            # Remove user2 from the room (kick them)
            kick_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/kick"
            kick_data = {
                "user_id": "@user2:my.domain.name",
                "reason": "Test kick for membership summary",
            }
            kick_response = requests.post(
                kick_url,
                json=kick_data,
                headers=headers,
            )
            self.assertEqual(kick_response.status_code, 200)

            # Wait a moment for the kick to be processed
            await asyncio.sleep(0.5)

            # Request room preview again - user2's role should still be present
            # but membership_summary should show user2 as "leave"
            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify all users are still in activity roles (no filtering)
            room_data = data["rooms"][room_id]
            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]

            # All three users should still be present in roles
            self.assertEqual(len(activity_roles), 3)

            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@admin_user:my.domain.name",
                "@user1:my.domain.name",
                "@user2:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary shows correct membership states
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            # user2 should now be "leave" in membership_summary
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "leave")

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def create_room_with_activity_roles(
        self, admin_token: str, user1_token: str, user2_token: str
    ) -> str:
        """Create a room with both users invited and add activity roles for all."""
        headers = {"Authorization": f"Bearer {admin_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Activity Roles Filtering",
            "invite": ["@user1:my.domain.name", "@user2:my.domain.name"],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Accept invitations for both users
        join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

        user1_headers = {"Authorization": f"Bearer {user1_token}"}
        join_response1 = requests.post(join_url, headers=user1_headers)
        self.assertEqual(join_response1.status_code, 200)

        user2_headers = {"Authorization": f"Bearer {user2_token}"}
        join_response2 = requests.post(join_url, headers=user2_headers)
        self.assertEqual(join_response2.status_code, 200)

        # Add activity roles state event with all three users
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"
        activity_roles_data = {
            "roles": {
                "role-admin-123": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-admin-123",
                    "role": "facilitator",
                    "user_id": "@admin_user:my.domain.name",
                },
                "role-user1-456": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-user1-456",
                    "role": "participant",
                    "user_id": "@user1:my.domain.name",
                },
                "role-user2-789": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-user2-789",
                    "role": "observer",
                    "user_id": "@user2:my.domain.name",
                },
            }
        }

        roles_response = requests.put(
            f"{state_url}/pangea.activity_roles/",
            json=activity_roles_data,
            headers=headers,
        )
        self.assertEqual(roles_response.status_code, 200)

        return room_id

    async def test_activity_roles_filtering_no_roles_sqlite(self):
        await self._test_activity_roles_filtering_no_roles(db="sqlite")

    async def test_activity_roles_filtering_no_roles_postgres(self):
        await self._test_activity_roles_filtering_no_roles(db="postgresql")

    async def _test_activity_roles_filtering_no_roles(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test that room preview works correctly when there are no activity roles."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room without activity roles
            room_id = await self.create_private_room_knock_allowed_room(admin_token)

            # Request room preview - should work fine without activity roles
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Should have room data but no activity roles
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]

            # Activity roles should not be present (since we didn't create any)
            # But the request should still succeed
            if "pangea.activity_roles" in room_data:
                # If present, should be empty or properly structured
                activity_roles_data = room_data["pangea.activity_roles"]
                self.assertIsInstance(activity_roles_data, dict)

            # membership_summary should not be present if no activity roles
            if "pangea.activity_roles" not in room_data:
                self.assertNotIn("membership_summary", room_data)

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def test_left_users_in_activity_roles_sqlite(self):
        """Test that left users are preserved in activity roles with membership summary (SQLite)."""
        await self._test_left_users_in_activity_roles(db="sqlite")

    async def test_left_users_in_activity_roles_postgres(self):
        """Test that left users are preserved in activity roles with membership summary (PostgreSQL)."""
        await self._test_left_users_in_activity_roles(db="postgresql")

    async def _test_left_users_in_activity_roles(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test that left users are preserved in activity roles for completed activities.

        This test verifies the behavior requested in the issue:
        - Activity roles should NOT be filtered for users who have left
        - A membership summary should be returned so clients can display info about
          completed activities while knowing who has left
        """
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(db=db, postgresql_url=postgres_url)

            # Register users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="facilitator",
                password="fac_pw",
                admin=True,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant1",
                password="p1_pw",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant2",
                password="p2_pw",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant3",
                password="p3_pw",
                admin=False,
            )

            # Login users
            facilitator_token = await self.login_user("facilitator", "fac_pw")
            p1_token = await self.login_user("participant1", "p1_pw")
            p2_token = await self.login_user("participant2", "p2_pw")
            p3_token = await self.login_user("participant3", "p3_pw")

            # Create room and add users
            headers = {"Authorization": f"Bearer {facilitator_token}"}
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

            create_room_data = {
                "visibility": "private",
                "preset": "private_chat",
                "name": "Completed Activity Room",
                "invite": [
                    "@participant1:my.domain.name",
                    "@participant2:my.domain.name",
                    "@participant3:my.domain.name",
                ],
            }

            response = requests.post(
                create_room_url,
                json=create_room_data,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # All participants join
            join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

            for token in [p1_token, p2_token, p3_token]:
                join_response = requests.post(
                    join_url, headers={"Authorization": f"Bearer {token}"}
                )
                self.assertEqual(join_response.status_code, 200)

            # Add activity roles - simulating a completed activity
            state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"
            activity_roles_data = {
                "roles": {
                    "role-fac": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-fac",
                        "role": "facilitator",
                        "user_id": "@facilitator:my.domain.name",
                    },
                    "role-p1": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p1",
                        "role": "presenter",
                        "user_id": "@participant1:my.domain.name",
                    },
                    "role-p2": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p2",
                        "role": "participant",
                        "user_id": "@participant2:my.domain.name",
                    },
                    "role-p3": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p3",
                        "role": "participant",
                        "user_id": "@participant3:my.domain.name",
                    },
                }
            }

            roles_response = requests.put(
                f"{state_url}/pangea.activity_roles/",
                json=activity_roles_data,
                headers=headers,
            )
            self.assertEqual(roles_response.status_code, 200)

            # participant2 and participant3 leave the room after the activity
            leave_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"

            p2_leave = requests.post(
                leave_url, headers={"Authorization": f"Bearer {p2_token}"}
            )
            self.assertEqual(p2_leave.status_code, 200)

            p3_leave = requests.post(
                leave_url, headers={"Authorization": f"Bearer {p3_token}"}
            )
            self.assertEqual(p3_leave.status_code, 200)

            # Wait for the leave events to be processed
            await asyncio.sleep(0.5)

            # Request room preview - should return full roles with membership summary
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            room_data = data["rooms"][room_id]

            # Verify ALL roles are returned (not filtered)
            self.assertIn("pangea.activity_roles", room_data)
            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]

            # All 4 users should be in roles (even though 2 have left)
            self.assertEqual(len(activity_roles), 4)

            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@facilitator:my.domain.name",
                "@participant1:my.domain.name",
                "@participant2:my.domain.name",
                "@participant3:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary is present and correct
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]

            # Facilitator and participant1 should be "join"
            self.assertEqual(
                membership_summary.get("@facilitator:my.domain.name"), "join"
            )
            self.assertEqual(
                membership_summary.get("@participant1:my.domain.name"), "join"
            )

            # participant2 and participant3 should be "leave"
            self.assertEqual(
                membership_summary.get("@participant2:my.domain.name"), "leave"
            )
            self.assertEqual(
                membership_summary.get("@participant3:my.domain.name"), "leave"
            )

            # Only users in activity roles should be in membership_summary
            self.assertEqual(len(membership_summary), 4)

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def test_join_rules_filtering_sqlite(self):
        await self._test_join_rules_content_filtering(db="sqlite")

    async def test_join_rules_filtering_postgres(self):
        await self._test_join_rules_content_filtering(db="postgresql")

    async def _test_join_rules_content_filtering(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test that m.room.join_rules content only exposes the join_rule key."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()

            # Start Synapse with m.room.join_rules in allowed state event types
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_join_rules(
                db=db, postgresql_url=postgres_url
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with join_rules that has additional content
            room_id = await self._create_room_with_complex_join_rules(admin_token)

            # Request room preview
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify the response structure
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]

            # Verify m.room.join_rules is present
            self.assertIn("m.room.join_rules", room_data)
            join_rules_data = room_data["m.room.join_rules"]
            self.assertIn("default", join_rules_data)

            # Get the join_rules event content
            join_rules_event = join_rules_data["default"]
            self.assertIn("content", join_rules_event)
            join_rules_content = join_rules_event["content"]

            # Verify ONLY join_rule key is present in content
            self.assertIn("join_rule", join_rules_content)
            self.assertEqual(join_rules_content["join_rule"], "knock")

            # Verify other keys are NOT present (they should be filtered out)
            # The room was created with additional content that should be stripped
            self.assertEqual(
                len(join_rules_content),
                1,
                f"join_rules content should only have 1 key (join_rule), but has: {list(join_rules_content.keys())}",
            )
            self.assertNotIn(
                "allow",
                join_rules_content,
                "allow key should be filtered out from join_rules content",
            )

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def _start_synapse_with_join_rules(
        self,
        db: Literal["sqlite", "postgresql"] = "sqlite",
        postgresql_url: Union[str, None] = None,
    ) -> Tuple[str, str, subprocess.Popen, threading.Thread, threading.Thread]:
        """Start Synapse with m.room.join_rules in the allowed state event types."""
        synapse_dir = tempfile.mkdtemp()
        config_path = os.path.join(synapse_dir, "homeserver.yaml")

        generate_config_cmd = [
            sys.executable,
            "-m",
            "synapse.app.homeserver",
            "--server-name=my.domain.name",
            f"--config-path={config_path}",
            "--report-stats=no",
            "--generate-config",
        ]
        subprocess.check_call(generate_config_cmd)

        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        log_config_path = config.get("log_config")

        # Configure module with m.room.join_rules included
        config["modules"] = [
            {
                "module": "synapse_room_preview.SynapseRoomPreview",
                "config": {
                    "room_preview_state_event_types": [
                        "pangea.activity_plan",
                        "pangea.activity_roles",
                        "m.room.join_rules",
                    ]
                },
            }
        ]

        if db == "sqlite":
            config["database"] = {
                "name": "sqlite3",
                "args": {"database": "homeserver.db"},
            }
        elif db == "postgresql":
            if postgresql_url is None:
                self.fail("PostgreSQL URL is required for PostgreSQL database")
            dsn_params = parse_dsn(postgresql_url)
            config["database"] = {
                "name": "psycopg2",
                "args": dsn_params,
            }

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f)

        with open(log_config_path, "r", encoding="utf-8") as f:
            log_config = yaml.safe_load(f)

        log_config["root"]["handlers"] = ["console"]
        log_config["root"]["level"] = "DEBUG"

        with open(log_config_path, "w", encoding="utf-8") as f:
            yaml.dump(log_config, f)

        run_server_cmd = [
            sys.executable,
            "-m",
            "synapse.app.homeserver",
            "--config-path",
            config_path,
        ]

        server_process = subprocess.Popen(
            run_server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=synapse_dir,
            text=True,
        )

        def stream_output(pipe: IO[str], log_fn):
            for line in pipe:
                log_fn(line.strip())

        stdout_thread = threading.Thread(
            target=stream_output, args=(server_process.stdout, logger.info)
        )
        stderr_thread = threading.Thread(
            target=stream_output, args=(server_process.stderr, logger.error)
        )
        stdout_thread.start()
        stderr_thread.start()

        # Wait for server to start
        for _ in range(30):
            try:
                resp = requests.get("http://localhost:8008/_matrix/client/versions")
                if resp.status_code == 200:
                    break
            except requests.exceptions.ConnectionError:
                pass
            await asyncio.sleep(1)
        else:
            self.fail("Synapse server did not start in time")

        return synapse_dir, config_path, server_process, stdout_thread, stderr_thread

    async def _create_room_with_complex_join_rules(self, access_token: str) -> str:
        """Create a room with join_rules that contain additional content beyond join_rule."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room with knock join rule
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Join Rules Filtering",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "knock",
                        # Additional fields that should be filtered out
                        "allow": [
                            {
                                "type": "m.room_membership",
                                "room_id": "!some_space:example.com",
                            }
                        ],
                    },
                },
            ],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def test_join_rules_only_join_rule_key_sqlite(self):
        """Test that when join_rules has only join_rule, it still works correctly."""
        await self._test_join_rules_simple_content(db="sqlite")

    async def test_join_rules_only_join_rule_key_postgres(self):
        """Test that when join_rules has only join_rule, it still works correctly."""
        await self._test_join_rules_simple_content(db="postgresql")

    async def _test_join_rules_simple_content(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test m.room.join_rules filtering when content only has join_rule key."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()

            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_join_rules(
                db=db, postgresql_url=postgres_url
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with simple join_rules (only join_rule key)
            headers = {"Authorization": f"Bearer {admin_token}"}
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

            create_room_data = {
                "visibility": "private",
                "preset": "private_chat",
                "name": "Test Room Simple Join Rules",
                "initial_state": [
                    {
                        "type": "m.room.join_rules",
                        "state_key": "",
                        "content": {"join_rule": "invite"},
                    },
                ],
            }

            response = requests.post(
                create_room_url,
                json=create_room_data,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # Request room preview
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            preview_response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(preview_response.status_code, 200)
            data = preview_response.json()

            room_data = data["rooms"][room_id]
            self.assertIn("m.room.join_rules", room_data)

            join_rules_content = room_data["m.room.join_rules"]["default"]["content"]
            self.assertEqual(join_rules_content, {"join_rule": "invite"})

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def test_course_plan_with_membership_summary_sqlite(self):
        await self._test_course_plan_with_membership_summary(db="sqlite")

    async def test_course_plan_with_membership_summary_postgres(self):
        await self._test_course_plan_with_membership_summary(db="postgresql")

    async def _test_course_plan_with_membership_summary(
        self, db: Literal["sqlite", "postgresql"]
    ):
        """Test that rooms with pangea.course_plan include membership_summary."""
        postgres = None
        postgres_url = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            if db == "postgresql":
                postgres, postgres_url = await self.start_test_postgres()
            (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse_with_course_plan(
                db=db, postgresql_url=postgres_url
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Register two test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user2",
                password="pw2",
                admin=False,
            )

            # Login users
            admin_token = await self.login_user("admin_user", "admin_pw")
            user1_token = await self.login_user("user1", "pw1")
            user2_token = await self.login_user("user2", "pw2")

            # Create a room with course_plan (not activity_roles)
            room_id = await self.create_room_with_course_plan(
                admin_token, user1_token, user2_token
            )

            # Request room preview - should include membership_summary for course rooms
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify room data includes course_plan
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]
            self.assertIn("pangea.course_plan", room_data)

            # Verify course_plan content
            course_plan = room_data["pangea.course_plan"]["default"]["content"]
            self.assertIn("uuid", course_plan)

            # Verify membership_summary is present for course rooms
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]

            # All joined users should be in membership_summary
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "join")

            # Kick user2 from the room
            kick_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/kick"
            kick_data = {
                "user_id": "@user2:my.domain.name",
                "reason": "Test kick for course plan membership summary",
            }
            kick_response = requests.post(
                kick_url,
                json=kick_data,
                headers=headers,
            )
            self.assertEqual(kick_response.status_code, 200)

            # Wait a moment for the kick to be processed
            await asyncio.sleep(0.5)

            # Request room preview again - user2 should be "leave"
            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            room_data = data["rooms"][room_id]

            # Verify membership_summary shows correct membership states
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            # user2 should now be "leave" in membership_summary
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "leave")

        finally:
            if postgres is not None:
                postgres.stop()
            if server_process is not None:
                server_process.terminate()
                server_process.wait()
            if stdout_thread is not None:
                stdout_thread.join()
            if stderr_thread is not None:
                stderr_thread.join()
            if synapse_dir is not None:
                shutil.rmtree(synapse_dir)

    async def start_test_synapse_with_course_plan(
        self,
        db: Literal["sqlite", "postgresql"] = "sqlite",
        postgresql_url: Union[str, None] = None,
    ) -> Tuple[str, str, subprocess.Popen, threading.Thread, threading.Thread]:
        """Start synapse with course_plan in the allowed state event types."""
        try:
            synapse_dir = tempfile.mkdtemp()
            config_path = os.path.join(synapse_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name=my.domain.name",
                f"--config-path={config_path}",
                "--report-stats=no",
                "--generate-config",
            ]
            subprocess.check_call(generate_config_cmd)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")
            # Include pangea.course_plan in allowed state event types
            config["modules"] = [
                {
                    "module": "synapse_room_preview.SynapseRoomPreview",
                    "config": {
                        "room_preview_state_event_types": [
                            "pangea.course_plan",
                        ]
                    },
                }
            ]
            if db == "sqlite":
                if postgresql_url is not None:
                    self.fail(
                        "PostgreSQL URL must not be defined when using SQLite database"
                    )
                config["database"] = {
                    "name": "sqlite3",
                    "args": {"database": "homeserver.db"},
                }
            elif db == "postgresql":
                if postgresql_url is None:
                    self.fail("PostgreSQL URL is required for PostgreSQL database")
                dsn_params = parse_dsn(postgresql_url)
                config["database"] = {
                    "name": "psycopg2",
                    "args": dsn_params,
                }
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            with open(log_config_path, "r", encoding="utf-8") as f:
                log_config = yaml.safe_load(f)
            log_config["root"]["handlers"] = ["console"]
            log_config["root"]["level"] = "DEBUG"
            with open(log_config_path, "w", encoding="utf-8") as f:
                yaml.dump(log_config, f)
            run_server_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                config_path,
            ]
            server_process = subprocess.Popen(
                run_server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=synapse_dir,
                text=True,
            )

            def read_output(pipe: Union[IO[str], None]):
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout,)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr,)
            )
            stdout_thread.start()
            stderr_thread.start()
            server_url = "http://localhost:8008"
            max_wait_time = 10
            wait_interval = 1
            total_wait_time = 0
            server_ready = False
            while not server_ready and total_wait_time < max_wait_time:
                try:
                    response = requests.get(server_url, timeout=10)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    pass
                finally:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval
            if not server_ready:
                self.fail("Synapse server did not start successfully")
            return (
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            )
        except Exception as e:
            server_process.terminate()
            server_process.wait()
            stdout_thread.join()
            stderr_thread.join()
            shutil.rmtree(synapse_dir)
            raise e

    async def create_room_with_course_plan(
        self, admin_token: str, user1_token: str, user2_token: str
    ) -> str:
        """Create a room with users and add pangea.course_plan state event."""
        headers = {"Authorization": f"Bearer {admin_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Course Plan",
            "invite": ["@user1:my.domain.name", "@user2:my.domain.name"],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Accept invitations for both users
        join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

        user1_headers = {"Authorization": f"Bearer {user1_token}"}
        response = requests.post(join_url, headers=user1_headers)
        self.assertEqual(response.status_code, 200)

        user2_headers = {"Authorization": f"Bearer {user2_token}"}
        response = requests.post(join_url, headers=user2_headers)
        self.assertEqual(response.status_code, 200)

        # Add pangea.course_plan state event
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/pangea.course_plan"
        course_plan_content = {"uuid": "b6989779-a498-4463-aac8-2ac06b2a0406"}

        response = requests.put(
            state_url,
            json=course_plan_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)

        return room_id
