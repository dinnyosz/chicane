"""Tests for chicane.mcp_server — MCP tool functions."""

import json
import re
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
    mcp_mod._SKILL_TEMPLATE = None
    yield
    mcp_mod._config = None
    mcp_mod._client = None
    mcp_mod._SKILL_TEMPLATE = None


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
        text = call_kwargs.kwargs["text"]
        # Full session_id should NOT be in text — only a funky alias
        assert "sess-abc" not in text
        assert "_(session:" in text
        # Alias format: 2+ hyphenated words (adjective-noun)
        assert re.search(r"\(session: [a-z]+(?:-[a-z]+)+\)", text)
        assert "Working on auth flow" in text

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
                channel="my-channel",
            )

        assert "Handoff posted to #my-channel" in result

    @pytest.mark.asyncio
    async def test_handoff_rejects_unmapped_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch("chicane.mcp_server.resolve_session_id", return_value="sess-1"),
        ):
            result = await mcp_mod.chicane_handoff(
                summary="test",
                channel="random-channel",
            )

        assert "Error:" in result
        assert "not in CHANNEL_DIRS" in result

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

        assert "pick it up" in result


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
    async def test_send_with_explicit_mapped_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C789"),
        ):
            result = await mcp_mod.chicane_send_message(
                text="Update", channel="my-channel"
            )

        assert "Message sent to #my-channel" in result

    @pytest.mark.asyncio
    async def test_send_rejects_unmapped_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
        ):
            result = await mcp_mod.chicane_send_message(
                text="test", channel="random-channel"
            )

        assert "Error:" in result
        assert "not in CHANNEL_DIRS" in result

    @pytest.mark.asyncio
    async def test_send_channel_not_found_in_slack(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value=None),
        ):
            result = await mcp_mod.chicane_send_message(
                text="test", channel="my-channel"
            )

        assert "Error:" in result
        assert "not found in Slack" in result


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
    async def test_uses_explicit_mapped_channel(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C999"),
        ):
            name, cid = await mcp_mod._resolve_channel("my-channel")

        assert name == "my-channel"
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
    async def test_raises_when_channel_not_in_channel_dirs(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
        ):
            with pytest.raises(ValueError, match="not in CHANNEL_DIRS"):
                await mcp_mod._resolve_channel("ghost-channel")

    @pytest.mark.asyncio
    async def test_raises_when_mapped_channel_not_in_slack(self, config, mock_client):
        with (
            patch.object(mcp_mod, "_get_config", return_value=config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value=None),
        ):
            with pytest.raises(ValueError, match="not found in Slack"):
                await mcp_mod._resolve_channel("my-channel")

    @pytest.mark.asyncio
    async def test_allows_any_channel_when_no_channel_dirs(self, mock_client):
        """When CHANNEL_DIRS is not configured, any explicit channel is allowed."""
        no_dirs_config = Config(
            slack_bot_token="xoxb-test",
            slack_app_token="xapp-test",
        )
        with (
            patch.object(mcp_mod, "_get_config", return_value=no_dirs_config),
            patch.object(mcp_mod, "_get_client", return_value=mock_client),
            patch("chicane.mcp_server.resolve_channel_id", return_value="C999"),
        ):
            name, cid = await mcp_mod._resolve_channel("any-channel")

        assert name == "any-channel"
        assert cid == "C999"


class TestChicaneInit:
    @pytest.mark.asyncio
    async def test_global_scope(self, tmp_path):
        with patch("chicane.mcp_server.Path") as MockPath:
            MockPath.__truediv__ = Path.__truediv__
            mcp_mod._SKILL_TEMPLATE = "skill content here"

            home = tmp_path / "home"
            MockPath.home.return_value = home

            result = await mcp_mod.chicane_init(
                scope="global",
                add_allowed_tools=False,
                mcp_server_name="chicane-dev",
            )

        target = home / ".claude" / "skills" / "chicane" / "SKILL.md"
        assert target.exists()
        assert target.read_text() == "skill content here"
        assert "Installed" in result

    @pytest.mark.asyncio
    async def test_project_scope(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "project skill"

        result = await mcp_mod.chicane_init(
            scope="project",
            project_root=str(tmp_path),
            add_allowed_tools=False,
            mcp_server_name="chicane-dev",
        )

        target = tmp_path / ".claude" / "skills" / "chicane" / "SKILL.md"
        assert target.exists()
        assert target.read_text() == "project skill"
        assert "Installed" in result

    @pytest.mark.asyncio
    async def test_project_scope_requires_root(self):
        result = await mcp_mod.chicane_init(
            scope="project",
            add_allowed_tools=False,
            mcp_server_name="chicane-dev",
        )
        assert "Error:" in result
        assert "project_root" in result

    @pytest.mark.asyncio
    async def test_invalid_scope(self):
        result = await mcp_mod.chicane_init(
            scope="banana",
            add_allowed_tools=False,
            mcp_server_name="chicane-dev",
        )
        assert "Error:" in result
        assert "banana" in result

    @pytest.mark.asyncio
    async def test_reads_real_template(self):
        """Ensure _get_skill_content reads the bundled skill.md."""
        mcp_mod._SKILL_TEMPLATE = None
        content = mcp_mod._get_skill_content()
        assert "chicane_handoff" in content
        assert "{{CHICANE_PATH}}" not in content

    @pytest.mark.asyncio
    async def test_add_allowed_tools_creates_settings(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "skill"

        result = await mcp_mod.chicane_init(
            scope="project",
            project_root=str(tmp_path),
            add_allowed_tools=True,
            mcp_server_name="chicane-dev",
        )

        settings = tmp_path / ".claude" / "settings.local.json"
        assert settings.exists()
        data = json.loads(settings.read_text())
        allow = data["permissions"]["allow"]
        assert "mcp__chicane-dev__chicane_handoff" in allow
        assert "mcp__chicane-dev__chicane_send_message" in allow
        assert "mcp__chicane-dev__chicane_init" in allow
        assert "Added 3 tool(s)" in result
        assert "Restart Claude Code" in result

    @pytest.mark.asyncio
    async def test_add_allowed_tools_merges_existing(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "skill"

        # Pre-populate settings with one existing tool
        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {
                "allow": ["Bash(git *)", "mcp__chicane-dev__chicane_handoff"]
            }
        }))

        result = await mcp_mod.chicane_init(
            scope="project",
            project_root=str(tmp_path),
            add_allowed_tools=True,
            mcp_server_name="chicane-dev",
        )

        data = json.loads(settings.read_text())
        allow = data["permissions"]["allow"]
        # Existing entries preserved
        assert "Bash(git *)" in allow
        # Duplicate not added
        assert allow.count("mcp__chicane-dev__chicane_handoff") == 1
        # New ones added
        assert "mcp__chicane-dev__chicane_send_message" in allow
        assert "Added 2 tool(s)" in result
        assert "Restart Claude Code" in result

    @pytest.mark.asyncio
    async def test_add_allowed_tools_all_exist(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "skill"

        settings = tmp_path / ".claude" / "settings.local.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "permissions": {
                "allow": [
                    "mcp__chicane-dev__chicane_handoff",
                    "mcp__chicane-dev__chicane_send_message",
                    "mcp__chicane-dev__chicane_init",
                ]
            }
        }))

        result = await mcp_mod.chicane_init(
            scope="project",
            project_root=str(tmp_path),
            add_allowed_tools=True,
            mcp_server_name="chicane-dev",
        )

        assert "already in" in result

    @pytest.mark.asyncio
    async def test_add_allowed_tools_custom_server_name(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "skill"

        result = await mcp_mod.chicane_init(
            scope="project",
            project_root=str(tmp_path),
            add_allowed_tools=True,
            mcp_server_name="my-chicane",
        )

        settings = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text())
        allow = data["permissions"]["allow"]
        assert "mcp__my-chicane__chicane_handoff" in allow

    @pytest.mark.asyncio
    async def test_allowed_tools_global_scope(self, tmp_path):
        mcp_mod._SKILL_TEMPLATE = "skill"

        with patch("chicane.mcp_server.Path") as MockPath:
            MockPath.__truediv__ = Path.__truediv__
            MockPath.home.return_value = tmp_path / "home"

            result = await mcp_mod.chicane_init(
                scope="global",
                add_allowed_tools=True,
                mcp_server_name="chicane-dev",
            )

        settings = tmp_path / "home" / ".claude" / "settings.local.json"
        assert settings.exists()
        assert "Added 3 tool(s)" in result
        assert "Restart Claude Code" in result


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
