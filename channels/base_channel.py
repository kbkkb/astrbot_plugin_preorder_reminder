from abc import ABC, abstractmethod
from typing import List, Dict, Any

class BaseChannel(ABC):
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
