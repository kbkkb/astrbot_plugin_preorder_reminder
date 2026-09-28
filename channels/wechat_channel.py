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
                                        self.last_status[wechat_account] = {"status": "ok", "msg": f"WeWe-RSS抓取成功({len(results)}篇)"}
                                        return results
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"[WeChatChannel] RSS 轮询公众号【{wechat_account}】异常: {e}")
            # 策略 2: 内置搜狗微信公开检索通道（插件内原生实现，无需额外部署 Docker 容器）
            try:
                shop_name = shop.get("name", "")
                search_queries = []
                if wechat_account:
                    search_queries.append(f"{wechat_account} 补款")
                    search_queries.append(wechat_account)
                if shop_name and shop_name != wechat_account:
                    search_queries.append(f"{shop_name} 补款")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Referer": "https://weixin.sogou.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }

                seen_titles = set()
                for query in search_queries:
                    import urllib.parse
                    s_url = f"https://weixin.sogou.com/weixin?type=2&query={urllib.parse.quote(query)}"
                    async with session.get(s_url, headers=headers, timeout=10) as resp:
                        if resp.status != 200:
                            continue
                        html_text = await resp.text()
                        if "antispider" in str(resp.url) or "antispider" in html_text:
                            logger.info(f"[WeChatChannel] 搜狗微信检索触发反爬验证 (Query: {query})")
                            self.last_status[wechat_account] = {
                                "status": "warning",
                                "msg": "搜狗微信反爬验证中，可直接在群内发送文章链接自动解析，或配置WeWe-RSS"
                            }
                            continue
                        soup = BeautifulSoup(html_text, "html.parser")
                        boxes = soup.find_all("div", class_="txt-box")
                        for b in boxes:
                            h3 = b.find("h3")
                            a = h3.find("a") if h3 else None
                            if not a:
                                continue
                            raw_title = a.get_text().strip()
                            if raw_title in seen_titles:
                                continue

                            # 提取文章公众号发布主体与时间
                            acc_name = ""
                            created_at = ""
                            sp = b.find("div", class_="s-p")
                            if sp:
                                acc_el = sp.find(["a", "span"])
                                acc_name = acc_el.get_text().strip() if acc_el else ""
                                m_time = re.search(r"timeConvert\('(\d+)'\)", str(sp))
                                if m_time:
                                    import datetime
                                    try:
                                        created_at = datetime.datetime.fromtimestamp(int(m_time.group(1))).strftime("%Y-%m-%d %H:%M:%S")
                                    except Exception:
                                        pass

                            # 公众号匹配校验：过滤掉无关主体
                            if acc_name and (wechat_account or shop_name):
                                targets = [x.lower() for x in [wechat_account, shop_name] if x]
                                acc_lower = acc_name.lower()
                                matched = any(t in acc_lower or acc_lower in t for t in targets)
                                if not matched:
                                    continue

                            seen_titles.add(raw_title)

                            href = "https://weixin.sogou.com" + a["href"] if a["href"].startswith("/") else a["href"]
                            real_url = ""
                            try:
                                async with session.get(href, headers=headers, timeout=8) as r2:
                                    t2 = await r2.text()
                                    parts = re.findall(r"url \+= '([^']+)';", t2)
                                    if parts:
                                        real_url = "".join(parts).replace("@", "")
                            except Exception:
                                pass

                            article_content = ""
                            if real_url:
                                try:
                                    async with session.get(real_url, headers=headers, timeout=10) as r3:
                                        if r3.status == 200:
                                            art_soup = BeautifulSoup(await r3.text(), "html.parser")
                                            content_el = art_soup.find("div", class_="rich_media_content")
                                            if content_el:
                                                article_content = self._clean_article_html(content_el.decode_contents())
                                except Exception:
                                    pass

                            if not article_content:
                                p_info = b.find("p", class_="txt-info")
                                article_content = p_info.get_text().strip() if p_info else raw_title

                            results.append({
                                "shop_id": shop.get("id"),
                                "shop_name": shop.get("name"),
                                "channel": "wechat",
                                "source_id": f"wechat_{abs(hash(real_url or raw_title))}",
                                "title": raw_title,
                                "content": f"{raw_title}\n\n{article_content[:3000]}",
                                "source_url": real_url or href,
                                "created_at": created_at
                            })
                            if len(results) >= 5:
                                break

                    if results:
                        self.last_status[wechat_account] = {"status": "ok", "msg": f"原生检索通道获取到 {len(results)} 篇相关文章"}
                        return results
            except Exception as e:
                logger.debug(f"[WeChatChannel] 原生公众号检索异常: {e}")

        if not results and wechat_account and wechat_account not in self.last_status:
            self.last_status[wechat_account] = {"status": "warning", "msg": "未检索到文章，可直接在群内发送公众号文章链接自动解析"}

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
