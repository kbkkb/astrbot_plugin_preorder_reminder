import re
import json
import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup
from .base_channel import BaseChannel

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

class WeChatChannel(BaseChannel):
    def __init__(self, rss_base_url: str = ""):
        self.rss_base_url = rss_base_url.rstrip("/")
        self.last_status: Dict[str, Dict[str, Any]] = {}

    def get_shop_status(self, shop: Dict[str, Any]) -> Dict[str, Any]:
        """获取店铺微信公众号渠道的最新状态诊断"""
        account = str(shop.get("wechat_account") or "").strip()
        if not account:
            return {"status": "no_account", "msg": "未配置公众号名称/ID"}
        if not self.rss_base_url:
            return {
                "status": "need_service",
                "msg": f"已绑定公众号【{account}】，但未配置抓取服务地址(wechat_rss_base_url)。微信官方禁止外部免登录爬虫，需配置自建 WeWe-RSS 服务，或直接将文章链接发给 Bot 解析。"
            }
        return self.last_status.get(account, {"status": "ok", "msg": "已连接 WeWe-RSS 服务"})


    def _clean_article_html(self, raw_html: str) -> str:
        """清洗公众号文章 HTML"""
        if not raw_html:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        soup = BeautifulSoup(text, "html.parser")
        # 移除无用标签
        for s in soup(["script", "style"]):
            s.decompose()
        clean = soup.get_text(separator=" ").strip()
        clean = re.sub(r"[ \t]+", " ", clean)
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean

    async def fetch_latest_notices(self, shop: Dict[str, Any]) -> List[Dict[str, Any]]:
        wechat_account = str(shop.get("wechat_account") or "").strip()
        if not wechat_account:
            return []

        results = []
        async with aiohttp.ClientSession() as session:
            # 策略 1: 走 WeWe-RSS / RSSHub
            if self.rss_base_url:
                try:
                    # 尝试常见的 WeWe-RSS / RSSHub 路由
                    endpoints = [
                        f"{self.rss_base_url}/feeds/{wechat_account}.xml",
                        f"{self.rss_base_url}/rss/{wechat_account}",
                        f"{self.rss_base_url}/wechat/mp/msghistory/{wechat_account}"
                    ]
                    for ep in endpoints:
                        try:
                            async with session.get(ep, timeout=12) as resp:
                                if resp.status == 200:
                                    xml_text = await resp.text()
                                    soup = BeautifulSoup(xml_text, "xml")
                                    items = soup.find_all("item")
                                    for item in items[:8]:
                                        title = item.title.text if item.title else ""
                                        desc = self._clean_article_html(item.description.text if item.description else "")
                                        link = item.link.text if item.link else ""
                                        guid = item.guid.text if item.guid else link or title
                                        results.append({
                                            "shop_id": shop.get("id"),
                                            "shop_name": shop.get("name"),
                                            "channel": "wechat",
                                            "source_id": f"wechat_{guid}",
                                            "title": title,
                                            "content": f"{title}\n\n{desc[:3000]}",  # 截取前3000字符避免过大
                                            "source_url": link,
                                            "created_at": item.pubDate.text if item.pubDate else ""
                                        })
                                    if results:
                                        return results
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"[WeChatChannel] RSS 轮询公众号【{wechat_account}】异常: {e}")

        return results

    async def parse_article_url(self, url: str) -> Optional[Dict[str, str]]:
        """直接解析用户发送的微信文章链接内容"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        soup = BeautifulSoup(html, "html.parser")
                        title_el = soup.find("h1", class_="rich_media_title")
                        content_el = soup.find("div", class_="rich_media_content")
                        
                        title = title_el.get_text().strip() if title_el else ""
                        content = self._clean_article_html(content_el.decode_contents()) if content_el else ""
                        return {
                            "title": title,
                            "content": content,
                            "url": url
                        }
        except Exception as e:
            logger.warning(f"[WeChatChannel] 解析单篇微信文章链接失败: {e}")
        return None
