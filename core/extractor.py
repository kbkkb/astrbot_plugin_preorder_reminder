import re
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

# ==================== 补款/开订情报结构化提取 ====================
# 面向微信推文与 OCR 海报识别的纯文本，提取：
#   商品名、定金、尾款/全款、补款截止日期
# 兼容三种常见排版：
#   1. 行内混排：「初音韶华 定金100 尾款899 截止10月10日」
#   2. 海报拆行：「初音韶华手办」/「全款 999」/「截单日期 10.10」
#   3. 表格 OCR：「商品名称 定金 尾款 截止」/「初音韶华 100 899 10.10」

_NUM = r"\d+(?:\.\d+)?"

DEPOSIT_PATTERNS = [
    rf"(?:定金|订金|首付|预付款)\s*[¥￥]?\s*({_NUM})",
    rf"[¥￥]\s*({_NUM})\s*(?:定金|订金)",
]
FINAL_PAY_PATTERNS = [
    rf"(?:尾款|补款|余款|全款|总价|合计)\s*[¥￥]?\s*({_NUM})",
    rf"[¥￥]\s*({_NUM})\s*(?:尾款|补款|全款)",
]
GENERIC_PRICE_PATTERNS = [
    rf"[¥￥]\s*({_NUM})",
    rf"({_NUM})\s*(?:元|块|rmb|RMB)",
]

# 截止/截单日期（与 Matcher 保持一致的宽松口径）
DEADLINE_PATTERNS = [
    r"(?:截止|截单|补款截止|补款期限|截止时间|截单时间|截单日期)(?:时间|日期)?[:：\s]*"
    r"((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s*\d{1,2}[:：点]\d{1,2}(?:分)?)?)",
    r"((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?(?:\s*\d{1,2}[:：点]\d{1,2}(?:分)?)?)\s*(?:截止|截单|前补完)",
    r"(?:截止|截单|补款期限)[:：\s]*([^\n,，。;；|]{2,24})",
]

# 表头/噪音行（不应被当作商品名）
_HEADER_TOKENS = ("商品名称", "商品名", "产品名称", "品名", "名称", "商品", "产品",
                  "定金", "尾款", "补款", "全款", "价格", "售价", "截单", "截止",
                  "日期", "时间", "数量", "序号", "编号", "比例", "厂商")
_NOISE_LINE_RE = re.compile(
    r"^(?:阅读原文|长按识别|扫码关注|点击上方|关注我们|欢迎转发|转载请注明|"
    r"戳|阅读|分享|点赞|在看|点击|扫描|二维码|微信号|公众号|"
    r"\d+\s*[\-—–]+\s*\d*$|[-—–=_*·\s|]+$)", re.IGNORECASE)


def _to_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _clean_product(raw: str) -> str:
    """清洗商品名候选串"""
    s = raw.strip()
    # 去掉行首序号 / 项目符号
    s = re.sub(r"^\s*(?:[\d一二三四五六七八九十]{1,3}[\.、\s\)）:：]|[•·▪◦\-–—*]+)\s*", "", s)
    # 去掉表格残留分隔符
    s = re.sub(r"[|｜]+", " ", s)
    # 去掉常见标签括号内容外的杂号
    s = re.sub(r"\s{2,}", " ", s).strip(" -—–_·:：,，、")
    # 去掉字段前缀
    s = re.sub(r"^(?:商品名称|商品名|产品名称|品名|名称|商品|产品)\s*[:：]?\s*", "", s)
    return s.strip()
def _core_product_name(raw: str) -> str:
    """从商品名候选中提炼核心名称：去掉比例、版本、品类等修饰后缀"""
    s = _clean_product(raw)
    if not s:
        return ""
    # 1. 去掉尾部修饰语（比例/版本/品类/材质/套装）
    s = re.sub(
        r"[\s,，、]*(?:\d+\s*[/\\]\s*\d+\s*(?:比例)?|(?:ver|version)\.?\s*[\w.]*|"
        r"(?:dx|DX)版|限定版|特别版|特典版|标准版|普通版|豪华版|再版|复刻版|"
        r"手办|模型|景品|黏土人|粘土人|可动人形|figma|FIGURE|Figure|"
        r"周边|挂件|钥匙扣|徽章|立牌|色纸|海报|套装|组套|预售|预定|预订|"
        r"gk|GK|树脂|PVC|pvc)+[\s,，、]*$", "", s).strip(" -—–_·:：,，、")
    # 2. 若仍含空格分隔的多段，取首段（核心角色/IP名）
    parts = [p for p in re.split(r"\s{1,}", s) if p]
    if len(parts) >= 2:
        s = parts[0]
    return s.strip()

def _is_noise_line(line: str) -> bool:
    if not line:
        return True
    if _NOISE_LINE_RE.search(line):
        return True
    # 去掉序号后过短
    if len(_clean_product(line)) < 2:
        return True
    return False


def _is_header_line(line: str) -> bool:
    """判断是否为表格表头行（如『商品名称 定金 尾款 截止』）"""
    cleaned = _clean_product(line)
    if not cleaned:
        return False
    hits = sum(1 for t in _HEADER_TOKENS if t in cleaned)
    # 含 2 个以上字段词且基本不含数字价格 → 视为表头
    digits = re.search(r"\d", cleaned)
    return hits >= 2 and not digits


class ReplenishExtractor:
    """从推文/OCR 纯文本中结构化提取补款与开订商品条目"""

    def __init__(self, max_items: int = 60):
        self.max_items = max_items

    # ---------- 单行解析 ----------

    def _match_first(self, patterns, text: str) -> float:
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return _to_float(m.group(1))
        return 0.0

    def _extract_deadline(self, text: str) -> str:
        for pat in DEADLINE_PATTERNS:
            m = re.search(pat, text)
            if m:
                res = m.group(1).strip()
                if 2 <= len(res) <= 24:
                    return res
        return ""

    def _split_name_and_rest(self, line: str) -> Tuple[str, str]:
        """把一行切成 (商品名候选, 其余部分)"""
        # 找到第一个价格/日期标记的位置，之前的部分视为商品名
        markers = []
        for pat in (DEPOSIT_PATTERNS + FINAL_PAY_PATTERNS + GENERIC_PRICE_PATTERNS
                    + [p for p in DEADLINE_PATTERNS if p.count("(") >= 1]):
            m = re.search(pat, line)
            if m:
                markers.append(m.start())
        # 日期关键词本身也应作为切分点
        for kw in ("截止", "截单", "补款期限", "尾款", "补款", "定金", "订金", "全款", "总价"):
            idx = line.find(kw)
            if idx > 0:
                markers.append(idx)
        if not markers:
            return line, ""
        cut = min(markers)
        return line[:cut], line[cut:]

    def _extract_bare_date(self, text: str) -> str:
        """识别本身就是日期的单元格/片段（如 '10月10日'、'10.10'、'2026-10-10'）"""
        s = text.strip()
        m = re.match(r"^((?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?)$", s)
        if m:
            return m.group(1)
        return ""

    def _extract_deadline(self, text: str) -> str:
        for pat in DEADLINE_PATTERNS:
            m = re.search(pat, text)
            if m:
                res = m.group(1).strip()
                if 2 <= len(res) <= 24:
                    return res
        return ""

    def _parse_line(self, line: str) -> Optional[Dict[str, Any]]:
        """解析单行，返回结构化条目（无有效信息则 None）"""
        line = line.strip()
        if not line or _is_noise_line(line) or _is_header_line(line):
            return None

        deposit = self._match_first(DEPOSIT_PATTERNS, line)
        final_pay = self._match_first(FINAL_PAY_PATTERNS, line)
        if not deposit and not final_pay:
            generic = self._match_first(GENERIC_PRICE_PATTERNS, line)
            if generic:
                final_pay = generic
        deadline = self._extract_deadline(line)

        name_part, _ = self._split_name_and_rest(line)
        product = _core_product_name(name_part) or _clean_product(name_part)

        # 无价格且无日期 → 可能只是商品名行（供与下一行价格配对）
        if not deposit and not final_pay and not deadline:
            if 2 <= len(product) <= 40:
                return {"product": product, "deposit": 0.0, "final_payment": 0.0,
                        "deadline": "", "pending": True}
            return None

        # 有价格/日期但商品名缺失 → 交由上层用上文最近的商品名行补齐
        if not product:
            return {"product": "", "deposit": deposit, "final_payment": final_pay,
                    "deadline": deadline, "pending": True}

        return {"product": product, "deposit": deposit, "final_payment": final_pay,
                "deadline": deadline, "pending": False}

    # ---------- 主入口 ----------

    def extract_items(self, text: str) -> List[Dict[str, Any]]:
        """从纯文本中提取结构化商品条目列表"""
        if not text or not text.strip():
            return []

        raw_lines = re.split(r"[\r\n]+", text)
        items: List[Dict[str, Any]] = []
        last_name = ""

        for raw in raw_lines:
            line = raw.strip()
            if not line:
                continue

            # 表格行：按 | / ｜ / 多个空格 / Tab 切分单元格后重组为「名称+价格+日期」
            cells = [c.strip() for c in re.split(r"[|｜]|\t| {2,}", line) if c.strip()]
            if len(cells) >= 3 and any(re.search(r"\d", c) for c in cells):
                # 识别表头并跳过
                if _is_header_line(line):
                    continue
                item = self._parse_row(cells)
                if item:
                    if item["pending"] and last_name:
                        item["product"] = last_name
                        item["pending"] = False
                    if item["product"]:
                        last_name = item["product"]
                    items.append(item)
                    if len(items) >= self.max_items:
                        break
                continue

            parsed = self._parse_line(line)
            if not parsed:
                continue

            if parsed["pending"] and parsed["product"]:
                # 纯名称行：记住，可能与后续价格行配对
                if not (parsed["deposit"] or parsed["final_payment"] or parsed["deadline"]):
                    last_name = parsed["product"]
                    continue

            if parsed["pending"] and not parsed["product"] and last_name:
                parsed["product"] = last_name
                parsed["pending"] = False

            if parsed["product"]:
                last_name = parsed["product"]
                # 拆行合并：若上一条已记录同一商品，则补充价格/日期而非新增条目
                if items and items[-1]["product"] == parsed["product"]:
                    prev = items[-1]
                    if parsed.get("deposit") and not prev.get("deposit"):
                        prev["deposit"] = parsed["deposit"]
                    if parsed.get("final_payment") and not prev.get("final_payment"):
                        prev["final_payment"] = parsed["final_payment"]
                    if parsed.get("deadline") and not prev.get("deadline"):
                        prev["deadline"] = parsed["deadline"]
                    continue
                items.append(parsed)
                if len(items) >= self.max_items:
                    break

        # 清理 pending 标记并去重（同名同价同日只保留一条）
        seen = set()
        result = []
        for it in items:
            if not it.get("product"):
                continue
            key = (it["product"], it.get("deposit", 0.0), it.get("final_payment", 0.0),
                   it.get("deadline", ""))
            if key in seen:
                continue
            seen.add(key)
            it.pop("pending", None)
            result.append(it)
        return result

    def _parse_row(self, cells: List[str]) -> Optional[Dict[str, Any]]:
        """解析表格单元格：找出名称列、价格列、日期列"""
        product, deposit, final_pay, deadline = "", 0.0, 0.0, ""

        for c in cells:
            if not product:
                cand = _clean_product(c)
                # 名称列：含中文/字母且非纯数字/纯日期
                if cand and re.search(r"[\u4e00-\u9fa5A-Za-z]", cand) \
                        and not re.match(r"^\s*[¥￥]?\s*\d+(?:\.\d+)?\s*(?:元|块)?\s*$", cand.strip()) \
                        and not re.match(r"^\s*(?:20\d{2}[-/.年])?\d{1,2}[-/.月]\d{1,2}[日号]?\s*$", cand.strip()):
                    product = _core_product_name(cand) or cand
                    continue
            d = self._match_first(DEPOSIT_PATTERNS, c)
            if d and not deposit:
                deposit = d
                continue
            f = self._match_first(FINAL_PAY_PATTERNS, c)
            if f and not final_pay:
                final_pay = f
                continue
            dl = self._extract_deadline(c) or self._extract_bare_date(c)
            if dl and not deadline:
                deadline = dl

        # 单元格本身是纯数字的价格列（无单位词）：按列位置推断，先出现的视为定金
        numeric_cells = []
        for c in cells:
            if re.match(r"^\s*[¥￥]?\s*\d+(?:\.\d+)?\s*(?:元|块)?\s*$", c):
                numeric_cells.append(_to_float(re.search(r"\d+(?:\.\d+)?", c).group(0)))
        if numeric_cells:
            if len(numeric_cells) >= 2:
                if not deposit:
                    deposit = numeric_cells[0]
                if not final_pay:
                    final_pay = numeric_cells[1]
            else:
                val = numeric_cells[0]
                if not final_pay and not deposit:
                    final_pay = val
                elif not deposit:
                    deposit = val
                elif not final_pay:
                    final_pay = val

        if not product or (not deposit and not final_pay and not deadline):
            return None
        return {"product": product, "deposit": deposit, "final_payment": final_pay,
                "deadline": deadline, "pending": False}

    # ---------- 订阅匹配辅助 ----------

    def match_subscription(self, item_name: str, products: List[str]) -> Tuple[bool, float]:
        """
        判断订阅商品名是否命中提取出的商品名列表。
        返回 (是否命中, 置信度)。相比全文子串匹配，基于已提取商品名更精准。
        """
        target = re.sub(r"\s+", "", item_name).lower()
        if not target:
            return False, 0.0
        best = 0.0
        for p in products:
            pc = re.sub(r"\s+", "", p).lower()
            if not pc:
                continue
            if target == pc:
                return True, 1.0
            if target in pc or pc in target:
                best = max(best, 0.9)
        return (best > 0, best)
