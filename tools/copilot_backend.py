"""Optional, tool-free GitHub Copilot SDK transport."""

from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory


async def _complete(
    model: str, system_prompt: str, user_prompt: str, directory: str
) -> str:
    try:
        from copilot import CopilotClient, StopError
        from copilot._jsonrpc import JsonRpcError, ProcessExitedError
        from copilot.session_events import (
            AssistantMessageData,
            SessionErrorData,
            SessionEvent,
            SessionIdleData,
            SessionMode,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Copilot requires Python 3.11+ and the optional SDK. "
            "Run: python -m pip install -r requirements-copilot.txt"
        ) from exc

    client = CopilotClient(
        mode="copilot-cli",
        working_directory=directory,
        log_level="error",
    )
    session_id: str | None = None
    try:
        await client.start()
        if not (await client.get_auth_status()).isAuthenticated:
            raise RuntimeError(
                "Copilot is not authenticated. Install GitHub Copilot CLI, run "
                "copilot and sign in with /login, then retry. "
                "VS Code sign-in alone is not sufficient."
            )
        models = await client.list_models()
        if model not in {item.id for item in models}:
            raise ValueError(
                f"Copilot model '{model}' is not available to this account. "
                "Select an available model with /model in the Copilot CLI and set "
                "LINEAGE_COPILOT_MODEL to its exact ID."
            )
        session = await client.create_session(
            model=model,
            system_message={"mode": "replace", "content": system_prompt},
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
        session_id = session.session_id
        async with session:
            completed: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            last_content: str | None = None

            def on_event(event: SessionEvent) -> None:
                nonlocal last_content
                if event.agent_id or completed.done():
                    return
                match event.data:
                    case AssistantMessageData() as data:
                        last_content = data.content
                    case SessionErrorData() as data:
                        completed.set_exception(
                            RuntimeError(f"Copilot session error: {data.message}")
                        )
                    case SessionIdleData() as data if data.mode != SessionMode.AUTOPILOT:
                        if not last_content or not last_content.strip():
                            completed.set_exception(ValueError("Copilot response content is empty"))
                        else:
                            completed.set_result(last_content.strip())

            unsubscribe = session.on(on_event)
            try:
                await session.send(user_prompt)
                return await completed
            finally:
                unsubscribe()
    except (JsonRpcError, ProcessExitedError) as exc:
        raise RuntimeError(f"Copilot SDK request failed: {exc}") from exc
    finally:
        async def cleanup() -> None:
            try:
                if session_id is not None:
                    await client.delete_session(session_id)
            finally:
                await client.stop()

        try:
            try:
                await asyncio.wait_for(cleanup(), timeout=10)
            except* StopError as exc:
                raise RuntimeError(f"Copilot SDK cleanup failed: {exc}") from exc
        except (JsonRpcError, ProcessExitedError) as exc:
            await client.force_stop()
            raise RuntimeError(f"Copilot SDK cleanup failed: {exc}") from exc
        except (RuntimeError, TimeoutError):
            await client.force_stop()
            raise


def complete(
    *, model: str, system_prompt: str, user_prompt: str, timeout_seconds: float
) -> str:
    """Run one isolated request; never inspect a workspace or permit agent tools."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "Copilot lineage uses a synchronous entry point. From async code, "
            "run lineage generation with asyncio.to_thread."
        )
    with TemporaryDirectory(prefix="lineage-copilot-") as directory:
        try:
            return asyncio.run(asyncio.wait_for(
                _complete(model, system_prompt, user_prompt, directory),
                timeout=timeout_seconds,
            ))
        except TimeoutError as exc:
            raise RuntimeError(
                f"Copilot request timed out after {timeout_seconds:g} seconds "
                f"(model: {model})."
            ) from exc
