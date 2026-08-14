"""
LINEメッセージ処理 - 予約フロー全般
"""

from linebot.models import (
    TextSendMessage,
    TemplateSendMessage,
    ButtonsTemplate,
    PostbackAction,
    URIAction,
    DatetimePickerAction,
    CarouselTemplate,
    CarouselColumn,
    FlexSendMessage
)
from datetime import datetime, date, timedelta
import json
import logging

logger = logging.getLogger(__name__)

class LineHandler:
    def __init__(self, line_bot_api, db, owner_user_id: str = None, liff_id: str = None):
        self.line_bot_api = line_bot_api
        self.db = db
        self.owner_user_id = owner_user_id
        self.liff_id = liff_id
        # セッション管理（簡易版）
        self.user_sessions = {}

    def notify_owner(self, text: str):
        """オーナーへの通知（未設定の場合は何もしない）"""
        if not self.owner_user_id:
            logger.warning("OWNER_USER_ID未設定のためオーナー通知をスキップしました")
            return
        try:
            self.line_bot_api.push_message(self.owner_user_id, TextSendMessage(text=text))
        except Exception as e:
            logger.error(f"Error notifying owner: {e}")
    
    # =======================================
    # ユーティリティ
    # =======================================
    
    def send_text(self, user_id: str, text: str):
        """テキストメッセージ送信"""
        try:
            message = TextSendMessage(text=text)
            self.line_bot_api.push_message(user_id, message)
        except Exception as e:
            logger.error(f"Error sending text message: {e}")
    
    def send_template_message(self, user_id: str, template):
        """テンプレートメッセージ送信"""
        try:
            message = TemplateSendMessage(
                alt_text="メッセージ",
                template=template
            )
            self.line_bot_api.push_message(user_id, message)
        except Exception as e:
            logger.error(f"Error sending template message: {e}")
    
    def send_flex_message(self, user_id: str, flex_json: dict):
        """Flex Message送信"""
        try:
            message = FlexSendMessage(
                alt_text="メッセージ",
                contents=flex_json
            )
            self.line_bot_api.push_message(user_id, message)
        except Exception as e:
            logger.error(f"Error sending flex message: {e}")
    
    # =======================================
    # 初期化・ヘルプ
    # =======================================
    
    def on_user_follow(self, user_id: str):
        """友達追加時のウェルカムメッセージ"""
        self.db.add_customer(user_id)
        
        welcome_text = """
👋 ようこそ！

私たちのLINE予約ボットへ。
こちらから簡単に予約・変更・キャンセルができます。

🎯 できることは...
✅ 予約受付（日時・メニュー選択）
✅ 予約の変更・キャンセル
✅ あなたの来店履歴確認
✅ ご質問へのお応答

「予約」ボタンをタップして、さっそく予約してみましょう！
"""
        self.send_text(user_id, welcome_text)
        self.show_help(user_id)
    
    def show_help(self, user_id: str):
        """ヘルプメッセージ（メニュー表示）"""
        template = ButtonsTemplate(
            title="メニュー",
            text="ご希望の操作を選択してください",
            actions=[
                PostbackAction(label="📅 予約する", data="action=start_booking"),
                PostbackAction(label="📋 マイページ", data="action=show_my_page"),
                PostbackAction(label="❓ ご質問", data="action=show_faq"),
            ]
        )
        self.send_template_message(user_id, template)
    
    # =======================================
    # 予約フロー
    # =======================================
    
    def start_booking(self, user_id: str):
        """予約開始"""
        self.user_sessions[user_id] = {}
        
        # 日付選択
        today = date.today()
        one_month_later = today + timedelta(days=30)
        
        template = TemplateSendMessage(
            alt_text="日付を選択してください",
            template=ButtonsTemplate(
                title="📅 ご来店希望日を選択",
                text="カレンダーからお選びください",
                actions=[
                    DatetimePickerAction(
                        label="日付を選択",
                        data="action=select_date",
                        mode='date',
                        initial=today.isoformat(),
                        min=today.isoformat(),
                        max=one_month_later.isoformat()
                    )
                ]
            )
        )
        self.line_bot_api.push_message(user_id, template)
    
    def on_date_selected(self, user_id: str, date_str: str):
        """日付選択処理"""
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {}
        
        self.user_sessions[user_id]['booking_date'] = date_str
        logger.info(f"Date selected for {user_id}: {date_str}")
        
        # 時間選択へ
        self.show_time_picker(user_id, date_str)
    
    def show_time_picker(self, user_id: str, date_str: str):
        """時間選択画面"""
        # 営業時間：10:00-19:00（1時間間隔）
        business_hours = []
        for hour in range(10, 19):
            time_str = f"{hour:02d}:00"
            business_hours.append(time_str)
        
        # キャロセルで時間を表示（LINEの制約：最大10個のボタン）
        columns = []
        for i in range(0, len(business_hours), 5):
            time_group = business_hours[i:i+5]
            actions = []
            for time_str in time_group:
                actions.append(
                    PostbackAction(
                        label=time_str,
                        data=f"action=select_time&time={time_str}"
                    )
                )
            
            # 足りない分はダミーで埋める
            while len(actions) < 5:
                actions.append(
                    PostbackAction(
                        label="　",
                        data="action=noop"
                    )
                )
            
            columns.append(
                CarouselColumn(
                    text="ご希望の時間を選択",
                    actions=actions
                )
            )
        
        # シンプル版：ボタンでたくさん表示
        template = ButtonsTemplate(
            title="⏰ ご来店希望時間",
            text=f"{date_str} のご利用時間をお選びください",
            actions=[
                PostbackAction(label="10:00", data="action=select_time&time=10:00"),
                PostbackAction(label="11:00", data="action=select_time&time=11:00"),
                PostbackAction(label="12:00", data="action=select_time&time=12:00"),
                PostbackAction(label="13:00", data="action=select_time&time=13:00"),
            ]
        )
        self.send_template_message(user_id, template)
        
        # さらに時間を送信
        template2 = ButtonsTemplate(
            title="⏰ ご来店希望時間（続き）",
            text="スクロールしてさらに選択",
            actions=[
                PostbackAction(label="14:00", data="action=select_time&time=14:00"),
                PostbackAction(label="15:00", data="action=select_time&time=15:00"),
                PostbackAction(label="16:00", data="action=select_time&time=16:00"),
                PostbackAction(label="17:00", data="action=select_time&time=17:00"),
            ]
        )
        self.send_template_message(user_id, template2)
        
        template3 = ButtonsTemplate(
            title="⏰ ご来店希望時間（最終）",
            text="最後の選択肢",
            actions=[
                PostbackAction(label="18:00", data="action=select_time&time=18:00"),
                PostbackAction(label="19:00", data="action=select_time&time=19:00"),
            ]
        )
        self.send_template_message(user_id, template3)
    
    def on_time_selected(self, user_id: str, time_str: str):
        """時間選択処理"""
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {}
        
        self.user_sessions[user_id]['booking_time'] = time_str
        logger.info(f"Time selected for {user_id}: {time_str}")
        
        # メニュー選択へ
        self.show_menu_selection(user_id)
    
    def show_menu_selection(self, user_id: str):
        """メニュー選択画面"""
        menus = self.db.get_all_menus()
        
        if not menus:
            self.send_text(user_id, "申し訳ございません。現在メニューが登録されていません。\nお問い合わせください。")
            return
        
        # メニューをCarouselで表示
        columns = []
        for menu in menus:
            menu_id, name, price, duration_minutes = menu[0], menu[1], menu[2], menu[3]
            columns.append(
                CarouselColumn(
                    title=name,
                    text=f"¥{price:,}\n所要時間: {duration_minutes}分",
                    actions=[
                        PostbackAction(
                            label="このメニューを選択",
                            data=f"action=select_menu&menu_id={menu_id}"
                        )
                    ]
                )
            )
        
        template = CarouselTemplate(columns=columns)
        self.send_template_message(user_id, template)
    
    def on_menu_selected(self, user_id: str, menu_id: str):
        """メニュー選択処理"""
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {}
        
        self.user_sessions[user_id]['menu_id'] = int(menu_id)
        logger.info(f"Menu selected for {user_id}: {menu_id}")
        
        # 予約内容確認へ
        self.show_booking_confirmation(user_id)
    
    def show_booking_confirmation(self, user_id: str):
        """予約内容確認"""
        session = self.user_sessions.get(user_id)
        if not session or 'booking_date' not in session or 'menu_id' not in session:
            self.send_text(user_id, "エラーが発生しました。もう一度予約してください。")
            return
        
        booking_date = session['booking_date']
        booking_time = session['booking_time']
        menu_id = session['menu_id']
        
        menu = self.db.get_menu(menu_id)
        if not menu:
            self.send_text(user_id, "メニューが見つかりません")
            return
        
        menu_name = menu[1]
        menu_price = menu[2]
        menu_duration = menu[3]
        
        # 確認メッセージ
        confirmation_text = f"""
✅ 予約内容の確認

📅 日時：{booking_date} {booking_time}
🎨 メニュー：{menu_name}
💰 料金：¥{menu_price:,}
⏱️  所要時間：{menu_duration}分

この内容でよろしいですか？
"""
        
        template = ButtonsTemplate(
            title="📝 予約内容確認",
            text=confirmation_text,
            actions=[
                PostbackAction(label="✅ 予約確定", data="action=confirm_booking"),
                PostbackAction(label="❌ キャンセル", data="action=start_booking"),
            ]
        )
        self.send_template_message(user_id, template)
    
    def confirm_booking(self, user_id: str):
        """予約確定（新規予約 または 予約変更の反映）"""
        session = self.user_sessions.get(user_id)
        if not session:
            self.send_text(user_id, "エラーが発生しました")
            return
        
        booking_date = session['booking_date']
        booking_time = session['booking_time']
        menu_id = session['menu_id']
        modify_booking_id = session.get('modify_booking_id')

        menu = self.db.get_menu(menu_id)
        menu_name = menu["name"] if menu else "不明"
        customer = self.db.get_customer(user_id)
        customer_name = customer["name"] if customer and customer["name"] else user_id[:10] + "..."

        if modify_booking_id:
            # ===== 予約変更の場合：既存予約を更新 =====
            original_date = session.get('original_date')
            original_time = session.get('original_time')

            self.db.update_booking(modify_booking_id, booking_date=booking_date,
                                   booking_time=booking_time, menu_id=menu_id)

            self.db.add_booking_history(
                booking_id=modify_booking_id, action="modified", user_id=user_id,
                before_date=original_date, before_time=original_time,
                after_date=booking_date, after_time=booking_time
            )

            self.send_text(user_id, f"""
✅ ご予約を変更しました

📅 変更後の日時: {booking_date} {booking_time}
📍 予約ID: {modify_booking_id}

ご来店をお待ちしております！
""")

            self.notify_owner(
                f"📝 予約変更がありました\n\n"
                f"お客様: {customer_name}\n"
                f"変更前: {original_date} {original_time}\n"
                f"変更後: {booking_date} {booking_time}\n"
                f"メニュー: {menu_name}\n"
                f"予約ID: {modify_booking_id}"
            )

            del self.user_sessions[user_id]
            return

        # ===== 新規予約の場合 =====
        booking_id = self.db.add_booking(user_id, booking_date, booking_time, menu_id)
        
        if booking_id:
            self.db.add_booking_history(
                booking_id=booking_id, action="created", user_id=user_id,
                after_date=booking_date, after_time=booking_time
            )

            self.send_text(user_id, f"""
🎉 ご予約ありがとうございます！

📅 {booking_date} {booking_time}
📍 予約ID: {booking_id}

予約確定メールをお送りしました。
ご来店の7日前・3日前にリマインダーをお送りいたします。

📞 ご質問やご変更は、いつでもお気軽にお連絡ください。
""")

            self.notify_owner(
                f"🆕 新規予約が入りました\n\n"
                f"お客様: {customer_name}\n"
                f"予約日時: {booking_date} {booking_time}\n"
                f"メニュー: {menu_name}\n"
                f"予約ID: {booking_id}"
            )
            
            # セッション削除
            del self.user_sessions[user_id]
        else:
            self.send_text(user_id, "予約の保存に失敗しました。もう一度お試しください。")
    
    # =======================================
    # マイページ・履歴
    # =======================================
    
    def show_my_page(self, user_id: str):
        """マイページ表示"""
        customer = self.db.get_customer(user_id)
        bookings = self.db.get_bookings_by_user(user_id, 'confirmed')
        
        if not customer:
            self.send_text(user_id, "登録情報が見つかりません")
            return
        
        # 来店履歴
        visit_history = self.db.get_visit_history(user_id, 5)
        
        flex_json = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "heading",
                        "text": "マイページ",
                        "level": 1
                    },
                    {
                        "type": "box",
                        "layout": "vertical",
                        "margin": "md",
                        "contents": [
                            {
                                "type": "text",
                                "text": "👤 登録情報",
                                "weight": "bold",
                                "size": "sm"
                            },
                            {
                                "type": "text",
                                "text": f"ユーザーID: {user_id[:20]}...",
                                "size": "xs",
                                "color": "#999999",
                                "margin": "sm"
                            }
                        ]
                    }
                ]
            }
        }
        
        # 次回予定の表示
        if bookings:
            next_booking = bookings[0]
            booking_date = next_booking[1]
            booking_time = next_booking[3]
            menu = self.db.get_menu(next_booking[4])
            
            flex_json["body"]["contents"].append({
                "type": "box",
                "layout": "vertical",
                "margin": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "📅 次回ご来店予定",
                        "weight": "bold",
                        "size": "sm"
                    },
                    {
                        "type": "text",
                        "text": f"{booking_date} {booking_time}",
                        "size": "sm",
                        "color": "#0066FF",
                        "margin": "sm"
                    },
                    {
                        "type": "text",
                        "text": f"メニュー: {menu[1] if menu else '不明'}",
                        "size": "xs",
                        "margin": "sm"
                    }
                ]
            })
        else:
            flex_json["body"]["contents"].append({
                "type": "box",
                "layout": "vertical",
                "margin": "md",
                "contents": [
                    {
                        "type": "text",
                        "text": "予約がありません",
                        "size": "sm",
                        "color": "#999999"
                    }
                ]
            })
        
        self.send_flex_message(user_id, flex_json)
        
        # 予約一覧（各予約に変更・キャンセルボタンを付ける）
        if bookings:
            for booking in bookings[:5]:
                booking_id = booking['id']
                menu = self.db.get_menu(booking['menu_id'])
                menu_name = menu['name'] if menu else '不明'

                actions = []
                if self.liff_id:
                    reschedule_url = (
                        f"https://liff.line.me/{self.liff_id}?"
                        f"modify_booking_id={booking_id}&menu_id={booking['menu_id']}&menu_name={menu_name}"
                    )
                    actions.append(URIAction(label="📝 日時を変更する", uri=reschedule_url))
                actions.append(PostbackAction(label="❌ キャンセルする", data=f"action=cancel_booking&booking_id={booking_id}"))

                template = ButtonsTemplate(
                    title=f"{booking['booking_date']} {booking['booking_time']}",
                    text=f"メニュー: {menu_name}\n予約ID: {booking_id}",
                    actions=actions
                )
                self.send_template_message(user_id, template)
        else:
            self.send_text(user_id, "現在ご予約はありません。「予約する」と送ると新規予約ができます。")
    
    # =======================================
    # 予約変更・キャンセル
    # =======================================
    
    def start_modify_booking(self, user_id: str, booking_id: str):
        """予約変更開始"""
        booking_id = int(booking_id)
        booking = self.db.get_booking(booking_id)
        
        if not booking or booking[2] != user_id:
            self.send_text(user_id, "予約が見つかりません")
            return
        
        self.user_sessions[user_id] = {
            'modify_booking_id': booking_id,
            'original_date': booking['booking_date'],
            'original_time': booking['booking_time'],
            # 変更しなかった項目もそのまま予約に反映できるよう、現在の内容で初期化しておく
            'booking_date': booking['booking_date'],
            'booking_time': booking['booking_time'],
            'menu_id': booking['menu_id'],
        }
        
        template = ButtonsTemplate(
            title="📝 予約を変更",
            text="変更する項目を選択",
            actions=[
                PostbackAction(label="📅 日付を変更", data=f"action=select_date"),
                PostbackAction(label="⏰ 時間を変更", data=f"action=select_time"),
                PostbackAction(label="❌ キャンセル", data=f"action=cancel_booking&booking_id={booking_id}"),
            ]
        )
        self.send_template_message(user_id, template)
    
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

        # オーナーへ通知
        customer = self.db.get_customer(user_id)
        menu = self.db.get_menu(booking["menu_id"])
        customer_name = customer["name"] if customer and customer["name"] else user_id[:10] + "..."
        menu_name = menu["name"] if menu else "不明"
        self.notify_owner(
            f"❌ 予約キャンセルがありました\n\n"
            f"お客様: {customer_name}\n"
            f"予約日時: {booking['booking_date']} {booking['booking_time']}\n"
            f"メニュー: {menu_name}\n"
            f"予約ID: {booking_id}"
        )
        
        template = ButtonsTemplate(
            title="✅ キャンセル完了",
            text=f"予約をキャンセルしました\n\n予約ID: {booking_id}",
            actions=[
                PostbackAction(label="🏠 ホームに戻る", data="action=show_help"),
            ]
        )
        self.send_template_message(user_id, template)
    
    # =======================================
    # その他
    # =======================================
    
    def show_faq(self, user_id: str):
        """よくある質問"""
        faq_text = """
❓ よくある質問

Q1. 予約のキャンセルはどうするの？
A. マイページから変更・キャンセルができます。

Q2. 予約時間に遅刻したら？
A. お電話でお知らせください。

Q3. メニューの詳細が知りたい
A. スタッフまでお気軽にお問い合わせください。

その他ご質問は、スタッフまでお気軽にお問い合わせください！
"""
        self.send_text(user_id, faq_text)
