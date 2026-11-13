"""Account-specific QQ avatar libraries using existing OneBot connections."""

import asyncio
import base64
import json
import random
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_root,
)

from .library import AvatarLibraryView

PLUGIN = "astrbot_plugin_qq_avatar"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class QQAvatar(Star):
    """Expose library selection and independent schedules for QQ accounts."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Store runtime services and dashboard settings.

        Args:
            context: Active AstrBot runtime.
            config: Account libraries and rotation settings.
        """
        super().__init__(context)
        self.config = config
        self.accounts = {}
        self.state = {}
        self.lock = asyncio.Lock()
        self.library_view = AvatarLibraryView(self)

    async def initialize(self):
        """Restore selections and register nonpersistent account schedules.

        Raises:
            ValueError: If an account or schedule configuration is invalid.
        """
        self.state = await self.get_kv_data("state", {}) or {}
        manager = self.context.cron_manager
        # Remove only this plugin's orphaned jobs after an interrupted shutdown.
        await self.terminate()
        self.accounts.clear()

        for raw in self.config.get("accounts", []):
            if not raw.get("enabled", True):
                continue
            account = dict(raw)
            platform_id = str(account.get("platform_id", "")).strip()
            qq_id = str(account.get("qq_id", "")).strip()
            key = f"{platform_id}:{qq_id}"
            if not platform_id or not qq_id.isdigit() or int(qq_id) <= 0:
                raise ValueError("Each enabled account needs a platform ID and QQ ID")
            if key in self.accounts:
                raise ValueError(f"Duplicate avatar account: {key}")
            mode = account.get("selection", "random")
            if mode not in {"random", "sequential"}:
                raise ValueError(f"Invalid avatar selection mode for {key}: {mode}")
            cooldown = int(account.get("cooldown_minutes", 60))
            if cooldown < 0:
                raise ValueError(f"Avatar cooldown must be nonnegative: {key}")
            library = str(account.get("library_dir", "")).strip()
            directory = (
                Path(library).expanduser()
                if library
                else Path(get_astrbot_plugin_data_path()) / PLUGIN / qq_id
            )
            if not directory.is_absolute():
                directory = Path(get_astrbot_root()) / directory
            account.update(
                platform_id=platform_id,
                qq_id=qq_id,
                directory=directory.resolve(),
                cooldown_minutes=cooldown,
            )
            cron = str(account.get("cron", "")).strip()
            timezone = str(account.get("timezone", "Asia/Shanghai")).strip()
            if cron:
                ZoneInfo(timezone)
            self.accounts[key] = account

        try:
            for key, account in self.accounts.items():
                account["directory"].mkdir(parents=True, exist_ok=True)
                if not str(account.get("cron", "")).strip():
                    continue
                await manager.add_basic_job(
                    name=f"{PLUGIN}:{key}",
                    cron_expression=account["cron"].strip(),
                    timezone=account.get("timezone", "Asia/Shanghai").strip(),
                    handler=self.change_avatar,
                    payload={"account_key": key, "source": "scheduled"},
                    persistent=False,
                )
            self.library_view.register()
        except Exception:
            await self.terminate()
            raise

    async def terminate(self):
        """Remove account schedules and drain their running callbacks."""
        self.library_view.unregister()
        manager = self.context.cron_manager
        for job in await manager.list_jobs("basic"):
            if job.name.startswith(PLUGIN + ":"):
                await manager.delete_job(job.job_id)

    def account_for(self, event: AstrMessageEvent) -> tuple[str, dict]:
        """Resolve only the account associated with the incoming event.

        Args:
            event: Conversation or command event.

        Returns:
            Account key and configured account settings.

        Raises:
            ValueError: If the event is not from a configured OneBot account.
        """
        key = f"{event.get_platform_id()}:{event.get_self_id()}"
        if event.get_platform_name() != "aiocqhttp" or key not in self.accounts:
            raise ValueError("当前 OneBot 账号尚未配置头像库。")
        return key, self.accounts[key]

    def library_files(self, account: dict) -> dict[str, Path]:
        """Read a flat library without following links outside its directory.

        Args:
            account: Normalized account settings.

        Returns:
            Supported image filenames mapped to resolved local paths.

        Raises:
            OSError: If the configured directory cannot be read.
        """
        directory = account["directory"]
        return {
            path.name: path.resolve()
            for path in sorted(directory.iterdir(), key=lambda p: p.name)
            if path.suffix.lower() in IMAGE_SUFFIXES
            and path.is_file()
            and path.resolve().is_relative_to(directory)
        }

    async def change_avatar(
        self, account_key: str, file_name: str = "", source: str = "manual"
    ) -> str:
        """Select and upload an image through the account's existing connection.

        Args:
            account_key: Exact configured platform ID and QQ ID pair.
            file_name: Library filename, or empty for configured rotation.
            source: Caller label used in logs.

        Returns:
            Change confirmation or the reason a change was skipped.

        Raises:
            ValueError: If the library, image, or platform is unavailable.
            Exception: If OneBot rejects the upload or state cannot be saved.
        """
        async with self.lock:
            account = self.accounts[account_key]
            files = await asyncio.to_thread(self.library_files, account)
            if not files:
                raise ValueError("头像库为空，请添加 PNG、JPG 或 WebP 图片。")
            previous = self.state.get(account_key, {})
            last_file = previous.get("file_name", "")
            if file_name:
                if file_name not in files:
                    raise ValueError("图片不在当前账号的头像库中，请先查看头像库。")
            elif account.get("selection", "random") == "sequential":
                names = list(files)
                index = names.index(last_file) + 1 if last_file in names else 0
                file_name = names[index % len(names)]
            else:
                choices = [name for name in files if name != last_file] or list(files)
                file_name = random.choice(choices)
            if file_name == last_file:
                return f"未更换：{file_name} 已是本插件上次设置的头像。"
            remaining = (
                float(previous.get("changed_at", 0))
                + account["cooldown_minutes"] * 60
                - time.time()
            )
            if remaining > 0:
                return f"未更换：冷却中，约 {int(remaining) + 1} 秒后可再次更换。"
            path = files[file_name]
            if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                raise ValueError("头像图片不能为空，大小不能超过 5 MiB。")
            image = await asyncio.to_thread(path.read_bytes)
            if not 0 < len(image) <= MAX_IMAGE_BYTES:
                raise ValueError("头像图片不能为空，大小不能超过 5 MiB。")
            platform = self.context.get_platform_inst(account["platform_id"])
            if platform is None or platform.meta().name != "aiocqhttp":
                raise ValueError("对应的 OneBot 平台实例不可用。")
            # Inline bytes work even when AstrBot and SnowLuma use different paths.
            await platform.get_client().call_action(
                "set_qq_avatar",
                self_id=int(account["qq_id"]),
                file="base64://" + base64.b64encode(image).decode("ascii"),
            )
            self.state[account_key] = {
                "file_name": file_name,
                "changed_at": time.time(),
            }
            await self.put_kv_data("state", self.state)
            logger.info(
                "QQ avatar changed: account=%s file=%s source=%s",
                account_key,
                file_name,
                source,
            )
            return f"已将 QQ {account['qq_id']} 的头像更换为 {file_name}。"

    @filter.llm_tool(name="qq_avatar_library")
    async def avatar_library(self, event: AstrMessageEvent) -> str:
        """查看当前 QQ 账号的头像库，供选择新头像时参考。

        Returns:
            可用图片的文件名、上次选择的头像及轮换设置。
        """
        try:
            key, account = self.account_for(event)
            files = await asyncio.to_thread(self.library_files, account)
            return json.dumps(
                {
                    "account": key,
                    "ai_enabled": account.get("ai_enabled", True),
                    "last_selection": self.state.get(key, {}).get("file_name"),
                    "files": list(files),
                    "selection": account.get("selection", "random"),
                    "cron": account.get("cron", "") or "关闭",
                    "timezone": account.get("timezone", "Asia/Shanghai"),
                    "cooldown_minutes": account["cooldown_minutes"],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return f"无法查看头像库：{exc}"

    @filter.llm_tool(name="qq_avatar_change")
    async def choose_avatar(self, event: AstrMessageEvent, file_name: str) -> str:
        """从当前账号的头像库中选图，更换自己的 QQ 头像。

        可结合对话情境、人设或心情自主选择，仅影响当前账号，并遵守冷却时间。

        Args:
            file_name (string): qq_avatar_library 返回的完整图片文件名。

        Returns:
            更换结果、冷却提示或失败原因。
        """
        try:
            key, account = self.account_for(event)
            if not account.get("ai_enabled", True):
                return "当前账号未开启 AI 自主换头像。"
            if not file_name:
                return "请先查看头像库，再传入完整图片文件名。"
            return await self.change_avatar(key, file_name, source="ai")
        except Exception as exc:
            return f"更换头像失败：{exc}"

    @filter.on_llm_request()
    async def avatar_hint(self, event: AstrMessageEvent, request: ProviderRequest):
        """为已配置的 QQ 账号添加 AI 自主选择和更换头像的提示。

        Args:
            event: 用于确定当前 QQ 账号的对话事件。
            request: 待发送给模型的请求，用于追加头像能力说明。
        """
        try:
            _, account = self.account_for(event)
        except ValueError:
            return
        if account.get("ai_enabled", True):
            request.system_prompt = (request.system_prompt or "") + (
                "\n[QQ 头像能力] 你可以根据自身人设、心情和对话情境自主换头像，"
                "无需等用户每次提出要求。先用 qq_avatar_library 查看当前账号头像库，"
                "按文件名含义选择合适的图片，再用 qq_avatar_change 更换；"
                "合适时才更换，遵守冷却，未成功时不要声称已更换。"
            )

    @filter.command("头像库", alias={"avatar_library"})
    async def show_library(self, event: AstrMessageEvent):
        """查看当前账号的头像库和轮换设置。

        Args:
            event: 收到的头像库查询指令。
        """
        yield event.plain_result(await self.avatar_library(event))

    @filter.command("换头像", alias={"avatar_change"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manual_change(self, event: AstrMessageEvent, file_name: str = ""):
        """由管理员选择指定图片，或按当前配置轮换一次头像。

        Args:
            event: 管理员发送的换头像指令，用于确定目标账号。
            file_name: 完整图片文件名；留空时按当前账号的配置轮换。
        """
        try:
            key, _ = self.account_for(event)
            result = await self.change_avatar(key, file_name)
        except Exception as exc:
            result = f"更换头像失败：{exc}"
        yield event.plain_result(result)
