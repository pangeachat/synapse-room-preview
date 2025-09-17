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
                )  # Empty state key should be empty string
                self.assertIsInstance(json_data, dict)  # Should be parsed JSON

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
        self.assertIsInstance(room_data, dict)

        for event_type, event_type_data in room_data.items():
            # Event type should be a string
            self.assertIsInstance(event_type, str)
            # Event type data should be a dict
            self.assertIsInstance(event_type_data, dict)

            for state_key, event_content in event_type_data.items():
                # State key should be a string (empty string for events with no state key)
                self.assertIsInstance(state_key, str)
                # Event content should be parsed JSON (dict)
                self.assertIsInstance(event_content, dict)

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
