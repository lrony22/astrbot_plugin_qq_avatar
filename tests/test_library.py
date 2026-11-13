"""Exercise library operations through the real plugin request wrapper."""

import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from PIL import Image
from starlette.datastructures import FormData, Headers, QueryParams, UploadFile

from astrbot.api.web import PluginRequest, bind_request_context
from data.plugins.astrbot_plugin_qq_avatar.library import AvatarLibraryView
from data.plugins.astrbot_plugin_qq_avatar.main import QQAvatar


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    """Check isolation, file validation, state preservation, and API dispatch."""

    def setUp(self):
        """Prepare two independent libraries and a valid image upload."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts = {}
        for key in ["qq_bot_1:111", "qq_bot_2:222"]:
            path = Path(self.temp.name) / key.split(":")[0]
            path.mkdir()
            self.accounts[key] = {"directory": path, "cooldown_minutes": 60}
        self.plugin = SimpleNamespace(
            accounts=self.accounts,
            state={},
            lock=asyncio.Lock(),
            put_kv_data=AsyncMock(),
            change_avatar=AsyncMock(return_value="未更换：冷却中。"),
        )
        self.plugin.library_files = lambda account: QQAvatar.library_files(
            None, account
        )
        self.view = AvatarLibraryView(self.plugin)
        output = io.BytesIO()
        Image.new("RGB", (32, 32), "#5678ab").save(output, "PNG")
        self.png = output.getvalue()

    async def call(self, handler, payload=None, query=None, upload=None, **kwargs):
        """Bind a real plugin request to one endpoint invocation.

        Args:
            handler: Endpoint being exercised.
            payload: Optional JSON body.
            query: Optional query values.
            upload: Optional filename and image bytes.
            **kwargs: Dynamic route arguments.

        Returns:
            HTTP status and decoded JSON response.
        """
        form = FormData()
        if upload:
            name, content = upload
            form = FormData([("file", UploadFile(io.BytesIO(content), filename=name))])
        raw = SimpleNamespace(
            method="POST" if payload is not None or upload else "GET",
            url=SimpleNamespace(path="/local-test"),
            headers=Headers(),
            cookies={},
            client=None,
            query_params=QueryParams(urlencode(query or {})),
            json=AsyncMock(return_value=payload or {}),
            form=AsyncMock(return_value=form),
        )
        with bind_request_context(PluginRequest(raw, username="local-test")):
            response = await handler(**kwargs)
        return response.status_code, json.loads(response.body)

    async def test_upload_isolated_and_preview_roundtrip(self):
        """Keep uploads within one account and return a real decoded preview."""
        status, result = await self.call(
            self.view.upload, upload=("smile.png", self.png), account_key="qq_bot_2:222"
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["file_name"], "smile.png")
        self.assertFalse(
            (self.accounts["qq_bot_1:111"]["directory"] / "smile.png").exists()
        )
        status, result = await self.call(
            self.view.image,
            query={"account_key": "qq_bot_2:222", "file_name": "smile.png"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["data_url"].startswith("data:image/png;base64,"))

    async def test_upload_rejects_duplicate_invalid_and_oversized_files(self):
        """Preserve existing images and leave no temporary rejected uploads."""
        directory = self.accounts["qq_bot_2:222"]["directory"]
        (directory / "smile.png").write_bytes(self.png)
        for name, data, expected in [
            ("smile.png", self.png, 409),
            ("bad.png", b"not an image", 400),
            ("wrong.jpg", self.png, 400),
            ("../outside.png", self.png, 400),
            ("large.png", b"x" * (5 * 1024 * 1024 + 1), 400),
        ]:
            with self.subTest(name=name):
                status, _ = await self.call(
                    self.view.upload, upload=(name, data), account_key="qq_bot_2:222"
                )
                self.assertEqual(status, expected)
        self.assertEqual([p.name for p in directory.iterdir()], ["smile.png"])
        self.assertEqual((directory / "smile.png").read_bytes(), self.png)

    async def test_rename_preserves_cooldown_and_delete_retains_selection(self):
        """Retain last successful state while changing only the library file."""
        directory = self.accounts["qq_bot_2:222"]["directory"]
        (directory / "smile.png").write_bytes(self.png)
        changed_at = time.time()
        self.plugin.state["qq_bot_2:222"] = {
            "file_name": "smile.png",
            "changed_at": changed_at,
        }
        status, _ = await self.call(
            self.view.rename,
            payload={
                "account_key": "qq_bot_2:222",
                "file_name": "smile.png",
                "new_name": "happy.png",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.plugin.state["qq_bot_2:222"],
            {"file_name": "happy.png", "changed_at": changed_at},
        )
        status, _ = await self.call(
            self.view.delete,
            payload={"account_key": "qq_bot_2:222", "file_name": "happy.png"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.plugin.state["qq_bot_2:222"]["changed_at"], changed_at)
        status, result = await self.call(
            self.view.avatars, query={"account_key": "qq_bot_2:222"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(result["last_selection_exists"])
        self.assertGreater(result["cooldown_remaining"], 0)
        self.plugin.change_avatar.assert_not_awaited()

    async def test_apply_reports_skip_failure_and_success(self):
        """Use the real change method contract without guessing a successful result."""
        payload = {"account_key": "qq_bot_2:222", "file_name": "smile.png"}
        status, result = await self.call(self.view.apply, payload=payload)
        self.assertEqual(status, 200)
        self.assertFalse(result["changed"])
        self.plugin.change_avatar.assert_awaited_with(
            "qq_bot_2:222", "smile.png", source="library-view"
        )
        self.plugin.change_avatar.side_effect = RuntimeError("offline")
        status, result = await self.call(self.view.apply, payload=payload)
        self.assertEqual(status, 400)
        self.assertIn("offline", result["message"])

        async def success(*args, **kwargs):
            self.plugin.state["qq_bot_2:222"] = {
                "changed_at": time.time(),
                "file_name": "smile.png",
            }
            return "已更换。"

        self.plugin.change_avatar.side_effect = success
        status, result = await self.call(self.view.apply, payload=payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["changed"])


if __name__ == "__main__":
    unittest.main()
