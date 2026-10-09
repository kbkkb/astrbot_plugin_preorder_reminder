import re
import json
import time
import logging
import asyncio


import aiohttp
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
from .base_channel import BaseChannel

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

# ==================== 微博渠道可用性说明（2025 实测） ====================
# 1. m.weibo.cn JSON API：即便携带访客 Cookie 也返回 ok=-100（要求登录），
#    无 Cookie 裸奔直接 432（山海防火墙）；
# 2. weibo.com PC ajax (mymblog)：访客 Cookie 返回 403「前方有点拥堵，请登录后使用」，
#    仅真实登录 Cookie 可用；
# 3. m.weibo.cn 个人页 HTML：已改为 PWA 壳，$render_data 为 null，无数据可解析。
# 结论：微博匿名抓取通道已基本关闭，可靠抓取需要用户配置微博 Cookie
# （私聊发送 /设置微博cookie <cookie>）或在面板配置 weibo_rss_base_url 接入自建 RSSHub。
# 为避免每轮轮询都做无效请求，匿名通道失败后会进入冷却期。
# 面向大众部署时，微博渠道属于「进阶可选」：部署者可经 weibo_channel_enabled 一键关闭，
# 关闭后本渠道零请求，插件其余能力（QQ群嗅探/公众号/手动补录）不受任何影响。


class WeiboChannel(BaseChannel):
    ANON_FAIL_COOLDOWN_SECONDS = 1800  # 匿名通道失败冷却时长（秒），避免无效轮询
    PC_MAX_PAGES = 3                   # PC ajax 单次最多翻页数，控制单店抓取耗时

    def __init__(self, custom_cookie: str = "", rss_base_url: str = "", enabled: bool = True):
        self.custom_cookie = custom_cookie.strip()
        self.rss_base_url = rss_base_url.rstrip("/")
        self.enabled = enabled
        self._visitor_sub: str = ""
        self._visitor_subp: str = ""
        self.last_status: Dict[str, Dict[str, Any]] = {}   # uid -> {status, msg, time}
        self._anon_fail_until: Dict[str, float] = {}       # uid -> monotonic 时间戳

    def set_cookie(self, cookie: str):
        """动态更新微博 Cookie（接受 'SUB=xxx' 或完整 Cookie 字符串）"""
        self.custom_cookie = cookie.strip()
        # 用户更新 Cookie 后，重置匿名冷却，允许立即重新尝试
        self._anon_fail_until.clear()
        logger.info("[WeiboChannel] 微博 Cookie 已更新")

    def get_shop_status(self, shop: Dict[str, Any]) -> Dict[str, Any]:
        """获取店铺微博渠道的最新状态诊断"""
        if not self.enabled:
            return {"status": "disabled", "msg": "微博渠道已在配置中关闭（weibo_channel_enabled=false）"}
        weibo_uid = str(shop.get("weibo_uid") or "").strip()
        uid_match = re.search(r"\d{6,16}", weibo_uid)
        if not uid_match:
            return {"status": "no_uid", "msg": "未配置微博 UID"}
        uid = uid_match.group(0)
        return self.last_status.get(uid, {"status": "unknown", "msg": "尚未执行轮询"})

    # ==================== 时间标准化 ====================

    @staticmethod
    def _normalize_time(raw: Any) -> str:
        """微博时间标准化（实现已上移至 BaseChannel.normalize_time，此处保留兼容入口）"""
        return BaseChannel.normalize_time(raw)

    def _clean_weibo_html(self, raw_html: str) -> str:
        """清洗微博正文 HTML 为易读的纯文本"""
        if not raw_html:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        soup = BeautifulSoup(text, "html.parser")
        clean_text = soup.get_text(separator=" ").strip()
        clean_text = re.sub(r"[ \t]+", " ", clean_text)
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)
        return clean_text

    async def _get_visitor_cookie(self, session: aiohttp.ClientSession) -> Tuple[str, str]:
        """获取微博访客临时凭据 (SUB / SUBP)"""
        if self._visitor_sub:
            return self._visitor_sub, self._visitor_subp

        try:
            gen_url = "https://passport.weibo.com/visitor/genvisitor"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
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

    # ==================== 匿名失败冷却 ====================

    def _anon_in_cooldown(self, uid: str) -> bool:
        return time.monotonic() < self._anon_fail_until.get(uid, 0.0)

    def _mark_anon_fail(self, uid: str):
        self._anon_fail_until[uid] = time.monotonic() + self.ANON_FAIL_COOLDOWN_SECONDS

    # ==================== 抓取策略 ====================

    async def _fetch_via_rss(
        self,
        session: aiohttp.ClientSession,
        uid: str,
        shop_name: str,
        shop: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """策略 1: 自建 RSSHub 路由"""
        results = []
        try:
            rss_url = f"{self.rss_base_url}/weibo/user/{uid}"
            async with session.get(rss_url, timeout=15) as resp:
                if resp.status != 200:
                    return []
                xml_text = await resp.text()
            soup = self.parse_xml(xml_text)
            items = soup.find_all("item")
            for item in items[:15]:
                title = item.title.text if item.title else ""
                desc = self._clean_weibo_html(item.description.text if item.description else "")
                link = item.link.text if item.link else f"https://weibo.com/{uid}"
                guid = item.guid.text if item.guid else link
                pub_el = self.find_child_ci(item, "pubDate")
                pub = pub_el.text if pub_el else ""
                results.append({
                    "shop_id": shop.get("id"),
                    "shop_name": shop_name,
                    "channel": "weibo",
                    "source_id": f"weibo_{guid}",
                    "title": title,
                    "content": desc,
                    "source_url": link,
                    "created_at": self._normalize_time(pub)
                })
            if results:
                self.last_status[uid] = {"status": "ok", "msg": f"RSS抓取成功({len(results)}条)"}
        except Exception as e:
            logger.debug(f"[WeiboChannel] RSS 抓取 {shop_name} 异常: {e}")
        return results

    async def _fetch_via_pc_ajax(
        self,
        session: aiohttp.ClientSession,
        uid: str,
        shop_name: str,
        shop: Dict[str, Any],
        cookie_str: str
    ) -> List[Dict[str, Any]]:
        """策略 2: PC 端 Ajax API（需真实登录 Cookie，支持翻页）"""
        results = []
        pc_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": f"https://weibo.com/u/{uid}",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_str
        }
        try:
            for page in range(1, self.PC_MAX_PAGES + 1):
                pc_url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0"
                async with session.get(pc_url, headers=pc_headers, timeout=12) as resp:
                    if resp.status == 403:
                        # 微博对未登录/失效Cookie的固定拒绝文案
                        self.last_status[uid] = {
                            "status": "need_cookie",
                            "msg": "微博PC接口拒绝访问(403 需登录)，当前Cookie无效或已过期，请重新登录微博复制最新Cookie后发送 /设置微博cookie"
                        }
                        return []
                    if resp.status != 200:
                        self.last_status[uid] = {"status": "error", "msg": f"HTTP状态码: {resp.status}"}
                        return []
                    data = await resp.json()

                if not isinstance(data, dict):
                    break
                items = (data.get("data") or {}).get("list") or []
                for it in items:
                    mid = str(it.get("id") or it.get("mid") or "")
                    if not mid:
                        continue
                    raw_text = it.get("text_raw") or it.get("text") or ""
                    content = self._clean_weibo_html(raw_text)
                    source_url = f"https://weibo.com/{uid}/{mid}"
                    results.append({
                        "shop_id": shop.get("id"),
                        "shop_name": shop_name,
                        "channel": "weibo",
                        "source_id": f"weibo_{mid}",
                        "title": content[:40].replace("\n", " "),
                        "content": content,
                        "source_url": source_url,
                        "created_at": self._normalize_time(it.get("created_at"))
                    })
                # 不足一页说明没有更多数据，提前结束翻页
                if len(items) < 10:
                    break
            if results:
                self.last_status[uid] = {"status": "ok", "msg": f"PC接口抓取成功({len(results)}条)"}
        except Exception as e:
            self.last_status[uid] = {"status": "error", "msg": str(e)}
            logger.warning(f"[WeiboChannel] PC Ajax API 抓取 {shop_name} 异常: {e}")
        return results

    async def _fetch_via_mobile_api(
        self,
        session: aiohttp.ClientSession,
        uid: str,
        shop_name: str,
        shop: Dict[str, Any],
        cookie_str: str = "",
        allow_visitor: bool = True
    ) -> List[Dict[str, Any]]:
        """
        策略 3: m.weibo.cn 移动端接口。
        2025 起该接口对未登录访客返回 ok=-100，仅作为用户移动端 Cookie 的兼容通道，
        匿名访客失败时由上层进入冷却期。
        """
        results = []
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Referer": f"https://m.weibo.cn/u/{uid}",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*"
        }
        if cookie_str:
            headers["Cookie"] = cookie_str
        elif allow_visitor:
            sub, subp = await self._get_visitor_cookie(session)
            if sub:
                headers["Cookie"] = f"SUB={sub}; SUBP={subp}"

        url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}"
        try:
            async with session.get(url, headers=headers, timeout=12) as resp:
                if resp.status == 432:
                    self.last_status[uid] = {
                        "status": "need_cookie",
                        "msg": "触发微博山海防火墙风控拦截(HTTP 432，需配置Cookie)"
                    }
                    return []
                if resp.status != 200:
                    self.last_status[uid] = {"status": "error", "msg": f"HTTP状态码: {resp.status}"}
                    return []
                data = await resp.json()

            if not isinstance(data, dict):
                return []
            if data.get("ok") == -100 or "passport.weibo" in str(data.get("url", "")):
                if cookie_str:
                    msg = ("微博移动端接口要求登录，当前Cookie无效或已过期，"
                           "请重新登录微博复制最新Cookie后发送 /设置微博cookie")
                else:
                    msg = ("微博已关闭访客匿名访问(接口要求登录)。"
                           "请私聊发送 /设置微博cookie <cookie> 配置微博Cookie，"
                           "或在 AstrBot 面板配置 weibo_rss_base_url 接入自建 RSSHub")
                self.last_status[uid] = {"status": "need_cookie", "msg": msg}
                logger.warning(f"[WeiboChannel] 轮询店铺【{shop_name}】微博受限(需登录)。{msg}")
                return []

            cards = (data.get("data") or {}).get("cards") or []
            for card in cards:
                mblog = card.get("mblog") if isinstance(card, dict) else None
                if not mblog:
                    continue
                mid = str(mblog.get("id") or mblog.get("mid") or "")
                if not mid:
                    continue
                raw_text = mblog.get("raw_text") or mblog.get("text") or ""
                content = self._clean_weibo_html(raw_text)
                # 转发微博附带原微博正文
                retweeted = mblog.get("retweeted_status")
                if retweeted:
                    rt_text = self._clean_weibo_html(retweeted.get("raw_text") or retweeted.get("text") or "")
                    if rt_text:
                        content = f"{content}\n【转发原文】{rt_text}"
                results.append({
                    "shop_id": shop.get("id"),
                    "shop_name": shop_name,
                    "channel": "weibo",
                    "source_id": f"weibo_{mid}",
                    "title": content[:40].replace("\n", " "),
                    "content": content,
                    "source_url": f"https://weibo.com/{uid}/{mblog.get('bid') or mid}",
                    "created_at": self._normalize_time(mblog.get("created_at"))
                })
            if results:
                self.last_status[uid] = {"status": "ok", "msg": f"移动端接口抓取成功({len(results)}条)"}
        except Exception as e:
            self.last_status[uid] = {"status": "error", "msg": str(e)}
            logger.warning(f"[WeiboChannel] 移动端接口抓取 {shop_name} 异常: {e}")
        return results

    async def fetch_latest_notices(self, shop: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        weibo_uid = str(shop.get("weibo_uid") or "").strip()
        if not weibo_uid:
            return []

        uid_match = re.search(r"\d{6,16}", weibo_uid)
        if not uid_match:
            return []
        uid = uid_match.group(0)
        shop_name = shop.get("name", "店铺")

        results = []
        async with aiohttp.ClientSession() as session:
            # 策略 1: 自建 RSSHub（最稳定，优先）
            if self.rss_base_url:
                results = await self._fetch_via_rss(session, uid, shop_name, shop)
                if results:
                    return results

            # 策略 2: PC ajax（需用户配置真实登录 Cookie）
            cookie_str = self.custom_cookie
            if cookie_str and not cookie_str.startswith("SUB="):
                cookie_str = f"SUB={cookie_str}"
            if cookie_str:
                results = await self._fetch_via_pc_ajax(session, uid, shop_name, shop, cookie_str)
                if results:
                    return results

            # 策略 3: 移动端接口（用户 Cookie 或访客 Cookie）
            # 匿名抓取近期已失效：失败后进入冷却期，期间直接跳过，避免无效轮询
            if not cookie_str and self._anon_in_cooldown(uid):
                logger.debug(f"[WeiboChannel] 店铺【{shop_name}】微博匿名通道处于冷却期，跳过本轮")
                return []
            results = await self._fetch_via_mobile_api(
                session, uid, shop_name, shop,
                cookie_str=cookie_str, allow_visitor=not cookie_str)
            if results:
                return results
            if not cookie_str:
                self._mark_anon_fail(uid)

        return results
