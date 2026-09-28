import re
import json
import logging
import asyncio
import aiohttp
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base_channel import BaseChannel

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

class WeiboChannel(BaseChannel):
    def __init__(self, custom_cookie: str = "", rss_base_url: str = ""):
        self.custom_cookie = custom_cookie.strip()
        self.rss_base_url = rss_base_url.rstrip("/")
        self._visitor_sub: str = ""
        self._visitor_subp: str = ""

    async def _get_visitor_cookie(self, session: aiohttp.ClientSession) -> Tuple[str, str]:
        """获取微博访客临时凭据 (SUB / SUBP)"""
        if self._visitor_sub:
            return self._visitor_sub, self._visitor_subp

        try:
            gen_url = "https://passport.weibo.com/visitor/genvisitor"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            async with session.post(gen_url, data="cb=gen_visitor&fp=%7B%7D", headers=headers, timeout=10) as resp:
                text = await resp.text()
                tid_match = re.search(r'"tid":"([^"]+)"', text)
                if not tid_match:
                    return "", ""
                tid = tid_match.group(1)

            inc_url = f"https://passport.weibo.com/visitor/visitor?a=incarnate&t={tid}&w=2&c=095&gc=&cb=cross_domain&from=weibo"
            async with session.get(inc_url, headers=headers, timeout=10) as resp:
                text = await resp.text()
                sub_match = re.search(r'"sub":"([^"]+)"', text)
                subp_match = re.search(r'"subp":"([^"]+)"', text)
                if sub_match and subp_match:
                    self._visitor_sub = sub_match.group(1)
                    self._visitor_subp = subp_match.group(1)
                    return self._visitor_sub, self._visitor_subp
        except Exception as e:
            logger.debug(f"[WeiboChannel] 获取访客凭据失败: {e}")

        return "", ""

    def _clean_weibo_html(self, raw_html: str) -> str:
        """清洗微博正文 HTML 为易读的纯文本"""
        if not raw_html:
            return ""
        # 替换 <br /> 为换行
        text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        # 用 BeautifulSoup 抽取纯文本
        soup = BeautifulSoup(text, "html.parser")
        clean_text = soup.get_text(separator=" ").strip()
        # 清理多余空行与空格
        clean_text = re.sub(r"[ \t]+", " ", clean_text)
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
        return clean_text

    async def fetch_latest_notices(self, shop: Dict[str, Any]) -> List[Dict[str, Any]]:
        weibo_uid = str(shop.get("weibo_uid") or "").strip()
        if not weibo_uid:
            return []

        # 提取纯数字 UID
        uid_match = re.search(r"\d{6,16}", weibo_uid)
        if not uid_match:
            return []
        uid = uid_match.group(0)

        results = []
        async with aiohttp.ClientSession() as session:
            # 策略 1: 若配置了 RSS 路由 (如自建 RSSHub)，优先走 RSS
            if self.rss_base_url:
                try:
                    rss_url = f"{self.rss_base_url}/weibo/user/{uid}"
                    async with session.get(rss_url, timeout=15) as resp:
                        if resp.status == 200:
                            xml_text = await resp.text()
                            soup = BeautifulSoup(xml_text, "xml")
                            items = soup.find_all("item")
                            for item in items[:10]:
                                title = item.title.text if item.title else ""
                                desc = self._clean_weibo_html(item.description.text if item.description else "")
                                link = item.link.text if item.link else f"https://weibo.com/{uid}"
                                guid = item.guid.text if item.guid else link
                                results.append({
                                    "shop_id": shop.get("id"),
                                    "shop_name": shop.get("name"),
                                    "channel": "weibo",
                                    "source_id": f"weibo_{guid}",
                                    "title": title,
                                    "content": desc,
                                    "source_url": link,
                                    "created_at": ""
                                })
                            if results:
                                return results
                except Exception as e:
                    logger.debug(f"[WeiboChannel] RSS 抓取 {shop.get('name')} 异常: {e}")

            # 策略 2: 走 m.weibo.cn 移动端接口
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                    "Referer": f"https://m.weibo.cn/u/{uid}",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/plain, */*"
                }
                
                # Cookie 优先级：自定义 Cookie > 访客 SUB Cookie
                if self.custom_cookie:
                    headers["Cookie"] = self.custom_cookie
                else:
                    sub, subp = await self._get_visitor_cookie(session)
                    if sub:
                        headers["Cookie"] = f"SUB={sub}; SUBP={subp}"

                url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}"
                async with session.get(url, headers=headers, timeout=12) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cards = data.get("data", {}).get("cards", [])
                        for card in cards:
                            mblog = card.get("mblog")
                            if not mblog:
                                continue
                            
                            mid = str(mblog.get("id") or mblog.get("mid"))
                            raw_text = mblog.get("raw_text") or mblog.get("text") or ""
                            content = self._clean_weibo_html(raw_text)
                            source_url = f"https://weibo.com/{uid}/{mblog.get('bid') or mid}"
                            
                            results.append({
                                "shop_id": shop.get("id"),
                                "shop_name": shop.get("name"),
                                "channel": "weibo",
                                "source_id": f"weibo_{mid}",
                                "title": content[:40].replace("\n", " "),
                                "content": content,
                                "source_url": source_url,
                                "created_at": str(mblog.get("created_at") or "")
                            })
            except Exception as e:
                logger.warning(f"[WeiboChannel] 轮询店铺【{shop.get('name')}】微博失败: {e}")

        return results
