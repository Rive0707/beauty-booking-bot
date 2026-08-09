"""
自動リマインダー機能
予約の7日前に自動でリマインドメッセージを送信
"""

from datetime import datetime, date, timedelta
from linebot.models import (
    TextSendMessage,
    TemplateSendMessage,
    ButtonsTemplate,
    PostbackAction
)
import logging

logger = logging.getLogger(__name__)

class ReminderScheduler:
    def __init__(self, line_bot_api, db):
        self.line_bot_api = line_bot_api
        self.db = db
    
    def check_and_send_reminders(self):
        """
        定期的に実行：7日以内の予約でリマインド未送信のものを検索して送信
        APSchedulerから1時間ごとに呼ばれる
        """
        logger.info("Checking for reminders to send...")
        
        try:
            # 今後7日間の予約を取得
            upcoming_bookings = self.db.get_upcoming_bookings(days_ahead=7)
            
            today = date.today()
            
            for booking in upcoming_bookings:
                booking_id = booking[0]
                booking_date_str = booking[1]
                user_id = booking[2]
                booking_time = booking[3]
                menu_id = booking[4]
                reminder_sent = booking[7]
                
                # リマインド送信済みはスキップ
                if reminder_sent:
                    continue
                
                # 日付パース
                try:
                    booking_date = datetime.strptime(booking_date_str, "%Y-%m-%d").date()
                except:
                    logger.error(f"Invalid date format: {booking_date_str}")
                    continue
                
                # 予約日と今日の差分
                days_until_booking = (booking_date - today).days
                
                # 正確に7日前、またはそれより前でまだ送信していない場合
                if 0 <= days_until_booking <= 7:
                    self.send_reminder(user_id, booking_id, booking_date_str, booking_time, menu_id)
                    # リマインド送信完了をマーク
                    self.db.mark_reminder_sent(booking_id)
        
        except Exception as e:
            logger.error(f"Error in check_and_send_reminders: {e}")
    
    def send_reminder(self, user_id: str, booking_id: int, booking_date: str, 
                     booking_time: str, menu_id: int):
        """リマインドメッセージ送信"""
        try:
            menu = self.db.get_menu(menu_id)
            if not menu:
                logger.error(f"Menu not found: {menu_id}")
                return
            
            menu_name = menu[1]
            
            # テキストメッセージ
            reminder_text = f"""
🔔 ご予約のリマインド

お疲れ様です！ご予約いただいたご来店日が近づいてきました。

📅 予約日時: {booking_date} {booking_time}
🎨 メニュー: {menu_name}
📍 予約ID: {booking_id}

◆ ご来店前の確認事項
✅ 事前に予約内容をご確認ください
✅ ご変更・キャンセルはお早めにお知らせください
✅ 時間に余裕を持ってご来店ください

ご質問やご不明な点がございましたら、いつでもお気軽にお問い合わせください。

本日のご来店をお待ちしております！
"""
            
            message = TextSendMessage(text=reminder_text)
            self.line_bot_api.push_message(user_id, message)
            
            # テンプレートメッセージで選択肢を提供
            template = ButtonsTemplate(
                title="📞 ご変更・ご質問はこちら",
                text="予約内容の変更やキャンセルはこちらからどうぞ",
                actions=[
                    PostbackAction(label="📝 予約を変更する", data=f"action=modify_booking&booking_id={booking_id}"),
                    PostbackAction(label="❌ キャンセルする", data=f"action=cancel_booking&booking_id={booking_id}"),
                    PostbackAction(label="✅ 予約確定", data="action=show_help"),
                ]
            )
            
            template_message = TemplateSendMessage(
                alt_text="ご予約のリマインド",
                template=template
            )
            self.line_bot_api.push_message(user_id, template_message)
            
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
