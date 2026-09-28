import re
import difflib
from typing import List, Dict, Any, Tuple, Optional

REPLENISH_KEYWORDS = [
    "补款", "尾款", "到货通知", "已到货", "开补", "补余款", "补款清单", "尾款开启", "补款截止"
]

NEW_PREORDER_KEYWORDS = [
    "开定", "开启预售", "新开预订", "预售开始", "新品上架", "接受预定", "接受预订", "预订开始", "WF限定预定"
]

DEADLINE_PATTERNS = [
    r"(?:截止|截单|至|前)(?:时间|日期)?[:：\s]*((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s*\d{1,2}[:：点]\d{1,2}(?:分)?)?)",
    r"((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s*\d{1,2}[:：点]\d{1,2}(?:分)?)?)[\s]*(?:截止|截单|前补完)",
    r"补款期限[:：\s]*([^\n,，。]{4,30})"
]

class Matcher:
    def __init__(self, enable_llm: bool = True):
        self.enable_llm = enable_llm

    def classify_notice(self, title: str, content: str) -> str:
        """识别通知类型：replenish (补款), new_preorder (新开预订), other"""
        text = f"{title}\n{content}".lower()
        
        # 补款优先级最高
        for kw in REPLENISH_KEYWORDS:
            if kw in text:
                return "replenish"
                
        for kw in NEW_PREORDER_KEYWORDS:
            if kw in text:
                return "new_preorder"
                
        return "other"

    def extract_deadline(self, text: str) -> str:
        """从通知正文中提取补款截止时间"""
        for pat in DEADLINE_PATTERNS:
            match = re.search(pat, text)
            if match:
                res = match.group(1).strip()
                if len(res) >= 2:
                    return res
        return ""

    def match_item(self, item_name: str, text: str) -> Tuple[bool, float]:
        """
        判断商品名是否命中通知文本。
        返回 (是否命中, 匹配置信度 0~1)
        """
        item_clean = re.sub(r"[^\w\u4e00-\u9fa5]", "", item_name).lower()
        if not item_clean:
            return False, 0.0

        text_clean = re.sub(r"[^\w\u4e00-\u9fa5]", "", text).lower()

        # 1. 严格全词子串匹配
        if item_clean in text_clean:
            return True, 1.0

        # 2. 提取核心有效词素（去除常见版本、比例、品类修饰词）
        noise_pattern = r"(?:ver\.?|version|手办|模型|景品|黏土人|比例|dx版|限定版|标准版|特典版|1/4|1/6|1/7|1/8|1/12)"
        item_denoised = re.sub(noise_pattern, "", item_name, flags=re.IGNORECASE)
        denoised_clean = re.sub(r"[^\w\u4e00-\u9fa5]", "", item_denoised).lower()
        if denoised_clean and denoised_clean in text_clean:
            return True, 0.95

        # 3. 核心分词多词素交集匹配
        raw_parts = re.split(r"[\s\-_/·・]+", item_name)
        parts = []
        for p in raw_parts:
            p_sub = re.sub(r"[^\w\u4e00-\u9fa5]", "", p).strip().lower()
            p_sub = re.sub(noise_pattern, "", p_sub, flags=re.IGNORECASE)
            if len(p_sub) >= 2:
                parts.append(p_sub)

        if parts:
            matched_count = sum(1 for p in parts if p in text_clean)
            ratio = matched_count / len(parts)
            if ratio >= 0.6 or (len(parts) == 1 and matched_count == 1):
                return True, ratio

        # 4. 模糊滑动窗口相似度匹配
        target_cmp = denoised_clean if denoised_clean else item_clean
        window_size = len(target_cmp) + 6
        if len(text_clean) >= len(target_cmp):
            best_sim = 0.0
            for i in range(0, len(text_clean) - len(target_cmp) + 1, 2):
                window = text_clean[i:i + window_size]
                sim = difflib.SequenceMatcher(None, target_cmp, window).ratio()
                if sim > best_sim:
                    best_sim = sim
            if best_sim >= 0.70:
                return True, best_sim

        return False, 0.0

    def find_matches_in_subscriptions(
        self,
        notice_shop_name: str,
        notice_title: str,
        notice_content: str,
        subscriptions: List[Dict[str, Any]]
    ) -> List[Tuple[Dict[str, Any], float]]:
        """
        在活跃订阅列表中找出被该通知命中的条目。
        返回 [(subscription, score), ...]
        """
        full_text = f"{notice_title}\n{notice_content}"
        matches = []

        for sub in subscriptions:
            sub_shop = sub.get("shop_name", "").strip().lower()
            notice_shop = notice_shop_name.strip().lower()

            # 店铺不为空时，需店铺名能对上
            if sub_shop and notice_shop:
                if sub_shop not in notice_shop and notice_shop not in sub_shop:
                    continue

            item_name = sub.get("item_name", "")
            hit, score = self.match_item(item_name, full_text)
            if hit:
                matches.append((sub, score))

        # 按置信度从高到低排序
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches
