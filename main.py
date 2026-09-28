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
    "1.0.0"
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
            rss_base_url=self.config.get("wechat_rss_base_url", "")
        )
        self.wechat_channel = WeChatChannel(
            rss_base_url=self.config.get("wechat_rss_base_url", "")
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
        yield event.plain_result(f"✅ 成功录入/更新店铺【{name}】档案！\n发送 `/店铺列表` 可查看详情。")

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
        return f"已成功更新/录入店铺【{shop_name}】的情报渠道！"

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
        notice_type: str = "replenish"
    ) -> str:
        """查询近期（如7天内、本周、3天内或今日）各大模玩店铺发布的补款公告或新开预订手办情报。

        当用户询问“查一下最近7天的补款”、“本周有什么补款”、“近几天有哪些新开预定”、“今天有什么开补”等时调用。

        Args:
            days(number): 查询最近几天内的情报，默认 7（例如 7 表示最近一周，1 表示今日）
            notice_type(string): 查询类型，可选 'replenish' (补款) 或 'new_preorder' (新开预订)
        """
        d = max(1, int(days)) if str(days).isdigit() else 7
        notices = self.db.get_recent_notices(days=d, notice_type=notice_type)
        return self.notifier.format_recent_notices(notices, days=d, notice_type=notice_type)

    @filter.llm_tool(name="query_today_notices")
    async def tool_query_today_notices(self, event: AstrMessageEvent, notice_type: str = "replenish") -> str:
        """查询今日各店铺最新的补款公告或新开预订手办情报。

        Args:
            notice_type(string): 查询类型，可选 'replenish' (补款) 或 'new_preorder' (新开预订)
        """
        return await self.tool_query_recent_notices(event, days=1, notice_type=notice_type)
