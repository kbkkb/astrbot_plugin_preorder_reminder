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
        aliases_json = json.dumps(aliases or [], ensure_ascii=False)
        qq_groups_json = json.dumps([str(g).strip() for g in (qq_groups or []) if str(g).strip()], ensure_ascii=False)
        admin_qq_ids_json = json.dumps([str(u).strip() for u in (admin_qq_ids or []) if str(u).strip()], ensure_ascii=False)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO shops (name, aliases, weibo_uid, wechat_account, qq_groups, admin_qq_ids, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                aliases=excluded.aliases,
                weibo_uid=CASE WHEN excluded.weibo_uid != '' THEN excluded.weibo_uid ELSE shops.weibo_uid END,
                wechat_account=CASE WHEN excluded.wechat_account != '' THEN excluded.wechat_account ELSE shops.wechat_account END,
                qq_groups=excluded.qq_groups,
                admin_qq_ids=excluded.admin_qq_ids,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """, (name.strip(), aliases_json, weibo_uid.strip(), wechat_account.strip(), qq_groups_json, admin_qq_ids_json, notes.strip(), now, now))
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
        matched_items: Optional[List[str]] = None
    ) -> int:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    def is_notice_processed(self, source_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM notices WHERE source_id = ?", (str(source_id).strip(),))
            return cursor.fetchone() is not None

    def get_recent_notices(self, days: int = 1, notice_type: Optional[str] = None) -> List[Dict[str, Any]]:
        since = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if notice_type:
                cursor.execute("""
                SELECT * FROM notices 
                WHERE created_at >= ? AND notice_type = ?
                ORDER BY id DESC
                """, (since, notice_type))
            else:
                cursor.execute("""
                SELECT * FROM notices 
                WHERE created_at >= ?
                ORDER BY id DESC
                """, (since,))
            rows = cursor.fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["matched_items"] = json.loads(d.get("matched_items") or "[]")
                res.append(d)
            return res
