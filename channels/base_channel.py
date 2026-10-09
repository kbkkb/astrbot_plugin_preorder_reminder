import re
import datetime
from abc import ABC, abstractmethod
from email.utils import parsedate_to_datetime
from typing import List, Dict, Any

from bs4 import BeautifulSoup


class BaseChannel(ABC):
    @staticmethod
    def parse_xml(xml_text: str):
        """
        解析 RSS/Atom XML 文本。
        优先使用 lxml('xml')；环境未安装 lxml 时回退 html.parser，
        避免 beautifulsoup4 在无 lxml 环境下抛 FeatureNotFound 导致 RSS 通道失效。
        """
        try:
            return BeautifulSoup(xml_text, "xml")
        except Exception:
            return BeautifulSoup(xml_text, "html.parser")

    @staticmethod
    def find_child_ci(parent, name: str):
        """
        大小写不敏感地查找子标签。
        lxml('xml') 保留原始大小写（pubDate），html.parser 会转小写（pubdate），
        此方法兼容两种解析器，避免 RSS 时间等驼峰标签取不到值。
        """
        return (parent.find(name)
                or parent.find(name.lower())
                or parent.find(name.upper()))

    @staticmethod
    def normalize_time(raw: Any) -> str:
        """
        将各渠道返回的时间统一为 '%Y-%m-%d %H:%M:%S'。
        数据库的近期通知查询按字符串比较 created_at，格式不统一会导致时间窗口失效。
        支持：
          - 'Tue Sep 23 19:30:47 +0800 2026'（m.weibo.cn）
          - 'Wed, 20 Aug 2025 09:00:00 +0800'（RSS pubDate）
          - '2026-09-23' / '2026-09-23 10:00:00'
          - ISO 8601（PC ajax 部分字段）
        """
        if not raw:
            return ""
        s = str(raw).strip()
        if not s:
            return ""

        # 1. 已是标准格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        # 2. m.weibo.cn 格式: 'Tue Sep 23 19:30:47 +0800 2026'
        m = re.match(
            r"^\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+\+\d{4}\s+(\d{4})$", s)
        if m:
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            try:
                mon = months.index(m.group(1)) + 1
                return (f"{int(m.group(6)):04d}-{mon:02d}-{int(m.group(2)):02d} "
                        f"{int(m.group(3)):02d}:{m.group(4)}:{m.group(5)}")
            except ValueError:
                pass

        # 3. RSS pubDate (RFC 822/1123)
        try:
            dt = parsedate_to_datetime(s)
            if dt is not None:
                # 统一按北京时间口径（微博时间均为 +0800）
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass

        # 4. ISO 8601
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

        return ""

    @abstractmethod
    async def fetch_latest_notices(self, shop: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        拉取目标店铺的最新动态/通知。
        返回标准化字典列表：
        [
            {
                "shop_id": int,
                "shop_name": str,
                "channel": str,
                "source_id": str,
                "title": str,
                "content": str,
                "source_url": str,
                "created_at": str
            },
            ...
        ]
        """
        pass
