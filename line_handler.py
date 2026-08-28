"""
LINEメッセージ処理 - LIFF統合版（軽量化済み）
"""

from linebot.models import (
    TextSendMessage,
    TemplateSendMessage,
    ButtonsTemplate,
    PostbackAction,
    URIAction,
    FlexSendMessage
)
from urllib.parse import quote
import logging

logger = logging.getLogger(__name__)

class LineHandler:
    def __init__(self, line_bot_api, db, owner_user_id: str = None, liff_id: str = None):
        self.line_bot_api = line_bot_api
        self.db = db
        self.owner_user_id = owner_user_id
        self.liff_id = liff_id

    def notify_owner(self, text: str):
        """オーナーへの通知"""
        if not self.owner_user_id:
            return
        try:
            self.line_bot_api.push_message(self.owner_user_id, TextSendMessage(text=text))
        except Exception as e:
            logger.error(f"Error notifying owner: {e}")

    def send_text(self, user_id: str, text: str):
        """テキストメッセージ送信"""
        try:
            self.line_bot_api.push_message(user_id, TextSendMessage(text=text))
        except Exception as e:
            logger.error(f"Error sending text message: {e}")

    def send_template_message(self, user_id: str, template):
        """テンプレートメッセージ送信"""
        try:
            self.line_bot_api.push_message(user_id, TemplateSendMessage(alt_text="メッセージ", template=template))
        except Exception as e:
            logger.error(f"Error sending template message: {e}")

    def send_flex_message(self, user_id: str, flex_json: dict):
        """Flex Message送信"""
        try:
            self.line_bot_api.push_message(user_id, FlexSendMessage(alt_text="マイページ", contents=flex_json))
        except Exception as e:
            logger.error(f"Error sending flex message: {e}")

    def on_user_follow(self, user_id: str):
        """友達追加時（DB追加のみ。メッセージはLINE公式側で制御）"""
        self.db.add_customer(user_id)

    def start_booking(self, user_id: str):
        """予約画面への案内"""
        if self.liff_id:
            template = ButtonsTemplate(
                title="📅 ご予約はこちらから",
                text="メニュー選択から日時の確認まで簡単に進められます",
                actions=[URIAction(label="予約画面を開く", uri=f"https://liff.line.me/{self.liff_id}")]
            )
            self.send_template_message(user_id, template)
        else:
            self.send_text(user_id, "予約画面のURLが設定されていません。")

    def show_my_page(self, user_id: str, reply_token: str = None):
        """マイページ・予約確認"""
        customer = self.db.get_customer(user_id)
        bookings = self.db.get_bookings_by_user(user_id, 'confirmed')
        
        if not customer:
            self._reply_or_push(user_id, reply_token, [TextSendMessage(text="登録情報が見つかりません")])
            return

        messages = []

        # 次回予約の表示（reply_messageは1回につき最大5件までなので、そのまま上限に一致）
        if bookings:
            for booking in bookings[:5]:
                booking_id = booking['id']
                shop_name = booking['shop_name'] if 'shop_name' in booking.keys() and booking['shop_name'] else 'URU SALON'
                
                # 複数メニュー対応の名称・IDを取得
                conn = self.db.get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT GROUP_CONCAT(m.name, ' + ') as menu_names,
                           GROUP_CONCAT(bm.menu_id) as menu_ids
                    FROM booking_menus bm
                    JOIN menus m ON bm.menu_id = m.id
                    WHERE bm.booking_id = ?
                    ORDER BY bm.sort_order
                ''', (booking_id,))
                row = cursor.fetchone()
                conn.close()

                menu_name = row['menu_names'] if (row and row['menu_names']) else '不明'
                menu_ids_str = row['menu_ids'] if (row and row['menu_ids']) else ''

                actions = []
                if self.liff_id:
                    reschedule_url = (
                        f"https://liff.line.me/{self.liff_id}?"
                        f"modify_booking_id={booking_id}&menu_name={quote(menu_name)}"
                        f"&shop_name={quote(shop_name)}"
                        f"&menu_ids={menu_ids_str}"
                    )
                    actions.append(URIAction(label="📝 日時を変更する", uri=reschedule_url))
                actions.append(PostbackAction(label="❌ キャンセルする", data=f"action=cancel_booking&booking_id={booking_id}"))

                template = ButtonsTemplate(
                    title=f"{booking['booking_date']} {booking['booking_time']}",
                    text=f"メニュー: {menu_name}\n予約ID: {booking_id}",
                    actions=actions
                )
                messages.append(TemplateSendMessage(alt_text="ご予約内容", template=template))
        else:
            messages.append(TextSendMessage(text="現在ご予約はありません。「予約する」ボタンからご予約ができます。"))

        self._reply_or_push(user_id, reply_token, messages)

    def _reply_or_push(self, user_id: str, reply_token: str, messages: list):
        """reply_tokenがあれば無料のreply APIで、無ければ従来通りpushで送信"""
        if reply_token:
            try:
                self.line_bot_api.reply_message(reply_token, messages)
                return
            except Exception as e:
                logger.error(f"Error replying, falling back to push: {e}")
        for msg in messages:
            try:
                self.line_bot_api.push_message(user_id, msg)
            except Exception as e:
                logger.error(f"Error pushing message: {e}")

    def cancel_booking(self, user_id: str, booking_id: str):
        """予約キャンセル"""
        booking_id = int(booking_id)
        booking = self.db.get_booking(booking_id)
        
        if not booking or booking[2] != user_id:
            self.send_text(user_id, "予約が見つかりません")
            return
        
        self.db.cancel_booking(booking_id)
        self.db.add_booking_history(
            booking_id=booking_id, action="cancelled", user_id=user_id,
            before_date=booking["booking_date"], before_time=booking["booking_time"]
        )

        customer = self.db.get_customer(user_id)
        customer_name = customer["name"] if (customer and customer["name"]) else user_id[:10] + "..."
        
        self.send_text(user_id, f"✅ 予約ID: {booking_id} のご予約をキャンセルしました。")
        self.notify_owner(
            f"❌ 予約キャンセルがありました\n\n"
            f"お客様: {customer_name}\n"
            f"予約日時: {booking['booking_date']} {booking['booking_time']}\n"
            f"予約ID: {booking_id}"
        )
