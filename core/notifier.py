from typing import List, Dict, Any

class Notifier:
    @staticmethod
    def format_urgent_alert(
        sub: Dict[str, Any],
        notice: Dict[str, Any],
        deadline: str = ""
    ) -> str:
        """格式化单条紧急补款提醒"""
        channel_names = {
            "weibo": "微博动态",
            "wechat": "微信公众号推文",
            "qq_group": "QQ补款群公告/消息"
        }
        channel_str = channel_names.get(notice.get("channel"), notice.get("channel", "情报源"))
        deadline_str = deadline or notice.get("replenish_deadline") or "以店铺通知为准"
        
        lines = [
            "🚨【手办模玩 · 紧急补款通知】🚨",
            "--------------------------------",
            f"📦 预订商品：{sub.get('item_name')}",
            f"🏬 对应店铺：{notice.get('shop_name') or sub.get('shop_name') or '未指定'}",
            f"⏰ 补款截止：{deadline_str}",
            f"📡 情报来源：{channel_str}",
        ]

        if notice.get("title"):
            lines.append(f"📌 通知标题：{notice['title']}")

        if notice.get("source_url"):
            lines.append(f"🔗 原文链接：{notice['source_url']}")

        lines.extend([
            "--------------------------------",
            "💡 提示：许多模玩店补款期仅有7-15天，请尽快前往淘宝搜索对应店铺完成补款，避免定金失效！"
        ])
        return "\n".join(lines)

    @staticmethod
    def format_shop_list(shops: List[Dict[str, Any]]) -> str:
        """格式化所有记录的店铺列表"""
        if not shops:
            return (
                "🏪【模玩店铺档案库】\n"
                "当前暂未登记任何店铺信息。\n"
                "💡 您可以通过：@Bot 添加店铺 [店铺名] 微博UID:xxx 公众号:xxx 补款群:xxx 来录入店铺！"
            )

        lines = [
            f"🏪【模玩店铺档案库】(共 {len(shops)} 家)",
            "================================"
        ]

        for idx, s in enumerate(shops, 1):
            sub_count = s.get("sub_count", 0)
            lines.append(f"{idx}. 🏬 【{s['name']}】 (追踪商品: {sub_count}件)")
            
            # 渠道状态
            channels = []
            if s.get("weibo_uid"):
                channels.append(f"微博[UID:{s['weibo_uid']}]")
            if s.get("wechat_account"):
                channels.append(f"公众号[{s['wechat_account']}]")
            if s.get("qq_groups"):
                channels.append(f"QQ群[{', '.join(s['qq_groups'])}]")
                
            ch_desc = " | ".join(channels) if channels else "未绑定情报渠道"
            lines.append(f"   📡 监控通道: {ch_desc}")
            
            if s.get("aliases"):
                lines.append(f"   🏷️ 别名: {', '.join(s['aliases'])}")
            if s.get("notes"):
                lines.append(f"   📝 备注: {s['notes']}")
            lines.append("")

        lines.append("💡 发送 `@Bot 订阅 [店铺名] [商品名]` 即可开启该店补款监控！")
        return "\n".join(lines).strip()

    @staticmethod
    def format_user_subscriptions(subs: List[Dict[str, Any]]) -> str:
        """格式化用户的预售订阅追踪清单"""
        if not subs:
            return (
                "📋【我的预售追踪清单】\n"
                "您当前没有任何正在追踪的预售商品。\n"
                "💡 发送：`@Bot 帮我盯着【猫受屋】的【初音韶华手办】补款` 即可添加订阅！"
            )

        waiting = [s for s in subs if s.get("status") == "waiting"]
        replenishing = [s for s in subs if s.get("status") == "replenishing"]
        completed = [s for s in subs if s.get("status") == "completed"]

        lines = [
            f"📋【我的预售追踪清单】(共 {len(subs)} 项)",
            "================================"
        ]

        if replenishing:
            lines.append("🚨【⚠️ 正在开补中 - 请尽快补款！】")
            for s in replenishing:
                dl = f" | 截止: {s.get('replenish_deadline')}" if s.get("replenish_deadline") else ""
                lines.append(f" • [ID:{s['id']}] 【{s['item_name']}】 @ {s['shop_name']}{dl}")
            lines.append("")

        if waiting:
            lines.append("⏳【等待补款中】")
            for s in waiting:
                dep = f" | 定金: ¥{s['deposit_amount']}" if s.get("deposit_amount") else ""
                est = f" | 预计: {s['estimated_month']}" if s.get("estimated_month") else ""
                lines.append(f" • [ID:{s['id']}] 【{s['item_name']}】 @ {s['shop_name'] or '全网监控'}{dep}{est}")
            lines.append("")

        if completed:
            lines.append(f"✅ 已完成补款 ({len(completed)} 项)")

        lines.extend([
            "--------------------------------",
            "💡 指令提示：",
            "• 完成补款：`@Bot 标记 [ID/商品名] 已补款`",
            "• 取消监控：`@Bot 取消订阅 [ID]`"
        ])
        return "\n".join(lines).strip()

    @staticmethod
    def format_daily_digest(
        replenish_notices: List[Dict[str, Any]],
        new_preorder_notices: List[Dict[str, Any]]
    ) -> str:
        """格式化每日早报"""
        import datetime
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        lines = [
            f"📢【模玩预售与补款早报】({today_str})",
            "================================"
        ]

        if replenish_notices:
            lines.append(f"🚨【今日最新补款情报】({len(replenish_notices)} 条)")
            for n in replenish_notices[:8]:
                shop = n.get("shop_name", "小店")
                title = n.get("title") or n.get("content")[:35].replace("\n", " ")
                lines.append(f" • [{shop}] {title}")
            lines.append("")
        else:
            lines.append("🍵 今日各大监控店铺暂无最新开补公告。")
            lines.append("")

        if new_preorder_notices:
            lines.append(f"🛒【今日新开预订手办/周边】({len(new_preorder_notices)} 条)")
            for n in new_preorder_notices[:8]:
                shop = n.get("shop_name", "店铺")
                title = n.get("title") or n.get("content")[:35].replace("\n", " ")
                lines.append(f" • [{shop}] {title}")
            lines.append("")

        lines.append("💡 发送 `@Bot 查我的补款清单` 检查您名下的在监商品！")
        return "\n".join(lines).strip()

    @staticmethod
    def format_recent_notices(
        notices: List[Dict[str, Any]],
        days: int = 7,
        notice_type: str = "replenish"
    ) -> str:
        """格式化近期（如7天内）的补款或开订汇总列表"""
        type_desc = "补款情报" if notice_type == "replenish" else "新开预订"
        icon = "🚨" if notice_type == "replenish" else "🛒"
        period_desc = "今日" if days == 1 else f"近 {days} 天"
        if not notices:
            return f"🍵 {period_desc}各大监控店铺暂无最新{type_desc}。"

        lines = [
            f"{icon}【{period_desc}模玩{type_desc}汇总】(共 {len(notices)} 条)",
            "================================"
        ]
        for n in notices[:25]:
            shop = n.get("shop_name", "小店")
            dt = (n.get("created_at") or "")[:10]  # YYYY-MM-DD
            time_tag = f"[{dt}] " if dt and days > 1 else ""
            title = n.get("title") or n.get("content")[:35].replace("\n", " ")
            lines.append(f" • {time_tag}[{shop}] {title}")

        if len(notices) > 25:
            lines.append(f"... 仅展示前 25 条，共 {len(notices)} 条记录")

        lines.extend([
            "--------------------------------",
            "💡 提示：使用 `@Bot 查我的补款清单` 检查您名下的在监商品是否在列！"
        ])
        return "\n".join(lines).strip()
