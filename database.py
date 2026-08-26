"""
SQLiteデータベース管理
顧客、予約、メニュー、来店履歴を管理
"""

from typing import Optional
import os
import sqlite3
from datetime import datetime, date, timedelta
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get("DB_PATH", "/data/beauty_booking.db")
        self.conn = None

    def get_connection(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        # 1. 顧客テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                user_id TEXT PRIMARY KEY,
                name TEXT,
                furigana TEXT,
                gender TEXT,
                birthdate TEXT,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 2. メニューテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS menus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                duration_minutes INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 3. 予約テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_date TEXT NOT NULL,
                user_id TEXT NOT NULL,
                booking_time TEXT NOT NULL,
                menu_id INTEGER NOT NULL,
                shop_name TEXT DEFAULT 'URU SALON',
                last_visit_date TEXT,
                notes TEXT,
                status TEXT DEFAULT 'confirmed',
                reminder_sent INTEGER DEFAULT 0,
                reminder_7d_sent INTEGER DEFAULT 0,
                reminder_3d_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES customers(user_id),
                FOREIGN KEY (menu_id) REFERENCES menus(id)
            )
        ''')

        # 4. 複数メニュー中間テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS booking_menus (
                booking_id INTEGER NOT NULL,
                menu_id INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (booking_id, menu_id),
                FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
                FOREIGN KEY (menu_id) REFERENCES menus(id) ON DELETE CASCADE
            )
        ''')

        # 5. 来店履歴・カルテテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS visit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                booking_id INTEGER,
                visit_date TEXT NOT NULL,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES customers(user_id)
            )
        ''')

        # 6. 変更履歴テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS booking_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                user_id TEXT NOT NULL,
                before_date TEXT,
                before_time TEXT,
                after_date TEXT,
                after_time TEXT,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 7. 臨時休業日テーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS closed_days (
                closed_date TEXT PRIMARY KEY,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 8. 顧客メモテーブル
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS customer_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # ★ 既存データベースのカラム自動マイグレーション（カラム不足による500エラー防止）
        cursor.execute("PRAGMA table_info(bookings)")
        columns = [column[1] for column in cursor.fetchall()]

        if "shop_name" not in columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN shop_name TEXT DEFAULT 'URU SALON'")

        if "last_visit_date" not in columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN last_visit_date TEXT")

        if "reminder_7d_sent" not in columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN reminder_7d_sent INTEGER DEFAULT 0")

        if "reminder_3d_sent" not in columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN reminder_3d_sent INTEGER DEFAULT 0")

        # ★ booking_menus テーブルのカラム自動マイグレーション
        cursor.execute("PRAGMA table_info(booking_menus)")
        bm_columns = [column[1] for column in cursor.fetchall()]

        if "sort_order" not in bm_columns:
            cursor.execute("ALTER TABLE booking_menus ADD COLUMN sort_order INTEGER DEFAULT 0")

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")

    def add_booking_history(self, booking_id: int, action: str, user_id: str = None,
                          before_date: str = None, before_time: str = None,
                          after_date: str = None, after_time: str = None, note: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO booking_history
                (booking_id, user_id, action, before_date, before_time, after_date, after_time, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (booking_id, user_id, action, before_date, before_time, after_date, after_time, note))
            conn.commit()
        except Exception as e:
            logger.error(f"Error adding booking history: {e}")
        finally:
            conn.close()

    def get_booking_history(self, limit: int = 50):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT h.*, c.name as customer_name, m.name as menu_name
            FROM booking_history h
            LEFT JOIN bookings b ON h.booking_id = b.id
            LEFT JOIN customers c ON h.user_id = c.user_id
            LEFT JOIN menus m ON b.menu_id = m.id
            ORDER BY h.created_at DESC
            LIMIT ?
        ''', (limit,))
        results = cursor.fetchall()
        conn.close()
        return results

    def get_all_upcoming_bookings(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        today = date.today().isoformat()
        cursor.execute('''
            SELECT b.*, c.name as customer_name, c.phone as customer_phone, m.name as menu_name
            FROM bookings b
            LEFT JOIN customers c ON b.user_id = c.user_id
            LEFT JOIN menus m ON b.menu_id = m.id
            WHERE b.booking_date >= ? AND b.status = 'confirmed'
            ORDER BY b.booking_date, b.booking_time
        ''', (today,))
        results = cursor.fetchall()
        conn.close()
        return results

    def get_all_bookings_with_details(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, c.name as customer_name, c.phone as customer_phone, m.name as menu_name
            FROM bookings b
            LEFT JOIN customers c ON b.user_id = c.user_id
            LEFT JOIN menus m ON b.menu_id = m.id
            ORDER BY b.booking_date DESC, b.booking_time DESC
        ''')
        results = cursor.fetchall()
        conn.close()
        return results

    def get_bookings_with_details_in_range(self, start_date_str: str, end_date_str: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT b.*, c.name as customer_name, c.phone as customer_phone, m.name as menu_name
            FROM bookings b
            LEFT JOIN customers c ON b.user_id = c.user_id
            LEFT JOIN menus m ON b.menu_id = m.id
            WHERE b.booking_date >= ? AND b.booking_date <= ? AND b.status = 'confirmed'
            ORDER BY b.booking_date, b.booking_time
        ''', (start_date_str, end_date_str))
        results = cursor.fetchall()
        conn.close()
        return results

    def add_customer(self, user_id: str, name: str = None, phone: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO customers (user_id, name, phone)
                VALUES (?, ?, ?)
            ''', (user_id, name, phone))
            conn.commit()
            logger.info(f"Customer added: {user_id}")
        except Exception as e:
            logger.error(f"Error adding customer: {e}")
        finally:
            conn.close()
    def has_active_bookings(self, user_id: str) -> bool:
        """confirmed状態の予約が残っているか確認"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) as cnt FROM bookings
            WHERE user_id = ? AND status = 'confirmed'
        ''', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row["cnt"] > 0

    def delete_customer(self, user_id: str) -> bool:
        """お客様情報を削除（予約が残っていないことは呼び出し側で確認済み前提）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM customers WHERE user_id = ?', (user_id,))
            conn.commit()
            logger.info(f"Customer deleted: {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting customer: {e}")
            return False
        finally:
            conn.close()

    def get_customer(self, user_id: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM customers WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def get_all_customers(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM customers')
        results = cursor.fetchall()
        conn.close()
        return results

    def update_customer(self, user_id: str, name: str = None, phone: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if name:
            cursor.execute('UPDATE customers SET name = ? WHERE user_id = ?', (name, user_id))
        if phone:
            cursor.execute('UPDATE customers SET phone = ? WHERE user_id = ?', (phone, user_id))
        conn.commit()
        conn.close()

    def save_customer_profile(self, user_id: str, name: str, furigana: str = None,
                              gender: str = None, birthdate: str = None, phone: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO customers (user_id, name, furigana, gender, birthdate, phone)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    furigana = excluded.furigana,
                    gender = excluded.gender,
                    birthdate = excluded.birthdate,
                    phone = excluded.phone
            ''', (user_id, name, furigana, gender, birthdate, phone))
            conn.commit()
            logger.info(f"Customer profile saved: {user_id}")
        except Exception as e:
            logger.error(f"Error saving customer profile: {e}")
            raise
        finally:
            conn.close()

    def add_menu(self, name: str, price: int, duration_minutes: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM menus')
            next_order = cursor.fetchone()[0]
            cursor.execute('''
                INSERT INTO menus (name, price, duration_minutes, sort_order)
                VALUES (?, ?, ?, ?)
            ''', (name, price, duration_minutes, next_order))
            conn.commit()
            menu_id = cursor.lastrowid
            logger.info(f"Menu added: {name} (ID: {menu_id})")
            return menu_id
        except Exception as e:
            logger.error(f"Error adding menu: {e}")
            return None
        finally:
            conn.close()

    def get_menu(self, menu_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM menus WHERE id = ?', (menu_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def get_all_menus(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM menus ORDER BY sort_order ASC, id ASC')
        results = cursor.fetchall()
        conn.close()
        return results

    def reorder_menus(self, menu_ids: list) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            for index, menu_id in enumerate(menu_ids):
                cursor.execute('UPDATE menus SET sort_order = ? WHERE id = ?', (index, menu_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error reordering menus: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def delete_menu(self, menu_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM menus WHERE id = ?', (menu_id,))
            conn.commit()
            logger.info(f"Menu deleted: {menu_id}")
        except Exception as e:
            logger.error(f"Error deleting menu: {e}")
        finally:
            conn.close()
    def is_slot_available(self, booking_date: str, booking_time: str, exclude_booking_id: int = None) -> bool:
        """その日時が他の予約と重複していないか確認（1件＝60分埋まる前提）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, booking_time FROM bookings
            WHERE booking_date = ? AND status = 'confirmed'
        ''', (booking_date,))
        rows = cursor.fetchall()
        conn.close()

        new_h, new_m = map(int, booking_time.split(":"))
        new_start = new_h * 60 + new_m

        for row in rows:
            if exclude_booking_id and row["id"] == exclude_booking_id:
                continue
            b_h, b_m = map(int, row["booking_time"].split(":"))
            b_start = b_h * 60 + b_m
            if abs(new_start - b_start) < 60:
                return False
        return True

    def add_booking(self, user_id: str, booking_date: str, booking_time: str,
                    menu_ids: list, notes: str = None, shop_name: str = "URU SALON") -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self.add_customer(user_id)
            
            # ① 既存の bookings テーブルには「最初のメニューID」を入れる（後方互換）
            primary_menu_id = menu_ids[0] if menu_ids else 1
            cursor.execute('''
                INSERT INTO bookings (booking_date, user_id, booking_time, menu_id, notes, status, shop_name)
                VALUES (?, ?, ?, ?, ?, 'confirmed', ?)
            ''', (booking_date, user_id, booking_time, primary_menu_id, notes, shop_name))
            booking_id = cursor.lastrowid
            
            # ② 新しい中間テーブル booking_menus に、選択したすべてのメニューを登録
            for idx, mid in enumerate(menu_ids):
                cursor.execute('''
                    INSERT INTO booking_menus (booking_id, menu_id, sort_order)
                    VALUES (?, ?, ?)
                ''', (booking_id, mid, idx))
            
            conn.commit()
            logger.info(f"Booking added: {booking_id} with menus {menu_ids}")
            return booking_id
        except Exception as e:
            logger.error(f"Error adding booking: {e}")
            return None
        finally:
            conn.close()

    def get_booking(self, booking_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def get_bookings_by_user(self, user_id: str, status: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if status:
            cursor.execute('''
                SELECT * FROM bookings
                WHERE user_id = ? AND status = ?
                ORDER BY booking_date DESC, booking_time DESC
            ''', (user_id, status))
        else:
            cursor.execute('''
                SELECT * FROM bookings
                WHERE user_id = ?
                ORDER BY booking_date DESC, booking_time DESC
            ''', (user_id,))
        results = cursor.fetchall()
        conn.close()
        return results

    def get_bookings_by_date(self, booking_date: date):
        conn = self.get_connection()
        cursor = conn.cursor()
        date_str = booking_date.isoformat()
        cursor.execute('''
            SELECT * FROM bookings
            WHERE booking_date = ? AND status = 'confirmed'
            ORDER BY booking_time
        ''', (date_str,))
        results = cursor.fetchall()
        conn.close()
        return results

    def get_booked_times_in_range(self, start_date_str: str, end_date_str: str, exclude_booking_id: int = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if exclude_booking_id:
            cursor.execute('''
                SELECT booking_date, booking_time FROM bookings
                WHERE booking_date >= ? AND booking_date <= ? AND status = 'confirmed' AND id != ?
            ''', (start_date_str, end_date_str, exclude_booking_id))
        else:
            cursor.execute('''
                SELECT booking_date, booking_time FROM bookings
                WHERE booking_date >= ? AND booking_date <= ? AND status = 'confirmed'
            ''', (start_date_str, end_date_str))
        results = cursor.fetchall()
        conn.close()

        booked = {}
        for row in results:
            booked.setdefault(row["booking_date"], []).append(row["booking_time"])
        return booked

    def get_upcoming_bookings(self, days_ahead: int = 7):
        conn = self.get_connection()
        cursor = conn.cursor()
        today = datetime.now().date()
        target_date = today + timedelta(days=days_ahead)

        cursor.execute('''
            SELECT * FROM bookings
            WHERE booking_date >= ? AND booking_date <= ? AND status = 'confirmed'
            ORDER BY booking_date, booking_time
        ''', (today.isoformat(), target_date.isoformat()))
        results = cursor.fetchall()
        conn.close()
        return results

    def mark_reminder_7d_sent(self, booking_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE bookings SET reminder_7d_sent = 1 WHERE id = ?', (booking_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Error marking 7d reminder sent: {e}")
        finally:
            conn.close()

    def mark_reminder_3d_sent(self, booking_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE bookings SET reminder_3d_sent = 1 WHERE id = ?', (booking_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Error marking 3d reminder sent: {e}")
        finally:
            conn.close()

    def confirm_no_change(self, booking_id: int):
        self.mark_reminder_3d_sent(booking_id)

    def cancel_booking(self, booking_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                UPDATE bookings SET status = 'cancelled' WHERE id = ?
            ''', (booking_id,))
            conn.commit()
            logger.info(f"Booking cancelled: {booking_id}")
        except Exception as e:
            logger.error(f"Error cancelling booking: {e}")
        finally:
            conn.close()

    def update_booking(self, booking_id: int, booking_date: str = None,
                       booking_time: str = None, menu_id: int = None,
                       menu_ids: list = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if booking_date:
                cursor.execute('UPDATE bookings SET booking_date = ? WHERE id = ?',
                               (booking_date, booking_id))
            if booking_time:
                cursor.execute('UPDATE bookings SET booking_time = ? WHERE id = ?',
                               (booking_time, booking_id))
            if menu_id:
                cursor.execute('UPDATE bookings SET menu_id = ? WHERE id = ?',
                               (menu_id, booking_id))
            # ★ 複数メニュー対応：中間テーブルを置き換え更新
            if menu_ids is not None:
                cursor.execute('DELETE FROM booking_menus WHERE booking_id = ?', (booking_id,))
                for idx, m_id in enumerate(menu_ids):
                    cursor.execute('''
                        INSERT INTO booking_menus (booking_id, menu_id, sort_order)
                        VALUES (?, ?, ?)
                    ''', (booking_id, m_id, idx))
            conn.commit()
            logger.info(f"Booking updated: {booking_id}")
        except Exception as e:
            logger.error(f"Error updating booking: {e}")
            conn.rollback()
        finally:
            conn.close()

    def mark_reminder_sent(self, booking_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE bookings SET reminder_sent = 1 WHERE id = ?',
                           (booking_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Error marking reminder sent: {e}")
        finally:
            conn.close()

    def add_customer_note(self, user_id: str, note: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO customer_notes (user_id, note)
                VALUES (?, ?)
            ''', (user_id, note))
            conn.commit()
            logger.info(f"Note added for {user_id}")
        except Exception as e:
            logger.error(f"Error adding note: {e}")
        finally:
            conn.close()

    def get_customer_notes(self, user_id: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM customer_notes
            WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        results = cursor.fetchall()
        conn.close()
        return results

    def add_visit_record(self, user_id: str, booking_id: int, memo: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            visited_date = datetime.now().date().isoformat()
            cursor.execute('''
                INSERT INTO visit_history (user_id, booking_id, visited_date, memo)
                VALUES (?, ?, ?, ?)
            ''', (user_id, booking_id, visited_date, memo))
            conn.commit()
            logger.info(f"Visit record added for {user_id}")
        except Exception as e:
            logger.error(f"Error adding visit record: {e}")
        finally:
            conn.close()

    def get_visit_history(self, user_id: str, limit: int = 5):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT vh.*, b.menu_id, m.name
            FROM visit_history vh
            LEFT JOIN bookings b ON vh.booking_id = b.id
            LEFT JOIN menus m ON b.menu_id = m.id
            WHERE vh.user_id = ?
            ORDER BY vh.visited_date DESC
            LIMIT ?
        ''', (user_id, limit))
        results = cursor.fetchall()
        conn.close()
        return results
        
    def merge_customers(self, manual_user_id: str, line_user_id: str) -> bool:
        """手動登録の顧客データをLINEユーザーへ統合し、旧手動データを削除
        情報の引き継ぎ: LINE側が未入力の項目だけ、手動側の値で埋める（LINE側の入力済みデータは上書きしない）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT * FROM customers WHERE user_id = ?', (manual_user_id,))
            manual_customer = cursor.fetchone()
            cursor.execute('SELECT * FROM customers WHERE user_id = ?', (line_user_id,))
            line_customer = cursor.fetchone()

            if manual_customer and line_customer:
                for field in ("name", "phone", "furigana", "gender", "birthdate"):
                    if not line_customer[field] and manual_customer[field]:
                        cursor.execute(
                            f'UPDATE customers SET {field} = ? WHERE user_id = ?',
                            (manual_customer[field], line_user_id)
                        )

            cursor.execute('UPDATE bookings SET user_id = ? WHERE user_id = ?', (line_user_id, manual_user_id))
            cursor.execute('UPDATE booking_history SET user_id = ? WHERE user_id = ?', (line_user_id, manual_user_id))
            cursor.execute('UPDATE visit_history SET user_id = ? WHERE user_id = ?', (line_user_id, manual_user_id))
            cursor.execute('DELETE FROM customers WHERE user_id = ?', (manual_user_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error merging customers: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def add_visit_history(self, user_id: str, booking_id: Optional[int], visited_date: str, notes: str):
        """来店履歴・カルテの追加保存"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # booking_id が None の場合は NOT NULL 制約回避のため 0 を代入
            valid_booking_id = booking_id if booking_id is not None else 0
            
            cursor.execute("""
                INSERT INTO visit_history (user_id, booking_id, visited_date, notes, created_at)
                VALUES (?, ?, ?, ?, DATETIME('now', 'localtime'))
            """, (user_id, valid_booking_id, visited_date, notes))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error adding visit history: {e}")
            return False

    def update_visit_history_notes(self, visit_id: int, notes: str) -> bool:
        """既存のカルテメモを更新（追記・修正）"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE visit_history SET notes = ? WHERE id = ?', (notes, visit_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error updating visit history: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def add_closed_day(self, closed_date: str, note: str = None) -> bool:
        """休業日を追加"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT OR REPLACE INTO closed_days (closed_date, note) VALUES (?, ?)', (closed_date, note))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding closed day: {e}")
            return False
        finally:
            conn.close()

    def get_closed_days(self):
        """休業日一覧を取得"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT * FROM closed_days ORDER BY closed_date ASC')
            results = cursor.fetchall()
            return results
        except Exception as e:
            logger.error(f"Error getting closed days: {e}")
            return []
        finally:
            conn.close()

    def delete_closed_day(self, closed_date: str) -> bool:
        """休業日を削除"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM closed_days WHERE closed_date = ?', (closed_date,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error deleting closed day: {e}")
            return False
        finally:
            conn.close()

    def update_booking_status(self, booking_id: int, status: str) -> bool:
        """予約のステータス（confirmed / completed / cancelled など）を更新"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE bookings SET status = ? WHERE id = ?', (status, booking_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error updating booking status: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def update_menu(self, menu_id: int, name: str, price: int, duration_minutes: int):
        """メニュー情報の更新"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE menus 
                SET name = ?, price = ?, duration_minutes = ?
                WHERE id = ?
            """, (name, price, duration_minutes, menu_id))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating menu: {e}")
            return False
