"""Tests for chicane.mcp_server — MCP tool functions."""

from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from chicane.config import Config
from chicane import mcp_server as mcp_mod


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset module-level singletons between tests."""
    mcp_mod._config = None
    mcp_mod._client = None
    yield
    mcp_mod._config = None
    mcp_mod._client = None


@pytest.fixture
def config():
    return Config(
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        base_directory=Path("/projects"),
        channel_dirs={"my-channel": "my-project"},
    )


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True})
    return client


class TestChicaneHandoff:
    @pytest.mark.asyncio
    async def test_successful_handoff(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C123"),
            patch("chicane.mcp_server.resolve_session_id", return_value="sess-abc"),
            patch("chicane.mcp_server.Path") as MockPath,
        ):
            MockPath.cwd.return_value = Path("/projects/my-project")

            result = await mcp_mod.chicane_handoff(
                summary="Working on auth flow",
            )

        assert "Handoff posted to #my-channel" in result
        mock_client.chat_postMessage.assert_awaited_once()
        call_kwargs = mock_client.chat_postMessage.call_args
        assert "sess-abc" in call_kwargs.kwargs["text"]
        assert "Working on auth flow" in call_kwargs.kwargs["text"]

    @pytest.mark.asyncio
    async def test_handoff_with_questions(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C123"),
            patch("chicane.mcp_server.resolve_session_id", return_value="sess-abc"),
            patch("chicane.mcp_server.Path") as MockPath,
        ):
            MockPath.cwd.return_value = Path("/projects/my-project")

            result = await mcp_mod.chicane_handoff(
                summary="Working on auth",
                questions="1. JWT or sessions?",
            )

        text = mock_client.chat_postMessage.call_args.kwargs["text"]
        assert "JWT or sessions?" in text

    @pytest.mark.asyncio
    async def test_handoff_with_explicit_channel_and_session(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C456"),
            patch("chicane.mcp_server.resolve_session_id", return_value="explicit-id"),
        ):
            result = await mcp_mod.chicane_handoff(
                summary="Explicit test",
                session_id="explicit-id",
                channel="other-channel",
            )

        assert "Handoff posted to #other-channel" in result

    @pytest.mark.asyncio
    async def test_handoff_session_id_error(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch("chicane.mcp_server.resolve_session_id", side_effect=ValueError("No history")),
        ):
            result = await mcp_mod.chicane_handoff(summary="test")

        assert "Error: No history" in result

    @pytest.mark.asyncio
    async def test_handoff_channel_resolve_error(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_session_id", return_value="sess-1"),
            patch("chicane.mcp_server.resolve_channel_id", return_value=None),
            patch("chicane.mcp_server.Path") as MockPath,
        ):
            MockPath.cwd.return_value = Path("/unknown/dir")

            result = await mcp_mod.chicane_handoff(summary="test")

        assert "Error:" in result

    @pytest.mark.asyncio
    async def test_handoff_tells_user_to_close_session(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C123"),
            patch("chicane.mcp_server.resolve_session_id", return_value="sess-1"),
            patch("chicane.mcp_server.Path") as MockPath,
        ):
            MockPath.cwd.return_value = Path("/projects/my-project")

            result = await mcp_mod.chicane_handoff(summary="test")

        assert "close this session" in result


class TestChicaneSendMessage:
    @pytest.mark.asyncio
    async def test_successful_send(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C123"),
            patch("chicane.mcp_server.Path") as MockPath,
        ):
            MockPath.cwd.return_value = Path("/projects/my-project")

            result = await mcp_mod.chicane_send_message(text="Hello team!")

        assert "Message sent to #my-channel" in result
        mock_client.chat_postMessage.assert_awaited_once_with(
            channel="C123", text="Hello team!"
        )

    @pytest.mark.asyncio
    async def test_send_with_explicit_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C789"),
        ):
            result = await mcp_mod.chicane_send_message(
                text="Update", channel="alerts"
            )

        assert "Message sent to #alerts" in result

    @pytest.mark.asyncio
    async def test_send_channel_not_found(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value=None),
        ):
            result = await mcp_mod.chicane_send_message(
                text="test", channel="nonexistent"
            )

        assert "Error:" in result
        assert "nonexistent" in result


class TestResolveChannel:
    @pytest.mark.asyncio
    async def test_resolves_from_cwd(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C123"),
        ):
            name, cid = await mcp_mod._resolve_channel(
                None, cwd=Path("/projects/my-project")
            )

        assert name == "my-channel"
        assert cid == "C123"

    @pytest.mark.asyncio
    async def test_uses_explicit_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C999"),
        ):
            name, cid = await mcp_mod._resolve_channel("explicit")

        assert name == "explicit"
        assert cid == "C999"

    @pytest.mark.asyncio
    async def test_raises_when_no_mapping(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
        ):
            with pytest.raises(ValueError, match="Could not resolve"):
                await mcp_mod._resolve_channel(None, cwd=Path("/unknown"))

    @pytest.mark.asyncio
    async def test_raises_when_channel_not_in_slack(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value=None),
        ):
            with pytest.raises(ValueError, match="not found in Slack"):
                await mcp_mod._resolve_channel("ghost-channel")


class TestMainConfigValidation:
    def test_exits_when_config_missing(self):
        with (
            patch(
                "chicane.mcp_server.Config.from_env",
                side_effect=ValueError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set."),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            mcp_mod.main()

    def test_starts_when_config_valid(self, config):
        with (
            patch("chicane.mcp_server.Config.from_env", return_value=config),
            patch.object(mcp_mod.mcp, "run") as mock_run,
        ):
            mcp_mod.main()
        mock_run.assert_called_once_with(transport="stdio")


class TestLazyInit:
    def test_get_config_caches(self, config):
        with patch("chicane.mcp_server.Config.from_env", return_value=config) as mock:
            c1 = mcp_mod._get_config()
            c2 = mcp_mod._get_config()
        assert c1 is c2
        mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_client_caches(self, config):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch("slack_sdk.web.async_client.AsyncWebClient") as MockClient,
        ):
            c1 = await mcp_mod._get_client()
            c2 = await mcp_mod._get_client()
        assert c1 is c2
        MockClient.assert_called_once_with(token="xoxb-test")
