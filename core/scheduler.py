import asyncio
import logging
import datetime
from typing import Dict, Any, List, Optional

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

class Scheduler:
    def __init__(
        self,
        db,
        matcher,
        notifier,
        weibo_channel,
        wechat_channel,
        send_message_func,
        poll_interval_minutes: int = 15,
        daily_digest_enabled: bool = True,
        daily_digest_time: str = "09:30",
        default_notify_target_type: str = "private",
        default_notify_group_id: str = ""
    ):
        self.db = db
        self.matcher = matcher
        self.notifier = notifier
        self.weibo_channel = weibo_channel
        self.wechat_channel = wechat_channel
        self.send_message_func = send_message_func

        self.poll_interval = max(5, poll_interval_minutes) * 60
        self.daily_digest_enabled = daily_digest_enabled
        self.daily_digest_time = daily_digest_time.strip()
        self.default_notify_target_type = default_notify_target_type
        self.default_notify_group_id = default_notify_group_id.strip()

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._digest_task: Optional[asyncio.Task] = None
        self._last_digest_date = ""

    def start(self):
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        if self.daily_digest_enabled:
            self._digest_task = asyncio.create_task(self._digest_loop())
        logger.info("[Scheduler] 预售补款后台轮询与日报调度器已启动")

    def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._digest_task:
            self._digest_task.cancel()
        logger.info("[Scheduler] 调度器已停止")

    async def _poll_loop(self):
        # 启动后先等待 10 秒再执行首次轮询，避免与启动流程冲突
        await asyncio.sleep(10)
        while self._running:
            try:
                await self.poll_all_shops()
            except Exception as e:
                logger.error(f"[Scheduler] 渠道轮询执行异常: {e}", exc_info=True)

            await asyncio.sleep(self.poll_interval)

    async def poll_all_shops(self):
        """轮询所有店铺的微博与微信公众号情报"""
        shops = self.db.get_all_shops()
        if not shops:
            return

        logger.debug(f"[Scheduler] 开始轮询 {len(shops)} 家店铺的最新情报...")
        for shop in shops:
            try:
                notices = []
                # 1. 微博
                if shop.get("weibo_uid"):
                    wb_notices = await self.weibo_channel.fetch_latest_notices(shop)
                    if wb_notices:
                        notices.extend(wb_notices)

                # 2. 微信公众号
                if shop.get("wechat_account"):
                    wx_notices = await self.wechat_channel.fetch_latest_notices(shop)
                    if wx_notices:
                        notices.extend(wx_notices)

                for n in notices:
                    await self.process_single_notice(n)

            except Exception as e:
                logger.warning(f"[Scheduler] 检查店铺【{shop.get('name')}】失败: {e}")

            # 错峰请求，防止连续请求打死目标接口
            await asyncio.sleep(2)

    async def process_single_notice(self, notice: Dict[str, Any]):
        """处理单条新通知，检查并推送命中订阅"""
        source_id = notice.get("source_id")
        if not source_id or self.db.is_notice_processed(source_id):
            return

        title = notice.get("title", "")
        content = notice.get("content", "")
        shop_name = notice.get("shop_name", "")
        shop_id = notice.get("shop_id")
        channel = notice.get("channel", "")
        source_url = notice.get("source_url", "")

        notice_type = self.matcher.classify_notice(title, content)
        deadline = self.matcher.extract_deadline(content)

        # 获取该店铺相关的有效订阅
        active_subs = self.db.get_active_subscriptions(shop_name)
        matched_items = []

        if active_subs:
            matched_pairs = self.matcher.find_matches_in_subscriptions(
                shop_name, title, content, active_subs
            )

            for sub, score in matched_pairs:
                matched_items.append(sub.get("item_name"))
                # 如果是补款通知，发出强提醒并更新状态
                if notice_type == "replenish":
                    self.db.update_subscription_status(
                        sub["id"],
                        status="replenishing",
                        deadline=deadline,
                        source_url=source_url
                    )
                    
                    alert_msg = self.notifier.format_urgent_alert(sub, notice, deadline)
                    target_type = sub.get("target_type", "private")
                    target_id = sub.get("target_id") or sub.get("user_id")

                    try:
                        await self.send_message_func(target_type, target_id, alert_msg)
                        logger.info(f"[Scheduler] 🚀 成功向用户【{sub.get('user_id')}】推送【{sub.get('item_name')}】补款紧急提醒！")
                    except Exception as e:
                        logger.error(f"[Scheduler] 推送补款提醒给【{target_id}】失败: {e}")

        # 记录到数据库历史通知中
        self.db.add_notice(
            shop_id=shop_id,
            shop_name=shop_name,
            channel=channel,
            notice_type=notice_type,
            title=title,
            content=content,
            source_id=source_id,
            source_url=source_url,
            matched_items=matched_items
        )

    async def _digest_loop(self):
        """每日早报循环调度"""
        await asyncio.sleep(15)
        while self._running:
            try:
                now = datetime.datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                now_hm = now.strftime("%H:%M")

                if now_hm == self.daily_digest_time and self._last_digest_date != today_str:
                    self._last_digest_date = today_str
                    await self.send_daily_digest()
            except Exception as e:
                logger.error(f"[Scheduler] 每日早报发送异常: {e}")

            await asyncio.sleep(40)

    async def send_daily_digest(self, force_target_id: Optional[str] = None, force_target_type: Optional[str] = None):
        """汇总生成并推送每日早报"""
        replenish_notices = self.db.get_recent_notices(days=1, notice_type="replenish")
        new_preorder_notices = self.db.get_recent_notices(days=1, notice_type="new_preorder")

        digest_text = self.notifier.format_daily_digest(replenish_notices, new_preorder_notices)

        if force_target_id:
            await self.send_message_func(force_target_type or "private", force_target_id, digest_text)
            return

        # 默认推送：推送到默认群或有在监商品的用户
        if self.default_notify_group_id:
            await self.send_message_func("group", self.default_notify_group_id, digest_text)
        else:
            # 推送给所有有等待补款商品的用户
            active_subs = self.db.get_active_subscriptions()
            notified_users = set()
            for sub in active_subs:
                uid = sub.get("user_id")
                if uid and uid not in notified_users:
                    notified_users.add(uid)
                    try:
                        await self.send_message_func("private", uid, digest_text)
                    except Exception as e:
                        logger.debug(f"[Scheduler] 推送早报给用户【{uid}】失败: {e}")
