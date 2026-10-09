import os
import json
import sqlite3
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

class Database:
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            # 默认保存在当前目录下的 data 文件夹
            base_dir = Path(__file__).resolve().parent.parent / "data"
            base_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(base_dir / "preorder.db")
        else:
            self.db_path = db_path
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. 店铺档案表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                aliases TEXT DEFAULT '[]',
                weibo_uid TEXT DEFAULT '',
                wechat_account TEXT DEFAULT '',
                qq_groups TEXT DEFAULT '[]',
                admin_qq_ids TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)

            # 2. 用户订阅商品表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'private',
                target_id TEXT NOT NULL,
                shop_id INTEGER,
                shop_name TEXT NOT NULL,
                item_name TEXT NOT NULL,
                deposit_amount REAL DEFAULT 0.0,
                estimated_month TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'waiting',
                replenish_deadline TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE SET NULL
            );
            """)

            # 3. 监控获取到的历史通知记录表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER,
                shop_name TEXT NOT NULL,
                channel TEXT NOT NULL,
                notice_type TEXT NOT NULL DEFAULT 'replenish',
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                source_id TEXT UNIQUE NOT NULL,
                source_url TEXT DEFAULT '',
                matched_items TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE SET NULL
            );
            """)

            # 4. 结构化商品条目表（从推文/OCR文本提取的产品、价格、日期）
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS notice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notice_id INTEGER,
                shop_id INTEGER,
                shop_name TEXT NOT NULL,
                product TEXT NOT NULL,
                deposit REAL DEFAULT 0.0,
                final_payment REAL DEFAULT 0.0,
                deadline TEXT DEFAULT '',
                notice_type TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (notice_id) REFERENCES notices(id) ON DELETE CASCADE
            );
            """)
            conn.commit()

        # 初始化预设知名模玩店铺
        self._seed_preset_shops()

    def _seed_preset_shops(self):
        """预置常用知名模玩店铺档案（如良笑GSC、猫受屋等）"""
        preset_shops = [
            {
                "name": "良笑GoodSmile",
                "aliases": ["GSC", "良笑", "GoodSmile", "良笑旗舰店"],
                "weibo_uid": "2638252657",
                "wechat_account": "",
                "notes": "良笑GoodSmile官方旗舰店（官方微博UID: 2638252657）"
            },
            {
                "name": "GoodSmile良笑社",
                "aliases": ["良笑社", "GSC官博"],
                "weibo_uid": "1798143541",
                "wechat_account": "",
                "notes": "GoodSmile良笑社官方微博（官方微博UID: 1798143541）"
            },
            {
                "name": "猫受屋",
                "aliases": ["猫屋"],
                "weibo_uid": "1874987014",
                "wechat_account": "",
                "notes": "猫受屋手办模型官方微博（官方微博UID: 1874987014）"
            },
            {
                "name": "淘模玩",
                "aliases": ["TaoMorrow", "淘模", "淘模玩TaoMorrow"],
                "weibo_uid": "",
                "wechat_account": "TaoMorrow",
                "notes": "淘模玩官方微信公众号（微信: TaoMorrow）"
            }
        ]

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for s in preset_shops:
                wb_uid = s.get("weibo_uid", "")
                wx_acc = s.get("wechat_account", "")
                cursor.execute(
                    "SELECT id, name, aliases, wechat_account FROM shops WHERE name = ? OR (weibo_uid != '' AND weibo_uid = ?) OR (wechat_account != '' AND wechat_account = ?)",
                    (s["name"], wb_uid, wx_acc)
                )
                row = cursor.fetchone()
                aliases_json = json.dumps(s["aliases"], ensure_ascii=False)
                if not row:
                    cursor.execute("""
                    INSERT INTO shops (name, aliases, weibo_uid, wechat_account, qq_groups, admin_qq_ids, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '[]', '[]', ?, ?, ?)
                    """, (s["name"], aliases_json, wb_uid, wx_acc, s["notes"], now, now))
                else:
                    # 如果已存在，更新别名与公众号配置
                    existing_aliases = json.loads(row[2] or "[]")
                    merged_aliases = list(dict.fromkeys(existing_aliases + s["aliases"]))
                    new_wx = row[3] or wx_acc
                    cursor.execute(
                        "UPDATE shops SET aliases = ?, wechat_account = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(merged_aliases, ensure_ascii=False), new_wx, now, row[0])
                    )
            conn.commit()

    # ==================== 店铺管理 (Shop) ====================

    def add_shop(
        self,
        name: str,
        aliases: Optional[List[str]] = None,
        weibo_uid: str = "",
        wechat_account: str = "",
        qq_groups: Optional[List[str]] = None,
        admin_qq_ids: Optional[List[str]] = None,
        notes: str = ""
    ) -> int:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name = name.strip()
        new_aliases = [str(a).strip() for a in (aliases or []) if str(a).strip()]
        new_groups = [str(g).strip() for g in (qq_groups or []) if str(g).strip()]
        new_admins = [str(u).strip() for u in (admin_qq_ids or []) if str(u).strip()]
        weibo_uid = weibo_uid.strip()
        wechat_account = wechat_account.strip()
        notes = notes.strip()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, aliases, weibo_uid, wechat_account, qq_groups, admin_qq_ids, notes FROM shops WHERE name = ?",
                (name,)
            )
            row = cursor.fetchone()

            if row:
                # 已存在：合并语义更新，避免清空本次未提及的绑定
                # （别名/QQ群/管理员取并集；weibo/wechat/notes 非空才覆盖）
                merged_aliases = list(dict.fromkeys(json.loads(row["aliases"] or "[]") + new_aliases))
                merged_groups = list(dict.fromkeys(json.loads(row["qq_groups"] or "[]") + new_groups))
                merged_admins = list(dict.fromkeys(json.loads(row["admin_qq_ids"] or "[]") + new_admins))
                cursor.execute(
                    """
                    UPDATE shops SET
                        aliases = ?, weibo_uid = ?, wechat_account = ?,
                        qq_groups = ?, admin_qq_ids = ?, notes = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(merged_aliases, ensure_ascii=False),
                        weibo_uid or (row["weibo_uid"] or ""),
                        wechat_account or (row["wechat_account"] or ""),
                        json.dumps(merged_groups, ensure_ascii=False),
                        json.dumps(merged_admins, ensure_ascii=False),
                        notes or (row["notes"] or ""),
                        now,
                        row["id"],
                    )
                )
                conn.commit()
                return row["id"]

            aliases_json = json.dumps(new_aliases, ensure_ascii=False)
            qq_groups_json = json.dumps(new_groups, ensure_ascii=False)
            admin_qq_ids_json = json.dumps(new_admins, ensure_ascii=False)
            cursor.execute("""
            INSERT INTO shops (name, aliases, weibo_uid, wechat_account, qq_groups, admin_qq_ids, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, aliases_json, weibo_uid, wechat_account, qq_groups_json, admin_qq_ids_json, notes, now, now))
            conn.commit()
            return cursor.lastrowid

    def update_shop(self, shop_id: int, **kwargs) -> bool:
        allowed = {"name", "aliases", "weibo_uid", "wechat_account", "qq_groups", "admin_qq_ids", "notes"}
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k} = ?")
                if isinstance(v, (list, dict)):
                    params.append(json.dumps(v, ensure_ascii=False))
                else:
                    params.append(str(v).strip())
        if not updates:
            return False

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates.append("updated_at = ?")
        params.append(now)
        params.append(shop_id)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE shops SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            return cursor.rowcount > 0

    def get_shop_by_id(self, shop_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM shops WHERE id = ?", (shop_id,))
            row = cursor.fetchone()
            return self._format_shop_row(row) if row else None

    def get_shop_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = name.strip().lower()
        shops = self.get_all_shops()
        for s in shops:
            if s["name"].lower() == target:
                return s
            if any(alias.lower() == target for alias in s.get("aliases", [])):
                return s
        return None

    def get_all_shops(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT s.*, COUNT(sub.id) as sub_count
            FROM shops s
            LEFT JOIN subscriptions sub ON s.id = sub.shop_id AND sub.status IN ('waiting', 'replenishing')
            GROUP BY s.id
            ORDER BY s.id ASC
            """)
            rows = cursor.fetchall()
            return [self._format_shop_row(row) for row in rows]

    def delete_shop(self, shop_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM shops WHERE id = ?", (shop_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_monitored_qq_groups(self) -> Dict[str, List[Dict[str, Any]]]:
        """返回被监控的群号与对应的店铺映射字典 { 'group_id': [shop1, shop2] }"""
        shops = self.get_all_shops()
        group_map: Dict[str, List[Dict[str, Any]]] = {}
        for s in shops:
            for g in s.get("qq_groups", []):
                gid = str(g).strip()
                if gid:
                    group_map.setdefault(gid, []).append(s)
        return group_map

    def _format_shop_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        d["qq_groups"] = json.loads(d.get("qq_groups") or "[]")
        d["admin_qq_ids"] = json.loads(d.get("admin_qq_ids") or "[]")
        if "sub_count" not in d:
            d["sub_count"] = 0
        return d

    # ==================== 订阅管理 (Subscription) ====================

    def add_subscription(
        self,
        user_id: str,
        item_name: str,
        shop_name: str = "",
        target_type: str = "private",
        target_id: str = "",
        deposit_amount: float = 0.0,
        estimated_month: str = ""
    ) -> int:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target_id = target_id or user_id

        # 查找是否存在对应的店铺
        shop_id = None
        if shop_name:
            shop = self.get_shop_by_name(shop_name)
            if shop:
                shop_id = shop["id"]
                shop_name = shop["name"]  # 对齐标准店名

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO subscriptions 
            (user_id, target_type, target_id, shop_id, shop_name, item_name, deposit_amount, estimated_month, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)
            """, (str(user_id), target_type, str(target_id), shop_id, shop_name.strip(), item_name.strip(), deposit_amount, estimated_month.strip(), now, now))
            conn.commit()
            return cursor.lastrowid

    def get_user_subscriptions(self, user_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute("""
                SELECT * FROM subscriptions 
                WHERE user_id = ? AND status = ?
                ORDER BY id DESC
                """, (str(user_id), status))
            else:
                cursor.execute("""
                SELECT * FROM subscriptions 
                WHERE user_id = ? AND status != 'cancelled'
                ORDER BY id DESC
                """, (str(user_id),))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_active_subscriptions(self, shop_name: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if shop_name:
                cursor.execute("""
                SELECT * FROM subscriptions 
                WHERE status IN ('waiting', 'replenishing') AND (shop_name = ? OR shop_name = '')
                """, (shop_name.strip(),))
            else:
                cursor.execute("""
                SELECT * FROM subscriptions 
                WHERE status IN ('waiting', 'replenishing')
                """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def update_subscription_status(
        self,
        sub_id: int,
        status: str,
        deadline: str = "",
        source_url: str = ""
    ) -> bool:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE subscriptions 
            SET status = ?, 
                replenish_deadline = CASE WHEN ? != '' THEN ? ELSE replenish_deadline END,
                source_url = CASE WHEN ? != '' THEN ? ELSE source_url END,
                updated_at = ?
            WHERE id = ?
            """, (status, deadline, deadline, source_url, source_url, now, sub_id))
            conn.commit()
            return cursor.rowcount > 0

    def delete_subscription(self, sub_id: int, user_id: Optional[str] = None) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if user_id:
                cursor.execute("DELETE FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, str(user_id)))
            else:
                cursor.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ==================== 通知历史 (Notices) ====================

    def add_notice(
        self,
        shop_id: Optional[int],
        shop_name: str,
        channel: str,
        notice_type: str,
        title: str,
        content: str,
        source_id: str,
        source_url: str = "",
        matched_items: Optional[List[str]] = None,
        created_at: Optional[str] = None
    ) -> int:
        now = created_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        matched_json = json.dumps(matched_items or [], ensure_ascii=False)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR IGNORE INTO notices 
            (shop_id, shop_name, channel, notice_type, title, content, source_id, source_url, matched_items, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (shop_id, shop_name.strip(), channel, notice_type, title.strip(), content.strip(), str(source_id).strip(), source_url.strip(), matched_json, now))
            conn.commit()
            return cursor.lastrowid


    # ==================== 结构化商品条目 (Notice Items) ====================

    def add_notice_items(
        self,
        notice_id: int,
        shop_id: Optional[int],
        shop_name: str,
        items: List[Dict[str, Any]],
        notice_type: str = "",
        created_at: Optional[str] = None
    ) -> int:
        """批量写入一条通知下提取出的结构化商品条目"""
        if not items:
            return 0
        now = created_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for it in items:
            product = str(it.get("product") or "").strip()
            if not product:
                continue
            rows.append((
                notice_id, shop_id, shop_name.strip(), product,
                float(it.get("deposit") or 0.0), float(it.get("final_payment") or 0.0),
                str(it.get("deadline") or ""), notice_type, now
            ))
        if not rows:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
            INSERT INTO notice_items
            (notice_id, shop_id, shop_name, product, deposit, final_payment, deadline, notice_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            return len(rows)

    def get_recent_items(
        self,
        days: int = 7,
        shop_name: Optional[str] = None,
        keyword: Optional[str] = None,
        notice_type: Optional[str] = None,
        limit: int = 40
    ) -> List[Dict[str, Any]]:
        """查询近期结构化商品条目，支持按店铺/关键词/类型过滤"""
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        target_shop = self.get_shop_by_name(shop_name) if shop_name else None

        conditions = ["i.created_at >= ?"]
        params: List[Any] = [since]

        if shop_name:
            if target_shop:
                conditions.append("(i.shop_id = ? OR i.shop_name = ? OR i.shop_name LIKE ?)")
                params.extend([target_shop["id"], target_shop["name"], f"%{shop_name.strip()}%"])
            else:
                conditions.append("(i.shop_name = ? OR i.shop_name LIKE ?)")
                params.extend([shop_name.strip(), f"%{shop_name.strip()}%"])

        if keyword and keyword.strip():
            conditions.append("i.product LIKE ?")
            params.append(f"%{keyword.strip()}%")

        if notice_type:
            conditions.append("i.notice_type = ?")
            params.append(notice_type)

        query = f"""
        SELECT i.*, n.title AS notice_title, n.source_url, n.channel
        FROM notice_items i
        LEFT JOIN notices n ON i.notice_id = n.id
        WHERE {' AND '.join(conditions)}
        ORDER BY i.created_at DESC, i.id DESC
        LIMIT {int(limit)}
        """

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return [dict(r) for r in cursor.fetchall()]

    def get_items_for_notice(self, notice_id: int) -> List[Dict[str, Any]]:
        """获取某条通知下的全部结构化商品条目"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT * FROM notice_items WHERE notice_id = ? ORDER BY id ASC
            """, (notice_id,))
            return [dict(r) for r in cursor.fetchall()]

    def get_user_focused_items(self, user_id: str, days: int = 30, limit: int = 40) -> List[Dict[str, Any]]:
        """查询用户订阅商品命中的结构化条目（只看玩家关注的部分）"""
        subs = self.get_user_subscriptions(user_id)
        if not subs:
            return []
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        results = []
        seen = set()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for sub in subs:
                item_name = (sub.get("item_name") or "").strip()
                if not item_name:
                    continue
                cursor.execute("""
                SELECT i.*, n.title AS notice_title, n.source_url
                FROM notice_items i
                LEFT JOIN notices n ON i.notice_id = n.id
                WHERE i.product LIKE ? AND i.created_at >= ?
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT 20
                """, (f"%{item_name}%", since))
                for r in cursor.fetchall():
                    key = r["id"]
                    if key in seen:
                        continue
                    seen.add(key)
                    d = dict(r)
                    d["sub_id"] = sub.get("id")
                    d["sub_item"] = item_name
                    results.append(d)
                    if len(results) >= limit:
                        return results
        return results
    def is_notice_processed(self, source_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM notices WHERE source_id = ?", (str(source_id).strip(),))
            return cursor.fetchone() is not None

    def get_recent_notices(
        self,
        days: int = 1,
        notice_type: Optional[str] = None,
        shop_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        target_shop = self.get_shop_by_name(shop_name) if shop_name else None
        
        conditions = ["created_at >= ?"]
        params: List[Any] = [since]

        if notice_type:
            conditions.append("notice_type = ?")
            params.append(notice_type)

        if shop_name:
            if target_shop:
                conditions.append("(shop_id = ? OR shop_name = ? OR shop_name LIKE ?)")
                params.extend([target_shop["id"], target_shop["name"], f"%{shop_name.strip()}%"])
            else:
                conditions.append("(shop_name = ? OR shop_name LIKE ?)")
                params.extend([shop_name.strip(), f"%{shop_name.strip()}%"])

        query = f"""
        SELECT * FROM notices 
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC, id DESC
        """

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["matched_items"] = json.loads(d.get("matched_items") or "[]")
                res.append(d)
            return res

    def record_manual_notice(
        self,
        shop_name: str,
        notice_type: str,
        content: str,
        title: str = "",
        source_url: str = ""
    ) -> int:
        """手动记录/补录一条店铺情报"""
        shop = self.get_shop_by_name(shop_name)
        shop_id = shop["id"] if shop else None
        final_shop_name = shop["name"] if shop else shop_name.strip()
        now_ts = int(datetime.datetime.now().timestamp())
        source_id = f"manual_{now_ts}_{final_shop_name}"

        return self.add_notice(
            shop_id=shop_id,
            shop_name=final_shop_name,
            channel="manual",
            notice_type=notice_type,
            title=title or (content[:30] + "..."),
            content=content,
            source_id=source_id,
            source_url=source_url,
            matched_items=[]
        )

    def get_latest_notice_for_shop(self, shop_name: str) -> Optional[Dict[str, Any]]:
        """获取指定店铺最新的一篇通知/博文/文章（不受时间范围限制）"""
        target_shop = self.get_shop_by_name(shop_name)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if target_shop:
                cursor.execute("""
                SELECT * FROM notices 
                WHERE shop_id = ? OR shop_name = ? OR shop_name LIKE ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """, (target_shop["id"], target_shop["name"], f"%{shop_name.strip()}%"))
            else:
                cursor.execute("""
                SELECT * FROM notices 
                WHERE shop_name = ? OR shop_name LIKE ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """, (shop_name.strip(), f"%{shop_name.strip()}%"))
            row = cursor.fetchone()
            if row:
                d = dict(row)
                d["matched_items"] = json.loads(d.get("matched_items") or "[]")
                return d
            return None


