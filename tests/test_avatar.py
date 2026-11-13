"""Exercise account routing and scheduling without changing any QQ profile."""

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from astrbot.core.cron.manager import CronJobManager
from data.plugins.astrbot_plugin_qq_avatar.main import PLUGIN, QQAvatar


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    """Check stateful avatar behavior against the AstrBot callback contract."""

    async def asyncSetUp(self):
        """Prepare separate libraries, connections, and in-memory state."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clients = {name: AsyncMock() for name in ["qq_bot_1", "qq_bot_2"]}
        self.context = MagicMock()
        self.context.cron_manager.list_jobs = AsyncMock(return_value=[])
        self.context.cron_manager.add_basic_job = AsyncMock()
        self.context.cron_manager.delete_job = AsyncMock()
        platforms = {}
        accounts = []
        for name, qq in [("qq_bot_1", "111"), ("qq_bot_2", "222")]:
            directory = self.root / name
            directory.mkdir()
            for filename in ["happy.png", "sleep.jpg"]:
                (directory / filename).write_bytes(b"avatar-" + name.encode())
            platform = MagicMock()
            platform.meta.return_value = SimpleNamespace(name="aiocqhttp")
            platform.get_client.return_value = self.clients[name]
            platforms[name] = platform
            accounts.append(
                {
                    "platform_id": name,
                    "qq_id": qq,
                    "library_dir": str(directory),
                    "selection": "sequential",
                    "cooldown_minutes": 0,
                    "cron": "0 9 * * 7",
                    "timezone": "Asia/Shanghai",
                }
            )
        self.context.get_platform_inst.side_effect = platforms.get
        self.plugin = QQAvatar(self.context, {"accounts": accounts})
        self.persisted = {}
        self.plugin.get_kv_data = AsyncMock(
            side_effect=lambda key, default: copy.deepcopy(
                self.persisted.get(key, default)
            )
        )
        self.plugin.put_kv_data = AsyncMock(
            side_effect=lambda key, value: self.persisted.update(
                {key: copy.deepcopy(value)}
            )
        )
        await self.plugin.initialize()

    async def test_event_routes_only_to_its_own_account(self):
        """Route tool calls through the matching instance and explicit self ID."""
        event = MagicMock()
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_platform_id.return_value = "qq_bot_2"
        event.get_self_id.return_value = "222"
        result = await self.plugin.choose_avatar(event, "happy.png")
        self.assertIn("已将 QQ 222", result)
        self.clients["qq_bot_1"].call_action.assert_not_awaited()
        call = self.clients["qq_bot_2"].call_action.await_args
        self.assertEqual(call.args, ("set_qq_avatar",))
        self.assertEqual(call.kwargs["self_id"], 222)
        self.assertEqual(
            base64.b64decode(call.kwargs["file"].removeprefix("base64://")),
            b"avatar-qq_bot_2",
        )
        event.get_self_id.return_value = "111"
        self.assertIn("尚未配置", await self.plugin.choose_avatar(event, "sleep.jpg"))
        self.assertEqual(self.clients["qq_bot_2"].call_action.await_count, 1)

    async def test_rotation_cooldown_and_persisted_selection(self):
        """Keep account cooldowns independent and resume sequential rotation."""
        first = "qq_bot_1:111"
        second = "qq_bot_2:222"
        self.plugin.accounts[first]["cooldown_minutes"] = 60
        self.plugin.config["accounts"][0]["cooldown_minutes"] = 60
        await self.plugin.change_avatar(first)
        self.assertIn("冷却中", await self.plugin.change_avatar(first))
        await self.plugin.change_avatar(second)
        self.assertEqual(self.plugin.state[second]["file_name"], "happy.png")
        saved = copy.deepcopy(self.plugin.state)
        self.persisted["state"] = saved
        await self.plugin.initialize()
        self.assertIn("冷却中", await self.plugin.change_avatar(first))
        self.plugin.accounts[first]["cooldown_minutes"] = 0
        self.assertIn("sleep.jpg", await self.plugin.change_avatar(first))
        self.assertEqual(self.plugin.state[first]["file_name"], "sleep.jpg")

    async def test_random_rotation_does_not_repeat(self):
        """Exclude the last successful filename when alternatives exist."""
        key = "qq_bot_1:111"
        self.plugin.accounts[key]["selection"] = "random"
        await self.plugin.change_avatar(key, "happy.png")
        await self.plugin.change_avatar(key)
        self.assertEqual(self.plugin.state[key]["file_name"], "sleep.jpg")

    async def test_api_failure_does_not_advance_state(self):
        """Leave selection and cooldown untouched after a rejected API call."""
        self.clients["qq_bot_1"].call_action.side_effect = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await self.plugin.change_avatar("qq_bot_1:111")
        self.assertNotIn("qq_bot_1:111", self.plugin.state)
        self.plugin.put_kv_data.assert_not_awaited()

    async def test_invalid_filename_and_ai_disabled(self):
        """Reject outside files and honor the account's AI switch."""
        with self.assertRaisesRegex(ValueError, "不在"):
            await self.plugin.change_avatar("qq_bot_1:111", "../qq_bot_2/happy.png")
        event = MagicMock()
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_platform_id.return_value = "qq_bot_1"
        event.get_self_id.return_value = "111"
        self.plugin.accounts["qq_bot_1:111"]["ai_enabled"] = False
        self.assertIn("未开启", await self.plugin.choose_avatar(event, "happy.png"))
        self.clients["qq_bot_1"].call_action.assert_not_awaited()
        self.assertIn("已将", await self.plugin.change_avatar("qq_bot_1:111"))
        self.assertEqual(
            json.loads(await self.plugin.avatar_library(event))["files"],
            ["happy.png", "sleep.jpg"],
        )

    async def test_metadata_restores_and_reaches_only_the_current_account_tool(self):
        """Preserve details across reloads and expose them alongside legacy filenames."""
        details = {"description": "温柔地微笑，适合打招呼。", "tags": ["开心", "温柔"]}
        await self.plugin.put_kv_data(
            "avatar_metadata", {"qq_bot_2:222": {"happy.png": details}}
        )
        await self.plugin.initialize()
        event = MagicMock()
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_platform_id.return_value = "qq_bot_2"
        event.get_self_id.return_value = "222"
        result = json.loads(await self.plugin.avatar_library(event))
        self.assertEqual(result["files"], ["happy.png", "sleep.jpg"])
        self.assertEqual(result["avatars"][0], {"file_name": "happy.png", **details})
        self.assertEqual(result["avatars"][1]["description"], "")
        self.assertEqual(result["avatars"][1]["tags"], [])
        request = SimpleNamespace(system_prompt="Existing persona")
        await self.plugin.avatar_hint(event, request)
        self.assertIn("中文说明、情绪标签", request.system_prompt)
        event.get_platform_id.return_value = "qq_bot_1"
        event.get_self_id.return_value = "111"
        result = json.loads(await self.plugin.avatar_library(event))
        self.assertTrue(
            all(
                not item["description"] and not item["tags"]
                for item in result["avatars"]
            )
        )

    async def test_native_cron_callback_and_owned_job_cleanup(self):
        """Run a registered callback with the real manager's payload dispatch."""
        manager = self.context.cron_manager
        registrations = manager.add_basic_job.await_args_list
        self.assertEqual(len(registrations), 2)
        registration = registrations[1].kwargs
        self.assertEqual(registration["cron_expression"], "0 9 * * 7")
        self.assertFalse(registration["persistent"])
        dispatcher = SimpleNamespace(_basic_handlers={"job": registration["handler"]})
        job = SimpleNamespace(job_id="job", payload=registration["payload"])
        await CronJobManager._run_basic_job(dispatcher, job)
        self.clients["qq_bot_2"].call_action.assert_awaited_once()
        manager.list_jobs.return_value = [
            SimpleNamespace(job_id="own", name=f"{PLUGIN}:qq_bot_2:222"),
            SimpleNamespace(job_id="other", name="unrelated"),
        ]
        await self.plugin.terminate()
        manager.delete_job.assert_awaited_once_with("own")


if __name__ == "__main__":
    unittest.main()
