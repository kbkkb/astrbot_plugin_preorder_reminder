import asyncio
import logging
import re
from typing import Dict, Any, List, Optional
from pathlib import Path

from astrbot.api.star import register, Star
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import event_message_type, EventMessageType
from astrbot.api.message_components import Plain
from astrbot.core.platform.astr_message_event import MessageSession

from .core.database import Database
from .core.matcher import Matcher
from .core.notifier import Notifier
from .core.scheduler import Scheduler
from .channels.weibo_channel import WeiboChannel
from .channels.wechat_channel import WeChatChannel
from .channels.qq_group_channel import QQGroupChannel

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

@register(
    "astrbot_plugin_preorder_reminder",
    "eskyfun",
    "手办模玩预售补款提醒与小店情报管家",
    "1.1.0"
)
class PreorderReminderPlugin(Star):
    def __init__(self, context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}

        # 1. 核心持久化与业务组件
        self.db = Database()
        self.matcher = Matcher(enable_llm=self.config.get("enable_llm_fuzzy_match", True))
        self.notifier = Notifier()

        # 2. 情报渠道
        self.weibo_channel = WeiboChannel(
            custom_cookie=self.config.get("weibo_cookie", ""),
            rss_base_url=self.config.get("weibo_rss_base_url", "")
        )
        self.wechat_channel = WeChatChannel(
            rss_base_url=self.config.get("wechat_rss_base_url", ""),
            enable_ocr=bool(self.config.get("enable_ocr", True)),
            max_ocr_images=int(self.config.get("max_ocr_images", 6))
        )
        self.qq_group_channel = QQGroupChannel(self.matcher)

        # 3. 后台轮询与早报调度器
        self.scheduler = Scheduler(
            db=self.db,
            matcher=self.matcher,
            notifier=self.notifier,
            weibo_channel=self.weibo_channel,
            wechat_channel=self.wechat_channel,
            send_message_func=self._send_push_message,
            poll_interval_minutes=int(self.config.get("poll_interval_minutes", 15)),
            daily_digest_enabled=bool(self.config.get("daily_digest_enabled", True)),
            daily_digest_time=str(self.config.get("daily_digest_time", "09:30")),
            default_notify_target_type=str(self.config.get("default_notify_target_type", "private")),
            default_notify_group_id=str(self.config.get("default_notify_group_id", ""))
        )

    async def initialize(self):
        """插件初始化，启动后台轮询"""
        self.scheduler.start()
        logger.info("[PreorderReminder] 预售补款提醒插件已启动就绪！")

    async def terminate(self):
        """插件停止，释放资源"""
        self.scheduler.stop()
        logger.info("[PreorderReminder] 预售补款提醒插件已安全停止")

    async def _send_push_message(self, target_type: str, target_id: str, text: str):
        """异步统一推送消息给目标用户或群聊"""
        if not target_id:
            return
        chain = MessageChain([Plain(text)])
        tid = str(target_id).strip()

        # 如果已经是 unified_msg_origin 格式
        if ":" in tid:
            await self.context.send_message(tid, chain)
            return

        # 若是纯数字，按照私聊或群聊包装
        if target_type == "group":
            try:
                await self.context.send_message(f"default:GroupMessage:{tid}", chain)
            except Exception:
                await self.context.send_message(tid, chain)
        else:
            try:
                await self.context.send_message(f"default:FriendMessage:{tid}", chain)
            except Exception:
                await self.context.send_message(tid, chain)

    def _is_admin(self, user_id: str) -> bool:
        admin_list = [str(u).strip() for u in self.config.get("admin_users", []) if str(u).strip()]
        if not admin_list:
            return True
        return str(user_id).strip() in admin_list

    # ==================== QQ 补款群静默嗅探探针 ====================

    @event_message_type(EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """
        监听所有群消息。
        如果属于被监控的补款群，进入纯单向静默监听模式：
        1. 过滤店主/管理员有效补款通知，命中后私聊推送订阅买家；
        2. 彻底阻断普通闲聊与Bot在群内的任何回复（event.stop_event），零打扰群友！
        """
        group_id = str(event.message_obj.group_id or "").strip()
        monitored_map = self.db.get_monitored_qq_groups()

        if group_id not in monitored_map:
            return  # 普通群，正常放行给 AstrBot 处理

        # 获取发言者信息
        sender_id = str(event.message_obj.sender.user_id or "").strip()
        sender_role = getattr(event.message_obj.sender, "role", "member") or "member"
        message_id = str(event.message_obj.message_id or "")
        raw_text = event.message_str or ""

        is_monitored, is_valid_notice, shop, notice_data = self.qq_group_channel.inspect_group_message(
            group_id=group_id,
            sender_id=sender_id,
            sender_role=sender_role,
            message_id=message_id,
            raw_text=raw_text,
            monitored_map=monitored_map
        )

        if is_monitored:
            # 如果是有效补款/开定通知，交给调度器做比对与私聊推送
            if is_valid_notice and notice_data:
                asyncio.create_task(self.scheduler.process_single_notice(notice_data))

            # 核心机制：对补款群内的所有消息，调用 stop_event 阻止后续插件和 Bot 闲聊回复
            # 确保 Bot 在补款群里永远保持 100% 绝对静默！
            event.stop_event()

    # ==================== 快捷指令体系 ====================

    @filter.command("店铺列表")
    async def cmd_shop_list(self, event: AstrMessageEvent):
        """查看当前所有记录的模玩店铺档案"""
        shops = self.db.get_all_shops()
        yield event.plain_result(self.notifier.format_shop_list(shops))

    @filter.command("添加店铺")
    async def cmd_add_shop(self, event: AstrMessageEvent, name: str, *args):
        """
        添加或绑定模玩店铺档案。
        用法：/添加店铺 猫受屋 weibo:12345678 group:720694396 wechat:猫受屋
        """
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("❌ 抱歉，只有管理员有权添加或修改店铺档案。")
            return

        weibo_uid = ""
        wechat = ""
        groups = []
        notes = ""

        for arg in args:
            arg_str = str(arg).strip()
            if arg_str.startswith("weibo:") or arg_str.startswith("微博:"):
                weibo_uid = arg_str.split(":", 1)[1]
            elif arg_str.startswith("group:") or arg_str.startswith("群:") or arg_str.startswith("补款群:"):
                groups.append(arg_str.split(":", 1)[1])
            elif arg_str.startswith("wechat:") or arg_str.startswith("公众号:"):
                wechat = arg_str.split(":", 1)[1]
            else:
                notes += f" {arg_str}"

        self.db.add_shop(
            name=name,
            weibo_uid=weibo_uid,
            wechat_account=wechat,
            qq_groups=groups,
            notes=notes.strip()
        )
        # 立即异步执行一次该店铺的初次抓取，无需等待定时轮询
        shop_obj = self.db.get_shop_by_name(name)
        if shop_obj:
            asyncio.create_task(self.scheduler.poll_single_shop(shop_obj))

        yield event.plain_result(f"✅ 成功录入/更新店铺【{name}】档案！已触发初次数据拉取。\n发送 `/店铺列表` 可查看详情。")

    @filter.command("刷新店铺")
    async def cmd_refresh_shop(self, event: AstrMessageEvent, shop_name: str):
        """立即对指定店铺执行一次拉取更新。用法：/刷新店铺 GSC"""
        yield event.plain_result(f"⏳ 正在即时刷新店铺【{shop_name}】的情报...")
        res = await self.scheduler.refresh_shop(shop_name)
        yield event.plain_result(res["msg"])

    @filter.command("设置微博cookie")
    async def cmd_set_weibo_cookie(self, event: AstrMessageEvent, cookie: str):
        """设置微博爬虫Cookie（用于绕过微博432风控）。用法：/设置微博cookie <SUB值或完整Cookie>"""
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("❌ 抱歉，只有管理员有权设置 Cookie。")
            return
        self.weibo_channel.set_cookie(cookie)
        yield event.plain_result("✅ 微博爬虫 Cookie 已更新并立即生效！后续所有店铺微博拉取将自动携带该凭据。")

    @filter.command("录入通知")
    async def cmd_record_notice(self, event: AstrMessageEvent, shop_name: str, notice_type: str, content: str):
        """手动录入一条补款或开订情报。用法：/录入通知 GSC 补款 25号初音韶华手办开补"""
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("❌ 抱歉，只有管理员有权手动录入通知。")
            return
        ntype = "replenish" if ("补" in notice_type or "款" in notice_type) else "new_preorder"
        nid = self.db.record_manual_notice(shop_name=shop_name, notice_type=ntype, content=content)
        shop = self.db.get_shop_by_name(shop_name)
        asyncio.create_task(self.scheduler.process_single_notice({
            "id": nid,
            "shop_id": shop["id"] if shop else None,
            "shop_name": shop["name"] if shop else shop_name,
            "channel": "manual",
            "source_id": f"manual_{nid}",
            "title": content[:30],
            "content": content,
            "source_url": ""
        }))
        type_str = "补款通知" if ntype == "replenish" else "新开预订"
        yield event.plain_result(f"✅ 已成功录入【{shop_name}】的{type_str}！已自动比对在监商品并执行推送。")

    @filter.command("删除店铺")
    async def cmd_del_shop(self, event: AstrMessageEvent, shop_name_or_id: str):
        """删除店铺档案"""
        if not self._is_admin(event.get_sender_id()):
            yield event.plain_result("❌ 抱歉，只有管理员有权删除店铺。")
            return

        shop = None
        if shop_name_or_id.isdigit():
            shop = self.db.get_shop_by_id(int(shop_name_or_id))
        if not shop:
            shop = self.db.get_shop_by_name(shop_name_or_id)

        if not shop:
            yield event.plain_result(f"未找到店铺【{shop_name_or_id}】。")
            return

        self.db.delete_shop(shop["id"])
        yield event.plain_result(f"🗑️ 已成功删除店铺【{shop['name']}】档案。")

    @filter.command("订阅补款")
    async def cmd_subscribe(self, event: AstrMessageEvent, shop_name: str, item_name: str, deposit: float = 0.0, est_month: str = ""):
        """
        订阅指定店铺的某款手办/周边补款提醒。
        用法：/订阅补款 猫受屋 初音韶华 50 10月
        """
        user_id = event.get_sender_id()
        origin = event.unified_msg_origin
        target_type = "group" if event.message_obj.group_id else "private"

        sub_id = self.db.add_subscription(
            user_id=user_id,
            item_name=item_name,
            shop_name=shop_name,
            target_type=target_type,
            target_id=origin,
            deposit_amount=deposit,
            estimated_month=est_month
        )

        res = [
            "✅ 成功开启补款追踪！",
            f"📦 预订商品：【{item_name}】",
            f"🏬 监控店铺：【{shop_name or '全网小店'}】"
        ]
        if deposit:
            res.append(f"💰 已付定金：¥{deposit}")
        if est_month:
            res.append(f"📅 预估月份：{est_month}")
        res.append("📡 监控渠道已就绪（微博 / 公众号 / QQ补款群），开补时将第一时间为您推送！")
        yield event.plain_result("\n".join(res))

    @filter.command("我的补款")
    async def cmd_my_subs(self, event: AstrMessageEvent):
        """查看我的所有在监预售商品清单"""
        user_id = event.get_sender_id()
        subs = self.db.get_user_subscriptions(user_id)
        yield event.plain_result(self.notifier.format_user_subscriptions(subs))

    @filter.command("标记补款")
    async def cmd_mark_done(self, event: AstrMessageEvent, item_or_id: str):
        """
        将商品标记为已完成补款。
        用法：/标记补款 101 或 /标记补款 初音韶华
        """
        user_id = event.get_sender_id()
        subs = self.db.get_user_subscriptions(user_id)
        target_sub = None

        if item_or_id.isdigit():
            sid = int(item_or_id)
            target_sub = next((s for s in subs if s["id"] == sid), None)

        if not target_sub:
            target_sub = next((s for s in subs if item_or_id.lower() in s["item_name"].lower()), None)

        if not target_sub:
            yield event.plain_result(f"未在您的追踪清单中找到【{item_or_id}】。")
            return

        self.db.update_subscription_status(target_sub["id"], status="completed")
        yield event.plain_result(f"🎉 已将【{target_sub['item_name']}】标记为【已完成补款】！")

    @filter.command("近期补款")
    async def cmd_recent_replenish(self, event: AstrMessageEvent, days: int = 7):
        """查看近期（默认7天内）各大店铺的补款公告。用法：/近期补款 或 /近期补款 7"""
        d = max(1, int(days)) if str(days).isdigit() else 7
        notices = self.db.get_recent_notices(days=d, notice_type="replenish")
        yield event.plain_result(self.notifier.format_recent_notices(notices, days=d, notice_type="replenish"))

    @filter.command("今日补款")
    async def cmd_today_replenish(self, event: AstrMessageEvent, days: int = 1):
        """查看今日（或指定天数内）最新开补情报。用法：/今日补款 或 /今日补款 3"""
        d = max(1, int(days)) if str(days).isdigit() else 1
        notices = self.db.get_recent_notices(days=d, notice_type="replenish")
        yield event.plain_result(self.notifier.format_recent_notices(notices, days=d, notice_type="replenish"))

    @filter.command("近期开订")
    async def cmd_recent_preorder(self, event: AstrMessageEvent, days: int = 7):
        """查看近期（默认7天内）各大店铺的新开预订手办新品。用法：/近期开订 或 /近期开订 7"""
        d = max(1, int(days)) if str(days).isdigit() else 7
        notices = self.db.get_recent_notices(days=d, notice_type="new_preorder")
        yield event.plain_result(self.notifier.format_recent_notices(notices, days=d, notice_type="new_preorder"))

    @filter.command("今日开订")
    async def cmd_today_preorder(self, event: AstrMessageEvent, days: int = 1):
        """查看今日最新开订情报。用法：/今日开订 或 /今日开订 3"""
        d = max(1, int(days)) if str(days).isdigit() else 1
        notices = self.db.get_recent_notices(days=d, notice_type="new_preorder")
        yield event.plain_result(self.notifier.format_recent_notices(notices, days=d, notice_type="new_preorder"))

    # ==================== 自然语言 LLM Tools ====================

    @filter.llm_tool(name="subscribe_preorder")
    async def tool_subscribe_preorder(
        self,
        event: AstrMessageEvent,
        shop_name: str,
        item_name: str,
        deposit: float = 0.0,
        estimated_month: str = ""
    ) -> str:
        """订阅某个店铺的某件手办或周边的预售补款提醒。

        当用户在自然语言中表达如“帮我盯一下猫受屋的初音韶华手办补款”、“我在GSC官方订了黏土人芙莉莲定金30”时调用此工具。

        Args:
            shop_name(string): 购买的店铺名称（如 猫受屋、GSC、B站会员购、某淘宝小店）
            item_name(string): 预订的手办或周边商品名称（如 初音韶华、黏土人芙莉莲）
            deposit(number): 已支付的定金金额（元），未提及则为 0.0
            estimated_month(string): 预估补款月份（如 10月、2026-11），未提及则留空
        """
        user_id = event.get_sender_id()
        origin = event.unified_msg_origin
        target_type = "group" if event.message_obj.group_id else "private"

        self.db.add_subscription(
            user_id=user_id,
            item_name=item_name,
            shop_name=shop_name,
            target_type=target_type,
            target_id=origin,
            deposit_amount=deposit,
            estimated_month=estimated_month
        )
        return f"已成功为用户记录并开启【{shop_name}】的【{item_name}】补款监控！"

    @filter.llm_tool(name="list_shops")
    async def tool_list_shops(self, event: AstrMessageEvent) -> str:
        """查看当前记录的所有模玩店铺档案列表及其绑定的渠道（微博、微信公众号、QQ补款群）。

        当用户询问“有哪些店铺”、“查看店铺列表”、“我们记录了哪些店”时调用。
        """
        shops = self.db.get_all_shops()
        return self.notifier.format_shop_list(shops)

    @filter.llm_tool(name="add_shop")
    async def tool_add_shop(
        self,
        event: AstrMessageEvent,
        shop_name: str,
        qq_group: str = "",
        weibo_uid: str = "",
        wechat_account: str = "",
        aliases: str = ""
    ) -> str:
        """添加或绑定一家模玩店铺的情报渠道。

        当用户说“添加店铺 猫受屋 补款群720694396”、“把猫受屋的微博绑定为12345678”时调用。

        Args:
            shop_name(string): 店铺名称
            qq_group(string): 绑定的补款QQ群号（多个群用逗号分隔）
            weibo_uid(string): 绑定的微博UID或主页数字
            wechat_account(string): 绑定的微信公众号名称
            aliases(string): 店铺别名（多个用逗号分隔）
        """
        if not self._is_admin(event.get_sender_id()):
            return "权限不足，只有管理员可以添加店铺档案。"

        groups = [g.strip() for g in qq_group.split(",") if g.strip()] if qq_group else []
        alias_list = [a.strip() for a in aliases.split(",") if a.strip()] if aliases else []

        self.db.add_shop(
            name=shop_name,
            aliases=alias_list,
            weibo_uid=weibo_uid,
            wechat_account=wechat_account,
            qq_groups=groups
        )
        shop_obj = self.db.get_shop_by_name(shop_name)
        if shop_obj:
            asyncio.create_task(self.scheduler.poll_single_shop(shop_obj))
        return f"已成功更新/录入店铺【{shop_name}】的情报渠道！已触发初次数据拉取。"

    @filter.llm_tool(name="query_my_preorders")
    async def tool_query_my_preorders(self, event: AstrMessageEvent) -> str:
        """查看当前用户所有在监的手办模玩预售与补款清单。

        当用户询问“查我的补款”、“我订了哪些手办”、“看看我的预定”时调用。
        """
        user_id = event.get_sender_id()
        subs = self.db.get_user_subscriptions(user_id)
        return self.notifier.format_user_subscriptions(subs)

    @filter.llm_tool(name="mark_preorder_completed")
    async def tool_mark_preorder_completed(self, event: AstrMessageEvent, item_name_or_id: str) -> str:
        """将某件预订商品标记为已完成补款。

        当用户说“我已经补完初音韶华了”、“把101号商品标记已补款”时调用。

        Args:
            item_name_or_id(string): 商品名称或商品ID
        """
        user_id = event.get_sender_id()
        subs = self.db.get_user_subscriptions(user_id)
        target = None
        if item_name_or_id.isdigit():
            sid = int(item_name_or_id)
            target = next((s for s in subs if s["id"] == sid), None)
        if not target:
            target = next((s for s in subs if item_name_or_id.lower() in s["item_name"].lower()), None)

        if not target:
            return f"未找到符合【{item_name_or_id}】的预订商品。"

        self.db.update_subscription_status(target["id"], status="completed")
        return f"已成功将【{target['item_name']}】标记为已完成补款！"

    @filter.llm_tool(name="query_recent_notices")
    async def tool_query_recent_notices(
        self,
        event: AstrMessageEvent,
        days: int = 7,
        notice_type: str = "replenish",
        shop_name: str = ""
    ) -> str:
        """查询近期（如7天内、本周、3天内或今日）各大模玩店铺或指定店铺发布的补款公告或新开预订手办情报。

        当用户询问“查一下最近7天的补款”、“本周有什么补款”、“近几天有哪些新开预定”、“gsc近期有补款通知，请你搜索”、“猫受屋最近补款”等时调用。

        Args:
            days(number): 查询最近几天内的情报，默认 7（例如 7 表示最近一周，1 表示今日）
            notice_type(string): 查询类型，可选 'replenish' (补款) 或 'new_preorder' (新开预订)
            shop_name(string): 可选，指定店铺名称或别名（如 'GSC'、'良笑'、'猫受屋'），留空则查询所有监控店铺
        """
        d = max(1, int(days)) if str(days).isdigit() else 7
        target_shop = None
        if shop_name:
            target_shop = self.db.get_shop_by_name(shop_name)
            if not target_shop:
                return f"系统监控库中未找到名为【{shop_name}】的店铺档案。您可以先使用指令 `/添加店铺 {shop_name}` 将其加入监控。"

        filter_shop_name = target_shop["name"] if target_shop else ""
        notices = self.db.get_recent_notices(days=d, notice_type=notice_type, shop_name=filter_shop_name)
        extra_hint = ""

        if target_shop and not notices:
            # 本地无记录时，尝试即时增量拉取一次
            refresh_res = await self.scheduler.refresh_shop(target_shop["name"])
            c_status = refresh_res.get("channel_status", {})
            wb_s = c_status.get("weibo")
            wx_s = c_status.get("wechat")
            hints = []
            if wb_s and wb_s.get("status") == "need_cookie":
                hints.append("该店铺微博触发反爬验证(HTTP 432/需登录)，需配置微博Cookie（私聊发送 `/设置微博cookie <cookie>`）")
            if wx_s and wx_s.get("status") == "need_service":
                hints.append(f"该店铺绑定了微信公众号【{target_shop.get('wechat_account')}】，但系统未配置公众号抓取服务(wechat_rss_base_url)。微信官方禁止外部免登录爬虫，请配置WeWe-RSS服务，或直接把文章链接发送给Bot解析")

            if refresh_res.get("new_count", 0) > 0:
                notices = self.db.get_recent_notices(days=d, notice_type=notice_type, shop_name=filter_shop_name)
            elif hints:
                extra_hint = "；".join(hints)
            else:
                extra_hint = "已即时检查网络渠道，但该店铺近几天尚未发布或抓取到相关情报。"

        return self.notifier.format_recent_notices(
            notices,
            days=d,
            notice_type=notice_type,
            shop_name=target_shop["name"] if target_shop else shop_name,
            extra_hint=extra_hint
        )

    @filter.llm_tool(name="get_latest_shop_notice")
    async def tool_get_latest_shop_notice(
        self,
        event: AstrMessageEvent,
        shop_name: str
    ) -> str:
        """查询指定店铺在系统记录中的最新一篇公告或上一篇文章内容与发布时间（当用户询问“上一篇文章是什么时候”、“上一篇内容有什么”、“最新发布的文章是什么”等时调用）。

        Args:
            shop_name(string): 店铺名称或别名（如 '淘模玩'、'GSC'、'猫受屋'）
        """
        target_shop = self.db.get_shop_by_name(shop_name)
        if not target_shop:
            return f"未找到名为【{shop_name}】的店铺档案。"

        notice = self.db.get_latest_notice_for_shop(target_shop["name"])
        if notice:
            dt = notice.get("created_at") or "未知时间"
            title = notice.get("title") or "无标题"
            content = notice.get("content") or ""
            channel = notice.get("channel", "未知渠道")
            ch_desc = "微信公众号" if channel == "wechat" else ("官方微博" if channel == "weibo" else channel)
            return (
                f"📰【{target_shop['name']}】的最新记录公告/文章详情：\n"
                f"• 发布时间：{dt}\n"
                f"• 来源渠道：{ch_desc}\n"
                f"• 标题：{title}\n"
                f"• 正文摘要：\n{content[:600]}\n"
                f"{'...(正文已截断)' if len(content) > 600 else ''}"
            )
        else:
            wx_account = target_shop.get("wechat_account", "")
            wb_uid = target_shop.get("weibo_uid", "")
            hints = []
            if wx_account and not self.config.get("wechat_rss_base_url"):
                hints.append(f"该店绑定了公众号【{wx_account}】，但系统未配置公众号抓取服务(wechat_rss_base_url)。微信官方禁止外部免登录爬虫，请配置WeWe-RSS服务，或直接把文章链接发送给Bot进行即时解析")
            if wb_uid and not self.config.get("weibo_cookie") and not self.weibo_channel.custom_cookie:
                hints.append(f"该店绑定了微博UID【{wb_uid}】，因微博432风控需配置Cookie（私聊发送 /设置微博cookie）")

            hint_str = "；".join(hints) if hints else "该店铺目前尚未成功拉取到历史博文或文章"
            return f"数据库中暂无【{target_shop['name']}】的历史文章/公告记录。\n💡 原因诊断：{hint_str}。"

    @filter.llm_tool(name="parse_wechat_article")
    async def tool_parse_wechat_article(
        self,
        event: AstrMessageEvent,
        url: str,
        shop_name: str = ""
    ) -> str:
        """解析单篇微信公众号文章链接（形如 https://mp.weixin.qq.com/s/...），提取补款或新开预订情报并自动推送给在监买家。当用户发送微信文章链接或要求解析微信推文时调用。

        Args:
            url(string): 微信公众号文章链接（需以 mp.weixin.qq.com 开头）
            shop_name(string): 可选，文章所属的店铺名称（如 '淘模玩'、'猫受屋'），留空则自动识别
        """
        if not ("mp.weixin.qq.com" in url or "weixin.qq.com" in url):
            return "提供的链接不是有效的微信公众号文章链接（需以 mp.weixin.qq.com 开头）。"

        article = await self.wechat_channel.parse_article_url(url)
        if not article or not article.get("title"):
            return "抓取微信文章失败，可能链接已失效或微信临时限制访问。"

        title = article.get("title", "")
        content = article.get("content", "")

        target_shop = None
        if shop_name:
            target_shop = self.db.get_shop_by_name(shop_name)
        if not target_shop:
            for s in self.db.get_all_shops():
                if s["name"] in title or s["name"] in content:
                    target_shop = s
                    break

        final_shop_name = target_shop["name"] if target_shop else (shop_name.strip() or "公众号店铺")
        notice_type = self.matcher.classify_notice(title, content)
        deadline = self.matcher.extract_deadline(content)

        notice_data = {
            "shop_id": target_shop["id"] if target_shop else None,
            "shop_name": final_shop_name,
            "channel": "wechat",
            "source_id": f"wechat_manual_{abs(hash(url))}",
            "title": title,
            "content": f"{title}\n\n{content}",
            "source_url": url
        }
        await self.scheduler.process_single_notice(notice_data)

        type_desc = "补款通知" if notice_type == "replenish" else "新开预订"
        return (
            f"✅ 成功解析微信公众号文章！\n"
            f"• 标题：{title}\n"
            f"• 所属店铺：{final_shop_name}\n"
            f"• 情报类型：{type_desc}\n"
            f"• 截止时间：{deadline or '文中未明确'}\n"
            f"已将该文内容入库，并自动完成了对在监商品的比对与推送！"
        )

    @filter.command("解析文章")
    async def cmd_parse_article(self, event: AstrMessageEvent, url: str, shop_name: str = ""):
        """解析单篇微信公众号文章并提取补款情报。用法：/解析文章 https://mp.weixin.qq.com/s/... [淘模玩]"""
        yield event.plain_result(await self.tool_parse_wechat_article(event, url=url, shop_name=shop_name))

    @filter.regex(r"https?://mp\.weixin\.qq\.com/s/[\w\-]+")
    async def on_wechat_link_received(self, event: AstrMessageEvent):
        """自动捕获消息中出现的微信公众号文章链接并解析入库"""
        match = re.search(r"https?://mp\.weixin\.qq\.com/s/[\w\-]+", event.message_str or "")
        if match:
            url = match.group(0)
            yield event.plain_result("🔍 检测到微信文章链接，正在抓取解析补款情报...")
            res = await self.tool_parse_wechat_article(event, url=url)
            yield event.plain_result(res)

    @filter.llm_tool(name="ocr_notice_image")
    async def tool_ocr_notice_image(
        self,
        event: AstrMessageEvent,
        image_url: str,
        shop_name: str = ""
    ) -> str:
        """从手办模玩补款海报、排期长图或截图中通过本地轻量OCR提取商品信息与补款截止时间并自动比对提醒。

        Args:
            image_url(string): 图片的网络URL或本地路径
            shop_name(string): 可选，图片所属店铺名称（如 '淘模玩'、'猫受屋'），留空则自动识别
        """
        try:
            from .core.ocr_helper import OCRHelper
        except (ImportError, ValueError):
            from core.ocr_helper import OCRHelper
        if not OCRHelper.is_available():
            return "本地 OCR 引擎未就绪或未安装 rapidocr_onnxruntime。"

        ocr_text = await OCRHelper.extract_text_from_url(image_url)
        if not ocr_text:
            return "未能从图片中提取到清晰的文字信息，可能图片过小或画质模糊。"

        target_shop = None
        if shop_name:
            target_shop = self.db.get_shop_by_name(shop_name)
        if not target_shop:
            for s in self.db.get_all_shops():
                if s["name"] in ocr_text:
                    target_shop = s
                    break

        final_shop_name = target_shop["name"] if target_shop else (shop_name.strip() or "截图店铺")
        notice_type = self.matcher.classify_notice(ocr_text[:50], ocr_text)
        deadline = self.matcher.extract_deadline(ocr_text)

        notice_data = {
            "shop_id": target_shop["id"] if target_shop else None,
            "shop_name": final_shop_name,
            "channel": "ocr_image",
            "source_id": f"image_{abs(hash(image_url))}",
            "title": f"【图文识别】{ocr_text.splitlines()[0] if ocr_text.splitlines() else '补款海报'}",
            "content": ocr_text,
            "source_url": image_url
        }
        await self.scheduler.process_single_notice(notice_data)

        type_desc = "补款通知" if notice_type == "replenish" else "新开预订"
        return (
            f"📷 本地 OCR 图片识别完成！\n"
            f"• 识别所属：{final_shop_name}\n"
            f"• 情报类型：{type_desc}\n"
            f"• 截止时间：{deadline or '图中未明确'}\n"
            f"• 识别内容摘要：\n{ocr_text[:300]}...\n\n"
            f"已自动入库并比对触发在监用户的补款提醒！"
        )

    @filter.command("识别补款图片")
    async def cmd_ocr_image(self, event: AstrMessageEvent, shop_name: str = ""):
        """从发送的图片或回复的图片中提取补款信息。用法：/识别补款图片 [店铺名]（需附带或回复图片）"""
        from astrbot.api.message_components import Image
        try:
            from .core.ocr_helper import OCRHelper
        except (ImportError, ValueError):
            from core.ocr_helper import OCRHelper

        images = []
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    images.append(comp)

        if not images:
            yield event.plain_result("请在发送指令时同时附带补款海报/长图，或回复某张图片发送指令。")
            return

        yield event.plain_result(f"🔍 正在通过本地轻量 OCR 分析 {len(images)} 张图片，请稍候...")

        import base64
        all_texts = []
        for idx, img in enumerate(images):
            try:
                b64 = await img.convert_to_base64()
                if b64:
                    raw_bytes = base64.b64decode(b64)
                    txt = await OCRHelper.extract_text_from_bytes(raw_bytes)
                    if txt:
                        all_texts.append(f"--- 图{idx + 1} ---\n{txt}")
            except Exception as e:
                logger.debug(f"[OCR] 识别单张组件图片异常: {e}")

        if not all_texts:
            yield event.plain_result("未能识别出图片中的有效文字，可能图片过小或内容模糊。")
            return

        full_content = "\n\n".join(all_texts)
        target_shop = self.db.get_shop_by_name(shop_name) if shop_name else None
        final_shop = target_shop["name"] if target_shop else (shop_name.strip() or "图片识别店铺")

        notice_data = {
            "shop_id": target_shop["id"] if target_shop else None,
            "shop_name": final_shop,
            "channel": "ocr_image",
            "source_id": f"image_{abs(hash(full_content[:100]))}",
            "title": f"【图文识别】{full_content.splitlines()[0] if full_content.splitlines() else '补款海报'}",
            "content": full_content,
            "source_url": ""
        }
        await self.scheduler.process_single_notice(notice_data)

        yield event.plain_result(
            f"✅ 本地 OCR 图片识别完成并已入库！\n"
            f"• 店铺：{final_shop}\n"
            f"• 识别文字量：{len(full_content)} 字\n"
            f"• 识别摘要：\n{full_content[:260]}...\n\n"
            f"已自动完成对群友订阅手办的匹配与推送！"
        )

    @filter.llm_tool(name="query_today_notices")
    async def tool_query_today_notices(
        self,
        event: AstrMessageEvent,
        notice_type: str = "replenish",
        shop_name: str = ""
    ) -> str:
        """查询今日各店铺最新的补款公告或新开预订手办情报。

        Args:
            notice_type(string): 查询类型，可选 'replenish' (补款) 或 'new_preorder' (新开预订)
            shop_name(string): 可选，指定店铺名称或别名（如 'GSC'、'良笑'、'猫受屋'），留空则查询所有监控店铺
        """
        return await self.tool_query_recent_notices(event, days=1, notice_type=notice_type, shop_name=shop_name)

    @filter.llm_tool(name="record_manual_notice")
    async def tool_record_manual_notice(
        self,
        event: AstrMessageEvent,
        shop_name: str,
        notice_type: str,
        content: str,
        source_url: str = ""
    ) -> str:
        """手动为店铺登记/补录一条补款公告或新开预订情报。当用户明确告知某店某日发布了什么补款/开订时调用。

        Args:
            shop_name(string): 店铺名称或别名（如 'GSC'、'良笑'、'猫受屋'）
            notice_type(string): 情报类型，'replenish' (补款通知) 或 'new_preorder' (新开预订)
            content(string): 公告正文内容或包含的具体补款商品详情
            source_url(string): 可选，情报原链接或说明
        """
        ntype = "replenish" if ("补" in notice_type or "款" in notice_type) else "new_preorder"
        nid = self.db.record_manual_notice(
            shop_name=shop_name,
            notice_type=ntype,
            content=content,
            source_url=source_url
        )
        shop = self.db.get_shop_by_name(shop_name)
        asyncio.create_task(self.scheduler.process_single_notice({
            "id": nid,
            "shop_id": shop["id"] if shop else None,
            "shop_name": shop["name"] if shop else shop_name,
            "channel": "manual",
            "source_id": f"manual_{nid}",
            "title": content[:30],
            "content": content,
            "source_url": source_url
        }))
        desc = "补款公告" if ntype == "replenish" else "新开预订情报"
        return f"已成功为店铺【{shop_name}】登记该条{desc}，并已完成对所有在监用户的补款比对！"


