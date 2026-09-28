import re
import logging
import asyncio
import aiohttp
from typing import List, Optional

logger = logging.getLogger("astrbot_plugin_preorder_reminder")

class OCRHelper:
    """本地轻量 OCR 辅助类，基于 RapidOCR (ONNX Runtime)，完全在本地 CPU 运行，0 Token 成本。"""

    _ocr_engine = None
    _ocr_available: Optional[bool] = None

    @classmethod
    def is_available(cls) -> bool:
        if cls._ocr_available is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                cls._ocr_available = True
            except ImportError:
                cls._ocr_available = False
        return cls._ocr_available

    @classmethod
    def get_engine(cls):
        if not cls.is_available():
            return None
        if cls._ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                # 初始化 RapidOCR 实例
                cls._ocr_engine = RapidOCR()
                logger.info("[OCRHelper] RapidOCR 引擎初始化成功")
            except Exception as e:
                logger.warning(f"[OCRHelper] 初始化 RapidOCR 失败: {e}")
                cls._ocr_available = False
                return None
        return cls._ocr_engine

    @classmethod
    def _run_ocr_sync(cls, image_bytes: bytes) -> str:
        """在同步线程中执行 OCR 计算，避免阻塞 asyncio 事件循环"""
        engine = cls.get_engine()
        if not engine:
            return ""
        try:
            result, _ = engine(image_bytes)
            if not result:
                return ""
            # result 结构为 [[box, text, score], ...]
            valid_lines = []
            for item in result:
                if item and len(item) > 1:
                    text = str(item[1]).strip()
                    score = float(item[2]) if len(item) > 2 else 1.0
                    # 过滤置信度过低或纯空白噪点
                    if text and score >= 0.5:
                        valid_lines.append(text)
            return "\n".join(valid_lines)
        except Exception as e:
            logger.debug(f"[OCRHelper] OCR 识别执行异常: {e}")
            return ""

    @classmethod
    async def extract_text_from_bytes(cls, image_bytes: bytes) -> str:
        """异步从图片字节流中提取文字"""
        if not image_bytes or not cls.is_available():
            return ""
        return await asyncio.to_thread(cls._run_ocr_sync, image_bytes)

    @classmethod
    async def extract_text_from_url(
        cls,
        url: str,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: int = 15
    ) -> str:
        """异步下载图片并提取文字"""
        if not url or not cls.is_available():
            return ""

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://mp.weixin.qq.com/",
        }

        try:
            close_session = False
            if session is None:
                session = aiohttp.ClientSession()
                close_session = True

            try:
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        return ""
                    img_bytes = await resp.read()
            finally:
                if close_session:
                    await session.close()

            # 过滤过小图片（如小于 5KB 的表情、图标、点阵）
            if len(img_bytes) < 5120:
                return ""

            return await cls.extract_text_from_bytes(img_bytes)
        except Exception as e:
            logger.debug(f"[OCRHelper] 获取并识别图片失败 ({url[:40]}...): {e}")
            return ""

    @classmethod
    async def batch_extract_text_from_urls(
        cls,
        urls: List[str],
        max_images: int = 6,
        session: Optional[aiohttp.ClientSession] = None
    ) -> str:
        """批量识别一组图片（如微信推文中的排期海报长图），合并输出识别文字"""
        if not urls or not cls.is_available():
            return ""

        # 过滤并去重 URL
        filtered_urls = []
        for u in urls:
            u_clean = u.strip()
            # 过滤明显的二维码、头像、logo小图
            low = u_clean.lower()
            if any(k in low for k in ["qrcode", "avatar", "logo", "icon", "wx_fmt=gif"]):
                continue
            if u_clean not in filtered_urls:
                filtered_urls.append(u_clean)
            if len(filtered_urls) >= max_images:
                break

        if not filtered_urls:
            return ""

        logger.info(f"[OCRHelper] 开始本地 OCR 识别 {len(filtered_urls)} 张推文图片...")

        tasks = [cls.extract_text_from_url(u, session=session) for u in filtered_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        extracted_texts = []
        for idx, res in enumerate(results):
            if isinstance(res, str) and res.strip():
                extracted_texts.append(f"--- 图片 {idx + 1} 识别内容 ---\n{res.strip()}")

        if not extracted_texts:
            return ""

        return "\n\n".join(extracted_texts)
