import re
import logging
from typing import Dict, Any, Optional, Tuple, List
from .base_channel import BaseChannel

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

class QQGroupChannel:
    def __init__(self, matcher):
        self.matcher = matcher

    def inspect_group_message(
        self,
        group_id: str,
        sender_id: str,
        sender_role: str,
        message_id: str,
        raw_text: str,
        monitored_map: Dict[str, List[Dict[str, Any]]]
    ) -> Tuple[bool, bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        嗅探群消息是否属于被监控的补款群。
        返回四元组：
        (
            is_monitored_group: bool,   # 是否属于被监控的群（若是，主程序应阻断闲聊/避免Bot发言）
            is_valid_notice: bool,      # 是否是有效补款/开订通知
            matched_shop: Optional,     # 对应的店铺档案
            notice_data: Optional       # 构建的标准通知数据
        )
        """
        gid = str(group_id).strip()
        if gid not in monitored_map:
            return False, False, None, None

        # 该群属于被监控店铺关联的补款群
        shops_in_group = monitored_map[gid]
        if not shops_in_group:
            return False, False, None, None

        sid = str(sender_id).strip()
        # 寻找命中的店铺
        target_shop = None
        for s in shops_in_group:
            admin_ids = [str(a).strip() for a in s.get("admin_qq_ids", [])]
            # 1. 如果指定了管理员 QQ 列表，必须在列表中
            if admin_ids:
                if sid in admin_ids:
                    target_shop = s
                    break
            else:
                # 2. 如果未指定具体 QQ，只允许群主(owner)或管理员(admin)发言触发
                if sender_role in ("owner", "admin"):
                    target_shop = s
                    break

        # 如果不是管理员/店主，直接认定为群友闲聊 -> 静默拦截，不处理
        if not target_shop:
            logger.debug(f"[QQGroupChannel] 补款群【{gid}】非管理发言({sid})，静默忽略")
            return True, False, None, None

        # 进一步检查内容是否属于真正的补款/开定通知
        clean_text = raw_text.strip()
        if len(clean_text) < 4:
            return True, False, target_shop, None

        notice_type = self.matcher.classify_notice("", clean_text)
        if notice_type not in ("replenish", "new_preorder"):
            logger.debug(f"[QQGroupChannel] 补款群【{gid}】管理员发言未检测到补款/开定特征，静默忽略: {clean_text[:30]}")
            return True, False, target_shop, None

        # 构成有效通知数据
        source_id = f"qq_{gid}_{message_id}"
        notice_data = {
            "shop_id": target_shop.get("id"),
            "shop_name": target_shop.get("name"),
            "channel": "qq_group",
            "source_id": source_id,
            "title": clean_text[:40].replace("\n", " "),
            "content": clean_text,
            "source_url": f"QQ群:{gid}",
            "created_at": ""
        }

        logger.info(f"[QQGroupChannel] 🎯 在补款群【{gid}】捕获到店铺【{target_shop.get('name')}】的有效通知: {notice_data['title']}")
        return True, True, target_shop, notice_data
