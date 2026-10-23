from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from copilot_backend import complete  # noqa: E402


@dataclass
class Message:
    content: str


@dataclass
class Error:
    message: str


@dataclass
class Idle:
    mode: str = "interactive"


class RpcError(Exception):
    pass


class CleanupError(Exception):
    pass


class CopilotBackendTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.session_id = "lineage-test-session"
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.__aexit__ = AsyncMock(return_value=False)
        self.session.send = AsyncMock(side_effect=self.emit_response)
        self.session.on.side_effect = self.subscribe
        self.unsubscribe = Mock()
        self.client = MagicMock()
        self.client.start = AsyncMock()
        self.client.stop = AsyncMock()
        self.client.force_stop = AsyncMock()
        self.client.delete_session = AsyncMock()
        self.client.get_auth_status = AsyncMock(
            return_value=SimpleNamespace(isAuthenticated=True)
        )
        self.client.list_models = AsyncMock(
            return_value=[SimpleNamespace(id="sonnet-test")]
        )
        self.client.create_session = AsyncMock(return_value=self.session)
        self.factory = Mock(return_value=self.client)
        self.events = [
            SimpleNamespace(data=Message('{"rows": []}'), agent_id=None),
            SimpleNamespace(data=Idle(), agent_id=None),
        ]
        self.modules = patch.dict(sys.modules, {
            "copilot": SimpleNamespace(CopilotClient=self.factory, StopError=CleanupError),
            "copilot._jsonrpc": SimpleNamespace(
                JsonRpcError=RpcError, ProcessExitedError=ConnectionError,
            ),
            "copilot.session_events": SimpleNamespace(
                AssistantMessageData=Message,
                SessionErrorData=Error,
                SessionEvent=SimpleNamespace,
                SessionIdleData=Idle,
                SessionMode=SimpleNamespace(AUTOPILOT="autopilot"),
            ),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def subscribe(self, handler):
        self.handler = handler
        return self.unsubscribe

    async def emit_response(self, prompt):
        for event in self.events:
            self.handler(event)

    def request(self, timeout=1):
        return complete(
            model="sonnet-test",
            system_prompt="system instructions",
            user_prompt="pipeline content",
            timeout_seconds=timeout,
        )

    def test_request_is_isolated_tool_free_and_cleaned_up(self):
        self.assertEqual('{"rows": []}', self.request())
        options = self.factory.call_args.kwargs
        self.assertEqual("copilot-cli", options["mode"])
        self.assertNotIn("base_directory", options)
        self.assertFalse(Path(options["working_directory"]).exists())
        self.client.create_session.assert_awaited_once_with(
            model="sonnet-test",
            system_message={"mode": "replace", "content": "system instructions"},
            available_tools=[],
            tools=[],
            mcp_servers={},
            enable_config_discovery=False,
            skip_custom_instructions=True,
            enable_skills=False,
            enable_file_hooks=False,
            enable_host_git_operations=False,
            enable_session_store=False,
            enable_session_telemetry=False,
            skip_embedding_retrieval=True,
            memory={"enabled": False},
            infinite_sessions={"enabled": False},
        )
        self.session.send.assert_awaited_once_with("pipeline content")
        self.session.__aexit__.assert_awaited_once()
        self.client.stop.assert_awaited_once()
        self.client.delete_session.assert_awaited_once_with("lineage-test-session")
        self.unsubscribe.assert_called_once()

    def test_unauthenticated_account_is_an_actionable_failure(self):
        self.client.get_auth_status.return_value.isAuthenticated = False
        with self.assertRaisesRegex(RuntimeError, "/login"):
            self.request()
        self.client.create_session.assert_not_awaited()
        self.client.delete_session.assert_not_awaited()
        self.client.stop.assert_awaited_once()

    def test_unavailable_model_is_not_silently_replaced(self):
        self.client.list_models.return_value = []
        with self.assertRaisesRegex(ValueError, "not available"):
            self.request()
        self.client.create_session.assert_not_awaited()
        self.client.stop.assert_awaited_once()

    def test_rpc_error_is_reported_and_runtime_stopped(self):
        self.client.create_session.side_effect = RpcError("access denied")
        with self.assertRaisesRegex(RuntimeError, "access denied"):
            self.request()
        self.client.stop.assert_awaited_once()

    def test_session_error_is_not_returned_as_success(self):
        self.events = [SimpleNamespace(data=Error("quota exceeded"), agent_id=None)]
        with self.assertRaisesRegex(RuntimeError, "quota exceeded"):
            self.request()
        self.client.stop.assert_awaited_once()
        self.unsubscribe.assert_called_once()

    def test_runtime_exit_is_reported(self):
        self.session.send.side_effect = ConnectionError("runtime exited")
        with self.assertRaisesRegex(RuntimeError, "runtime exited"):
            self.request()
        self.client.stop.assert_awaited_once()

    def test_session_deletion_failure_still_stops_runtime(self):
        self.client.delete_session.side_effect = RpcError("could not delete session")
        with self.assertRaisesRegex(RuntimeError, "could not delete session"):
            self.request()
        self.client.stop.assert_awaited_once()
        self.client.force_stop.assert_awaited_once()

    def test_cleanup_failure_forces_stop_and_is_reported(self):
        self.client.stop.side_effect = ExceptionGroup(
            "cleanup errors", [CleanupError("could not detach")]
        )
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            self.request()
        self.client.force_stop.assert_awaited_once()

    def test_empty_response_is_rejected(self):
        self.events = [SimpleNamespace(data=Idle(), agent_id=None)]
        with self.assertRaisesRegex(ValueError, "content is empty"):
            self.request()

    def test_subagent_events_cannot_supply_reply(self):
        self.events.insert(0, SimpleNamespace(data=Message("wrong"), agent_id="child"))
        self.assertEqual('{"rows": []}', self.request())

    def test_timeout_includes_startup_and_stops_runtime(self):
        async def delayed_start():
            await asyncio.sleep(10)

        self.client.start.side_effect = delayed_start
        with self.assertRaisesRegex(RuntimeError, "timed out after 0.01 seconds"):
            self.request(timeout=0.01)
        self.client.stop.assert_awaited_once()

    def test_timeout_during_response_unsubscribes_and_stops_runtime(self):
        self.events = []
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.request(timeout=0.01)
        self.session.__aexit__.assert_awaited_once()
        self.client.delete_session.assert_awaited_once_with("lineage-test-session")
        self.client.stop.assert_awaited_once()
        self.unsubscribe.assert_called_once()

    def test_missing_sdk_is_an_actionable_error(self):
        with patch.dict(sys.modules, {"copilot": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-copilot.txt"):
                self.request()

    def test_running_event_loop_is_explicitly_rejected(self):
        async def run():
            with self.assertRaisesRegex(RuntimeError, "asyncio.to_thread"):
                self.request()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
