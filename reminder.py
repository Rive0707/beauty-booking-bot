"""
自動リマインダー機能
予約の7日前に自動でリマインドメッセージを送信
"""

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from linebot.models import (
    TextSendMessage,
    TemplateSendMessage,
    ButtonsTemplate,
    PostbackAction,
    URIAction
)
import logging

logger = logging.getLogger(__name__)

class ReminderScheduler:
    def __init__(self, line_bot_api, db, liff_id: str = None):
        self.line_bot_api = line_bot_api
        self.db = db
        self.liff_id = liff_id
    
    def check_and_send_reminders(self):
        """
        定期的に実行：7日前・3日前のリマインドをそれぞれ1回だけ送信する
        APSchedulerから1時間ごとに呼ばれる
        """
        logger.info("Checking for reminders to send...")
        
        try:
            # 今後7日間の予約を取得
            upcoming_bookings = self.db.get_upcoming_bookings(days_ahead=7)
            
            # タイ時間（Asia/Bangkok）を基準に「今」を判定する
            now_local = datetime.now(ZoneInfo("Asia/Bangkok"))
            today = now_local.date()
            
            for booking in upcoming_bookings:
                booking_id = booking[0]
                booking_date_str = booking[1]
                user_id = booking[2]
                booking_time = booking[3]
                menu_id = booking[4]
                reminder_7d_sent = booking["reminder_7d_sent"] if "reminder_7d_sent" in booking.keys() else 0
                reminder_3d_sent = booking["reminder_3d_sent"] if "reminder_3d_sent" in booking.keys() else 0
                
                # 日付パース
                try:
                    booking_date = datetime.strptime(booking_date_str, "%Y-%m-%d").date()
                except:
                    logger.error(f"Invalid date format: {booking_date_str}")
                    continue

                # ★ 予約の日時（日付＋時刻）がすでに過ぎていたら、リマインドを送らず送信済み扱いにする
                try:
                    booking_datetime = datetime.strptime(
                        f"{booking_date_str} {booking_time}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=ZoneInfo("Asia/Bangkok"))
                    if booking_datetime <= now_local:
                        if not reminder_3d_sent:
                            self.db.mark_reminder_3d_sent(booking_id)
                        if not reminder_7d_sent:
                            self.db.mark_reminder_7d_sent(booking_id)
                        continue
                except ValueError:
                    logger.error(f"Invalid time format for booking {booking_id}: {booking_date_str} {booking_time}")
                
                # 予約日と今日の差分
                days_until_booking = (booking_date - today).days
                
                # 残り日数に応じて、7日前・3日前のリマインドを「1回のチェックにつき1通だけ」送る
                # （サーバーが一時停止していても、次に動いた時に取りこぼさず送れるよう <= で判定）
                shop_name = booking["shop_name"] if "shop_name" in booking.keys() else "URU SALON"
                if days_until_booking <= 3 and not reminder_3d_sent:
                    self.send_reminder(user_id, booking_id, booking_date_str, booking_time, menu_id, days_label=f"あと{days_until_booking}日", shop_name=shop_name)
                    self.db.mark_reminder_3d_sent(booking_id)
                    if not reminder_7d_sent:
                        self.db.mark_reminder_7d_sent(booking_id)
                elif days_until_booking <= 7 and not reminder_7d_sent:
                    self.send_reminder(user_id, booking_id, booking_date_str, booking_time, menu_id, days_label=f"あと{days_until_booking}日", shop_name=shop_name)
                    self.db.mark_reminder_7d_sent(booking_id)
        
        except Exception as e:
            logger.error(f"Error in check_and_send_reminders: {e}")
    
    def send_reminder(self, user_id: str, booking_id: int, booking_date: str, 
                     booking_time: str, menu_id: int, days_label: str = "", shop_name: str = "URU SALON"):
        """リマインドメッセージ送信"""
        try:
            menu = self.db.get_menu(menu_id)
            if not menu:
                logger.error(f"Menu not found: {menu_id}")
                return
            
            menu_name = menu[1]
            
            # テキストメッセージ
            reminder_text = f"""
🔔 ご予約のリマインド（{days_label}）

ご予約いただいたご来店日が近づいてきました。

📅 予約日時: {booking_date} {booking_time}
🎨 メニュー: {menu_name}

◆ ご来店前の確認事項
✅ 事前に予約内容をご確認ください。
✅ ご変更・キャンセルはお早めにお知らせください。
✅ 30分以上の遅刻は自動的にキャンセルとなりますのでお気をつけください。

ご質問やご不明な点がございましたら、いつでもお気軽にお問い合わせください。

ご来店をお待ちしております！
"""
            
            message = TextSendMessage(text=reminder_text)

            # テンプレートメッセージで選択肢を提供（7日前・3日前で共通）
            actions = []
            if self.liff_id:
                reschedule_url = (
                    f"https://liff.line.me/{self.liff_id}?"
                    f"modify_booking_id={booking_id}&menu_id={menu_id}&menu_name={quote(menu_name)}"
                    f"&shop_name={quote(shop_name)}"
                )
                actions.append(URIAction(label="📝 予約変更", uri=reschedule_url))
            actions.append(PostbackAction(label="✅ 予約変更なし", data=f"action=confirm_no_change&booking_id={booking_id}"))

            template = ButtonsTemplate(
                title="📞 ご予約の確認",
                text="変更がある場合はこちらから、なければ「予約変更なし」を選んでください",
                actions=actions
            )
            
            template_message = TemplateSendMessage(
                alt_text="ご予約のリマインド",
                template=template
            )

            # 1回の送信にまとめる（LINEの無料メッセージ通数を1通分に節約するため）
            self.line_bot_api.push_message(user_id, [message, template_message])
            
            logger.info(f"Reminder sent to {user_id} for booking {booking_id}")
        
        except Exception as e:
            logger.error(f"Error sending reminder to {user_id}: {e}")
    
    def send_daily_summary_to_owner(self, owner_user_id: str):
        """
        オーナー向け：翌日の予約サマリーを送信
        必要に応じて追加のエンドポイントから呼出
        """
        try:
            tomorrow = date.today() + timedelta(days=1)
            bookings = self.db.get_bookings_by_date(tomorrow)
            
            if not bookings:
                summary_text = f"""
📅 {tomorrow.strftime('%m月%d日')} の予約

本日の予約はありません。
"""
            else:
                summary_text = f"""
📅 {tomorrow.strftime('%m月%d日')} の予約

"""
                for booking in bookings:
                    customer = self.db.get_customer(booking[2])
                    menu = self.db.get_menu(booking[4])
                    customer_name = customer[1] if customer and customer[1] else "不明"
                    menu_name = menu[1] if menu else "不明"
                    
                    summary_text += f"⏰ {booking[3]} - {customer_name} ({menu_name})\n"
            
            message = TextSendMessage(text=summary_text)
            self.line_bot_api.push_message(owner_user_id, message)
            
            logger.info(f"Daily summary sent to owner")
        
        except Exception as e:
            logger.error(f"Error sending daily summary: {e}")
