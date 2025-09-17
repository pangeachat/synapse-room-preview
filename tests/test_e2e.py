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
                    "config": {},
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
            params = {"room_id": room_id}

            response = requests.get(
                room_preview_url,
                headers=headers,
                params=params,
                timeout=10,
            )

            # Expect a 200 response from the room_preview endpoint
            self.assertEqual(response.status_code, 200)

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
