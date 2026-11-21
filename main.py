"""伪造聊天（图片版）—— 把对话渲染成 QQ 风格聊天截图。

官方机器人不支持合并转发，所以改为本地渲染 PNG 再发送。

指令：
    /伪造聊天 QQ号 内容 | QQ号 内容 | ...
    每段支持 `QQ号#昵称 内容` 手动指定昵称。
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import tempfile
import time
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_fake_chat_image"
AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
OFFICIAL_AVATAR_URL = "https://q.qlogo.cn/qqapp/{appid}/{openid}/0"

CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f2f3f5;
  font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif;
}
#chat { width: 640px; padding: 18px 16px 22px; background: #f2f3f5; }
.time {
  text-align: center; color: #999; font-size: 12px; margin: 6px 0 14px;
}
.row { display: flex; margin-bottom: 16px; align-items: flex-start; }
.row.right { flex-direction: row-reverse; }
.avatar {
  width: 40px; height: 40px; border-radius: 50%; overflow: hidden;
  flex: 0 0 40px; background: #cfd8e3; display: flex;
  align-items: center; justify-content: center; color: #fff; font-size: 16px;
}
.avatar img { width: 100%; height: 100%; object-fit: cover; }
.body { max-width: 460px; margin: 0 10px; }
.row.right .body { text-align: right; }
.name { font-size: 12px; color: #8a8f99; margin: 0 0 4px 2px; }
.row.right .name { margin-right: 2px; }
.bubble {
  display: inline-block; text-align: left; background: #fff;
  color: #1f2329; padding: 9px 12px; border-radius: 10px;
  font-size: 15px; line-height: 1.45; word-break: break-word;
  white-space: pre-wrap; box-shadow: 0 1px 1px rgba(0,0,0,.04);
}
.row.right .bubble { background: #95ec69; }
"""


@register(PLUGIN_NAME, "chen", "伪造聊天（图片版）", "1.0.0")
class FakeChatImagePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self._nick_cache: dict[str, str] = {}
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ───────────────────────── 解析 ─────────────────────────
    async def _parse_event(self, event: AstrMessageEvent) -> tuple[list[dict], int]:
        """解析消息链：支持 `@某人 内容|@某人 内容` 与 `QQ号#昵称 内容`。

        官方 bot 不会把 @群友 解析成 At 组件，而是保留 `<@!openid>` 标记，
        所以这里同时处理 At 组件与文本标记两种形式。
        """
        # 1) 消息链 → 带标记的文本
        parts: list[str] = []
        for comp in event.get_messages():
            kind = type(comp).__name__
            if kind == "At":
                qq = str(getattr(comp, "qq", "") or "")
                name = str(getattr(comp, "name", "") or "").strip()
                # 机器人/全体 的 @ 不算说话人
                if qq in ("qq_official", "all", "unknown_selfid", ""):
                    continue
                parts.append(f"<@{qq}@{name}>")
            elif kind == "Plain":
                parts.append(str(getattr(comp, "text", "") or ""))
        text = "".join(parts)
        if not text:
            text = event.message_str or ""

        # 2) 去掉指令本体
        text = re.sub(r"^[\s/／]*(伪造聊天|伪造记录|假聊天)", "", text)
        text = text.replace("\u2005", " ")

        # 3) 按 <@id> / <@!id> / <@id@name> 切分说话人
        tokens = re.split(r"(<@!?[^>]*>)", text)
        chunks: list[dict] = []
        speaker: tuple[str, str] | None = None
        buf = ""

        def flush():
            nonlocal speaker, buf
            body = buf.strip()
            if speaker and body:
                chunks.append(
                    {"qq": speaker[0], "name": speaker[1], "text": body}
                )
            buf = ""

        for tok in tokens:
            m = re.match(r"<@!?([^>]*)>", tok)
            if m:
                flush()
                inner = m.group(1)
                if "@" in inner:
                    qq, name = inner.split("@", 1)
                else:
                    qq, name = inner, ""
                speaker = (qq.strip(), name.strip())
                continue
            pieces = tok.split("|")
            for i, piece in enumerate(pieces):
                buf += piece
                if i < len(pieces) - 1:
                    flush()
                    speaker = None
        flush()

        # 4) 没有识别到任何 @ → 退回旧格式 QQ号#昵称
        if not chunks:
            return self._parse(text)
        return chunks, 0

    def _parse(self, raw: str) -> tuple[list[dict], int]:
        """解析 `QQ号#昵称 内容 | ...`，返回 (消息列表, 格式错误段数)。"""
        items: list[dict] = []
        invalid = 0
        for chunk in raw.split("|"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(None, 1)
            if len(parts) < 2:
                invalid += 1
                continue
            head, content = parts[0], parts[1].strip()
            if "#" not in head:
                invalid += 1
                continue
            qq, name = head.split("#", 1)
            qq = qq.strip()
            name = name.strip()
            if not qq or not name or not content:
                invalid += 1
                continue
            items.append({"qq": qq, "name": name, "text": content})
        limit = int(self.config.get("max_messages", 20) or 20)
        return items[:limit], invalid

    async def _qq_nickname(self, qq: str) -> str:
        """借 NapCat（aiocqhttp）账号查询 QQ 昵称。"""
        if not qq.isdigit():
            return ""
        cached = self._nick_cache.get(qq)
        if cached:
            return cached
        # 聊天缓存里可能存了带前缀的 key，做一次宽松匹配
        for key, value in self._nick_cache.items():
            if key.endswith(qq) or qq.endswith(key):
                return value
        try:
            for platform in self.context.platform_manager.platform_insts:
                if platform.meta().name != "aiocqhttp":
                    continue
                client = platform.get_client()
                if client is None:
                    continue
                info = await client.call_action(
                    "get_stranger_info", user_id=int(qq), no_cache=False
                )
                if isinstance(info, dict):
                    nick = str(info.get("nick") or info.get("nickname") or "").strip()
                    if nick:
                        self._nick_cache[qq] = nick
                        return nick
        except Exception as exc:
            self._debug(f"昵称查询失败 {qq}: {exc}")
        return ""

    # ───────────────────────── 头像 ─────────────────────────
    async def _avatars(self, items: list[dict]) -> dict[str, str]:
        """下载头像，返回 qq -> data URI。失败返回空 dict。"""
        result: dict[str, str] = {}
        if not items:
            return result
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), trust_env=True
        ) as session:
            appid = str(self.config.get("qq_appid") or "").strip()

            async def fetch(qq: str):
                if qq.isdigit():
                    url = AVATAR_URL.format(qq=qq)
                elif appid:
                    url = OFFICIAL_AVATAR_URL.format(appid=appid, openid=qq)
                else:
                    return
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.read()
                        if len(data) < 200:
                            return
                        result[qq] = (
                            "data:image/png;base64,"
                            + base64.b64encode(data).decode()
                        )
                except Exception:
                    return

            await asyncio.gather(*(fetch(i["qq"]) for i in items))

        return result

    # ───────────────────────── HTML ─────────────────────────
    def _html(self, items: list[dict], avatars: dict[str, str], self_qq: str) -> str:
        rows = []
        for item in items:
            right = bool(self_qq) and item["qq"] == self_qq
            avatar = avatars.get(item["qq"])
            if avatar:
                avatar_html = f'<img src="{avatar}" />'
            else:
                letter = html.escape(item["name"][:1] or "?")
                avatar_html = f"<span>{letter}</span>"
            rows.append(
                f"""
      <div class="row{' right' if right else ''}">
        <div class="avatar">{avatar_html}</div>
        <div class="body">
          <div class="name">{html.escape(item['name'])}</div>
          <div class="bubble">{html.escape(item['text'])}</div>
        </div>
      </div>"""
            )
        stamp = time.strftime("%H:%M")
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>{CSS}</style></head>
<body><div id="chat">
  <div class="time">{stamp}</div>{''.join(rows)}
</div></body></html>"""

    # ───────────────────────── 渲染 ─────────────────────────
    async def _render(self, html_text: str) -> str:
        from playwright.async_api import async_playwright

        tmp_html = Path(tempfile.mkstemp(suffix=".html")[1])
        tmp_png = Path(tempfile.mkstemp(suffix=".png")[1])
        tmp_html.write_text(html_text, encoding="utf-8")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    args=["--no-sandbox", "--allow-file-access-from-files"]
                )
                page = await browser.new_page(
                    viewport={"width": 700, "height": 900},
                    device_scale_factor=2,
                )
                await page.goto(tmp_html.as_uri())
                await page.wait_for_timeout(350)
                element = await page.query_selector("#chat")
                if element is None:
                    await page.screenshot(path=str(tmp_png), full_page=True)
                else:
                    await element.screenshot(path=str(tmp_png))
                await browser.close()
            return str(tmp_png)
        except Exception as exc:
            logger.error(f"[伪造聊天] 渲染失败: {exc}")
            raise
        finally:
            try:
                tmp_html.unlink(missing_ok=True)
            except Exception:
                pass

    # ───────────────────────── 指令 ─────────────────────────
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cache_sender(self, event: AstrMessageEvent):
        """记录群内发言者昵称，供伪造聊天复用。"""
        try:
            name = (event.get_sender_name() or "").strip()
            sid = str(event.get_sender_id() or "")
            if name and sid:
                self._nick_cache[sid] = name
        except Exception:
            return

    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        """取消息链原始文本（保留 / 前缀）。"""
        parts = []
        try:
            for comp in event.get_messages():
                if type(comp).__name__ == "Plain":
                    parts.append(str(getattr(comp, "text", "") or ""))
        except Exception:
            return ""
        return "".join(parts).strip()

    def _debug(self, text: str) -> None:
        try:
            path = self.data_dir / "hook.log"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(time.strftime("%m-%d %H:%M:%S ") + text[:200] + "\n")
        except Exception:
            pass

    def _help_text(self) -> str:
        return (
            "▍用法\n"
            "/伪造聊天@某人 内容|@另一个人 内容\n"
            "\n"
            "▍示例\n"
            "/伪造聊天@晨喵 阿罗娜今天有点奇怪|@白子 她是不是又在偷看老师\n"
            "\n"
            "▍说明\n"
            "· 被 @ 的人先在群里说句话，昵称才能对上\n"
            "· 单次最多渲染 20 条"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def fake_chat(self, event: AstrMessageEvent):
        """伪造聊天记录（图片版）。

        用法：/伪造聊天@某人 内容|@另一个人 内容
              或 /伪造聊天 123456#昵称 内容|654321#昵称 内容
        """
        raw = self._raw_text(event) or (event.message_str or "")
        raw = raw.strip().lstrip("/／").strip()
        self._debug(f"收到: {raw!r}")
        if raw in ("伪造帮助",):
            yield event.plain_result(self._help_text())
            return
        for prefix in ("伪造聊天", "伪造记录", "假聊天"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
                break
        else:
            return  # 不是本指令，交给别的插件
        has_at = any(
            type(c).__name__ == "At" for c in event.get_messages()
        )
        if not raw and not has_at:
            yield event.plain_result(
                "用法：/伪造聊天@某人 内容|@另一个人 内容\n"
                "例：/伪造聊天@晨喵 今天谁去食堂|@白子 我去"
            )
            return

        items, invalid = await self._parse_event(event)
        if invalid:
            yield event.plain_result(
                "格式不对哦，可以这样写：\n"
                "/伪造聊天@晨喵 内容|@白子 内容\n"
                "或：/伪造聊天 123456#晨喵 内容|654321#白子 内容"
            )
            return
        if not items:
            yield event.plain_result(
                "用法：/伪造聊天 123456#晨喵 内容 | 654321#白子 内容"
            )
            return

        for item in items:
            if not item.get("name"):
                cached = self._nick_cache.get(item["qq"]) or ""
                if cached:
                    item["name"] = cached
                else:
                    item["name"] = "群友" + item["qq"][-4:] if item["qq"] else "群友"
        self_qq = str(self.config.get("self_qq") or "").strip()
        if not self_qq:
            sender = str(event.get_sender_id() or "")
            self_qq = sender if sender.isdigit() else ""

        avatars = await self._avatars(items)
        html_text = self._html(items, avatars, self_qq)
        try:
            path = await self._render(html_text)
        except Exception as exc:
            yield event.plain_result(f"渲染失败：{exc}")
            return
        yield event.image_result(path)

    async def terminate(self):
        logger.info("[伪造聊天] 插件已卸载")
