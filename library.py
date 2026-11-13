"""Authenticated avatar-library endpoints for the AstrBot plugin view."""

import asyncio
import base64
import io
import math
import time
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    json_response,
    request,
)

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class AvatarLibraryView:
    """Manage library files while sharing the plugin's existing account state."""

    def __init__(self, plugin):
        """Keep a reference to the live avatar plugin.

        Args:
            plugin: Plugin instance owning accounts, selections, and upload locks.
        """
        self.plugin = plugin

    def register(self):
        """Register routes behind the Dashboard's existing plugin authentication."""
        prefix = "/astrbot_plugin_qq_avatar"
        for endpoint, handler, method in [
            ("accounts", self.accounts, "GET"),
            ("avatars", self.avatars, "GET"),
            ("image", self.image, "GET"),
            ("upload/<account_key>", self.upload, "POST"),
            ("rename", self.rename, "POST"),
            ("delete", self.delete, "POST"),
            ("apply", self.apply, "POST"),
        ]:
            self.plugin.context.register_web_api(
                f"{prefix}/{endpoint}", handler, [method], "Manage QQ avatar library"
            )

    def unregister(self):
        """Detach this instance's handlers when the plugin is unloaded."""
        routes = self.plugin.context.registered_web_apis
        routes[:] = [r for r in routes if getattr(r[1], "__self__", None) is not self]

    def account(self, account_key: str) -> dict:
        """Resolve a configured account without accepting client filesystem paths.

        Args:
            account_key: Configured platform ID and bot QQ ID pair.

        Returns:
            Normalized account settings.

        Raises:
            ValueError: If the requested account is missing or disabled.
        """
        if not isinstance(account_key, str) or account_key not in self.plugin.accounts:
            raise ValueError("账号未配置或已停用，请检查插件配置并重载。")
        return self.plugin.accounts[account_key]

    def image_path(self, account_key: str, file_name: str) -> Path:
        """Resolve an image name inside its configured library.

        Args:
            account_key: Configured account key.
            file_name: A single portable image filename, including its suffix.

        Returns:
            Image path inside the account's approved directory.

        Raises:
            ValueError: If the filename or resolved path is invalid.
        """
        directory = self.account(account_key)["directory"]
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ["COM", "LPT"] for number in range(1, 10)
        }
        if (
            not isinstance(file_name, str)
            or not file_name
            or file_name.startswith(".")
            or file_name.endswith((" ", "."))
            or any(c in file_name for c in '/\\<>:"|?*')
            or any(ord(c) < 32 for c in file_name)
            or file_name.split(".", 1)[0].upper() in reserved
            or Path(file_name).suffix.lower() not in IMAGE_FORMATS
        ):
            raise ValueError("请使用有效的 PNG、JPG、JPEG 或 WebP 图片文件名。")
        path = directory / file_name
        if not path.resolve().is_relative_to(directory):
            raise ValueError("图片路径超出当前账号的头像库。")
        return path

    async def accounts(self):
        """Return the configured account choices for the library view.

        Returns:
            Account identifiers and their existing library configuration.
        """
        return json_response(
            {
                "accounts": [
                    {
                        "account_key": key,
                        "platform_id": account["platform_id"],
                        "qq_id": account["qq_id"],
                        "library_dir": str(account["directory"]),
                        "ai_enabled": account.get("ai_enabled", True),
                        "selection": account.get("selection", "random"),
                        "cron": account.get("cron", ""),
                        "timezone": account.get("timezone", "Asia/Shanghai"),
                    }
                    for key, account in self.plugin.accounts.items()
                ]
            }
        )

    async def avatars(self):
        """List images and report the last plugin selection and cooldown.

        Returns:
            Filenames, sizes, and current plugin-owned selection state.
        """
        try:
            key = request.query.get("account_key", "")
            account = self.account(key)
            async with self.plugin.lock:
                files = await asyncio.to_thread(self.plugin.library_files, account)
                previous = self.plugin.state.get(key, {})
                remaining = max(
                    0,
                    math.ceil(
                        previous.get("changed_at", 0)
                        + account["cooldown_minutes"] * 60
                        - time.time()
                    ),
                )
                return json_response(
                    {
                        "files": [
                            {"file_name": name, "size_bytes": path.stat().st_size}
                            for name, path in files.items()
                        ],
                        "last_selection": previous.get("file_name"),
                        "last_selection_exists": previous.get("file_name") in files,
                        "cooldown_remaining": remaining,
                    }
                )
        except (ValueError, OSError) as exc:
            return error_response(str(exc))

    async def image(self):
        """Return a bounded preview through the authenticated JSON bridge.

        Returns:
            PNG data URL for a thumbnail or expanded preview.
        """
        try:
            key = request.query.get("account_key", "")
            name = request.query.get("file_name", "")
            path = self.image_path(key, name)
            size = 1024 if request.query.get("full") == "1" else 320
            async with self.plugin.lock:
                if not path.is_file():
                    return error_response(
                        "图片已不存在，请刷新头像库。", status_code=404
                    )
                with Image.open(path) as original:
                    image = ImageOps.exif_transpose(original)
                    image.thumbnail((size, size))
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    output = io.BytesIO()
                    image.save(output, "PNG")
            return json_response(
                {
                    "data_url": "data:image/png;base64,"
                    + base64.b64encode(output.getvalue()).decode("ascii")
                }
            )
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            return error_response(f"无法预览图片：{exc}")

    async def upload(self, account_key: str):
        """Validate and atomically store one original image without overwriting.

        Args:
            account_key: Account selected in the upload route.

        Returns:
            Stored filename, or a validation error.
        """
        temporary = None
        try:
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile):
                return error_response("请选择要上传的图片。")
            path = self.image_path(account_key, upload.filename)
            async with self.plugin.lock:
                if path.exists() or path.is_symlink():
                    return error_response(
                        "已有同名图片，请修改文件名后上传。", status_code=409
                    )
                temporary = path.parent / f".{uuid4().hex}.upload"
                written = await upload.save(temporary, max_bytes=MAX_IMAGE_BYTES)
                if not written:
                    raise ValueError("图片不能为空。")
                with Image.open(temporary) as image:
                    if image.format != IMAGE_FORMATS[path.suffix.lower()]:
                        raise ValueError("图片实际格式与文件后缀不一致。")
                    image.verify()
                temporary.rename(path)
            return json_response({"file_name": path.name})
        except UnidentifiedImageError:
            return error_response("无法识别图片，请上传有效的 PNG、JPEG 或 WebP 图片。")
        except (ValueError, OSError, Image.DecompressionBombError) as exc:
            message = "图片不能超过 5 MiB。" if "bytes limit" in str(exc) else str(exc)
            return error_response(message)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def rename(self):
        """Rename an image and retain the last-selection cooldown.

        Returns:
            New filename, or a validation error.
        """
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求内容必须为 JSON 对象。")
            key = payload.get("account_key", "")
            old = self.image_path(key, payload.get("file_name", ""))
            new = self.image_path(key, payload.get("new_name", ""))
            if old.suffix.lower() != new.suffix.lower():
                raise ValueError("重命名时请保留原图片后缀。")
            async with self.plugin.lock:
                if not old.is_file():
                    return error_response(
                        "图片已不存在，请刷新头像库。", status_code=404
                    )
                if old.name == new.name:
                    return json_response({"file_name": old.name})
                if new.exists() or new.is_symlink():
                    return error_response(
                        "已有同名图片，请换一个名称。", status_code=409
                    )
                old.rename(new)
                # Explicitly shared folders refer to the same renamed library item.
                for account_key, account in self.plugin.accounts.items():
                    previous = self.plugin.state.get(account_key, {})
                    if (
                        account["directory"] == old.parent
                        and previous.get("file_name") == old.name
                    ):
                        previous["file_name"] = new.name
                await self.plugin.put_kv_data("state", self.plugin.state)
            return json_response({"file_name": new.name})
        except (ValueError, OSError) as exc:
            return error_response(str(exc))

    async def delete(self):
        """Remove a library image without changing the QQ account's live avatar.

        Returns:
            Deleted filename, or a validation error.
        """
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求内容必须为 JSON 对象。")
            path = self.image_path(
                payload.get("account_key", ""), payload.get("file_name", "")
            )
            async with self.plugin.lock:
                if not path.is_file():
                    return error_response(
                        "图片已不存在，请刷新头像库。", status_code=404
                    )
                path.unlink()
            return json_response({"file_name": path.name})
        except (ValueError, OSError) as exc:
            return error_response(str(exc))

    async def apply(self):
        """Apply a selected image using the existing OneBot account connection.

        Returns:
            Actual change or skip result, or the OneBot failure message.
        """
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求内容必须为 JSON 对象。")
            key = payload.get("account_key", "")
            name = payload.get("file_name", "")
            self.image_path(key, name)
            before = self.plugin.state.get(key, {}).get("changed_at")
            message = await self.plugin.change_avatar(key, name, source="library-view")
            changed = self.plugin.state.get(key, {}).get("changed_at") != before
            return json_response({"changed": changed, "message": message})
        except Exception as exc:
            return error_response(f"更换头像失败：{exc}")
