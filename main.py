"""
LINE美容室予約BOT メインアプリケーション - 完全版
FastAPI + LINE Messaging API + SQLite + APScheduler
顧客選択型予約追加機能を統合済み
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent, FollowEvent
from pydantic import BaseModel
import os
import logging
from datetime import datetime, timedelta
import json
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# 自作モジュール
from database import Database
from line_handler import LineHandler
from reminder import ReminderScheduler
from config import BUSINESS_HOURS_START, BUSINESS_HOURS_END, SLOT_INTERVAL_MINUTES, CLOSED_WEEKDAYS

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数から取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OWNER_USER_ID = os.getenv("OWNER_USER_ID")

# チェック
if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OWNER_USER_ID]):
    raise ValueError("必須環境変数が設定されていません: LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OWNER_USER_ID")

# FastAPI初期化
app = FastAPI(title="Beauty Booking Bot")

# CORS対応
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静的ファイル配信（LIFFページ用）
app.mount("/static", StaticFiles(directory="static"), name="static")

# LINE Bot初期化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# DB初期化
db = Database()
db.init_db()

# LINE ハンドラー初期化
line_handler = LineHandler(line_bot_api, db)

# リマインダー初期化
reminder_scheduler = ReminderScheduler(line_bot_api, db)

# APScheduler 設定（バックグラウンドリマインド）
scheduler = BackgroundScheduler()
scheduler.add_job(
    reminder_scheduler.check_and_send_reminders,
    'interval',
    hours=1,
    id='reminder_job'
)
scheduler.start()

# プロセス終了時にスケジューラーを停止
atexit.register(lambda: scheduler.shutdown())

# ===============================
# Pydantic モデル
# ===============================

class BookingAddWithCustomerRequest(BaseModel):
    customer_id: str
    booking_date: str
    booking_time: str
    menu_id: int
    notes: str = None

class MenuAddRequest(BaseModel):
    name: str
    price: int
    duration_minutes: int

class BookingCreateFromLiffRequest(BaseModel):
    user_id: str
    menu_id: int
    booking_date: str
    booking_time: str
    name: str
    furigana: str = None
    gender: str = None
    birthdate: str = None
    phone: str = None

# ===============================
# LINE Webhook エンドポイント
# ===============================

@app.post("/callback")
async def callback(request: Request):
    """LINE Webhook受信エンドポイント"""
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    return JSONResponse({"status": "ok"})

# ===============================
# LINEメッセージハンドラー
# ===============================

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """テキストメッセージ処理"""
    user_id = event.source.user_id
    text = event.message.text
    
    logger.info(f"Message from {user_id}: {text}")
    
    # 予約フロー開始
    if text in ["予約", "予約する"]:
        line_handler.start_booking(user_id)
    
    # マイページ
    elif text in ["マイページ", "履歴"]:
        line_handler.show_my_page(user_id)
    
    # ヘルプ
    elif text in ["ヘルプ", "メニュー"]:
        line_handler.show_help(user_id)
    
    # オーナーコマンド
    elif user_id == OWNER_USER_ID:
        handle_owner_command(user_id, text)
    
    else:
        line_handler.send_text(user_id, "「予約」「マイページ」などのボタンを使ってください")

@handler.add(PostbackEvent)
def handle_postback(event):
    """ポストバック処理（ボタン・日時選択など）"""
    user_id = event.source.user_id
    postback_data = event.postback.data
    
    logger.info(f"Postback from {user_id}: {postback_data}")
    
    # ポストバックデータをパース
    params = {}
    for param in postback_data.split("&"):
        k, v = param.split("=")
        params[k] = v
    
    action = params.get("action")
    
    # 日付選択
    if action == "select_date":
        date_str = event.postback.params.get("date")
        line_handler.on_date_selected(user_id, date_str)
    
    # 時間選択
    elif action == "select_time":
        time_str = event.postback.params.get("time")
        line_handler.on_time_selected(user_id, time_str)
    
    # メニュー選択
    elif action == "select_menu":
        menu_id = params.get("menu_id")
        line_handler.on_menu_selected(user_id, menu_id)
    
    # 予約確定
    elif action == "confirm_booking":
        line_handler.confirm_booking(user_id)
    
    # 予約キャンセル
    elif action == "cancel_booking":
        booking_id = params.get("booking_id")
        line_handler.cancel_booking(user_id, booking_id)
    
    # 予約変更
    elif action == "modify_booking":
        booking_id = params.get("booking_id")
        line_handler.start_modify_booking(user_id, booking_id)
    
    else:
        logger.warning(f"Unknown action: {action}")

@handler.add(FollowEvent)
def handle_follow(event):
    """友達追加時"""
    user_id = event.source.user_id
    line_handler.on_user_follow(user_id)

# ===============================
# Web管理画面
# ===============================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """管理ダッシュボード"""
    today = datetime.now().date()
    bookings = db.get_bookings_by_date(today)
    customers = db.get_all_customers()
    menus = db.get_all_menus()
    
    # 顧客オプション生成
    customer_options = ""
    for customer in customers:
        user_id, name = customer[1], customer[2]
        display_name = name if name else user_id[:15] + "..."
        customer_options += f'<option value="{user_id}">{display_name}</option>'
    
    if not customer_options:
        customer_options = '<option value="">顧客がまだ登録されていません</option>'
    
    # メニューオプション生成
    menu_options = ""
    for menu in menus:
        menu_id, name, price = menu[0], menu[1], menu[2]
        menu_options += f'<option value="{menu_id}">【{name}】 ¥{price:,}</option>'
    
    # 本日の予約 HTML 生成
    bookings_html = ""
    for booking in bookings:
        customer = db.get_customer(booking[2])
        menu = db.get_menu(booking[4])
        status = "✅ 確定" if booking[6] == "confirmed" else "❌ キャンセル"
        
        bookings_html += f"""
        <tr>
            <td>{booking[3]}</td>
            <td>{customer[1] if customer else "不明"}</td>
            <td>{menu[1] if menu else "不明"}</td>
            <td>{status}</td>
        </tr>
        """
    
    # メニュー一覧 HTML 生成
    menus_html = ""
    for menu in menus:
        menu_id, name, price, duration = menu[0], menu[1], menu[2], menu[3]
        menus_html += f"""
        <div class="menu-item">
            <div class="menu-item-info">
                <strong>{name}</strong>
                <small>¥{price:,} • {duration}分</small>
            </div>
            <button class="danger" onclick="deleteMenu({menu_id})" style="padding: 8px 15px; font-size: 0.9em;">削除</button>
        </div>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>美容室予約管理</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f5f5f5; padding: 20px; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 30px; }}
            .header h1 {{ font-size: 2.5em; margin-bottom: 10px; }}
            .header p {{ font-size: 1.1em; opacity: 0.9; }}
            
            .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 30px; }}
            .stat-box {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .stat-box h3 {{ color: #667eea; margin-bottom: 10px; font-size: 0.9em; text-transform: uppercase; letter-spacing: 1px; }}
            .stat-box .value {{ font-size: 2.5em; font-weight: bold; color: #333; }}
            
            .section {{ background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 30px; }}
            .section h2 {{ font-size: 1.8em; margin-bottom: 20px; color: #333; border-bottom: 3px solid #667eea; padding-bottom: 10px; }}
            .section h3 {{ font-size: 1.3em; margin: 15px 0; color: #333; }}
            
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            table thead {{ background: #f9f9f9; }}
            table th, table td {{ padding: 15px; text-align: left; border-bottom: 1px solid #e0e0e0; }}
            table th {{ font-weight: 600; color: #333; }}
            table tr:hover {{ background: #f5f5f5; }}
            
            .button-group {{ display: flex; gap: 10px; flex-wrap: wrap; }}
            button, input[type="text"], input[type="number"], input[type="date"], input[type="time"], select {{ 
                padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; 
                font-size: 1em; transition: all 0.3s ease;
            }}
            button {{ background: #667eea; color: white; font-weight: 600; }}
            button:hover {{ background: #764ba2; transform: translateY(-2px); box-shadow: 0 4px 8px rgba(102, 126, 234, 0.3); }}
            button.danger {{ background: #e74c3c; }}
            button.danger:hover {{ background: #c0392b; }}
            
            input[type="text"], input[type="number"], input[type="date"], input[type="time"], select {{ 
                border: 1px solid #ddd; background: white; color: #333; width: 100%;
            }}
            input[type="text"]:focus, input[type="number"]:focus, input[type="date"]:focus, input[type="time"]:focus, select:focus {{ 
                outline: none; border-color: #667eea; box-shadow: 0 0 5px rgba(102, 126, 234, 0.3);
            }}
            
            .form-group {{ margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 8px; font-weight: 600; color: #333; }}
            small {{ color: #666; margin-top: 5px; display: block; }}
            textarea {{ border: 1px solid #ddd; padding: 10px; border-radius: 5px; font-family: Arial; resize: vertical; }}
            
            .message {{ padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
            .message.success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .message.error {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            
            .menu-item {{ background: #f9f9f9; padding: 15px; border-radius: 5px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }}
            .menu-item-info {{ flex: 1; }}
            .menu-item-info strong {{ display: block; font-size: 1.1em; margin-bottom: 5px; }}
            .menu-item-info small {{ color: #666; }}
            
            .form-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }}
            @media (max-width: 768px) {{
                .form-grid {{ grid-template-columns: 1fr; }}
                .header h1 {{ font-size: 1.8em; }}
                .stats {{ grid-template-columns: 1fr; }}
                table {{ font-size: 0.9em; }}
                table th, table td {{ padding: 10px; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>✨ 美容室予約管理システム</h1>
                <p>LINE連携リアルタイム管理ダッシュボード</p>
            </div>
            
            <div class="stats">
                <div class="stat-box">
                    <h3>本日の予約数</h3>
                    <div class="value">{len(bookings)}</div>
                </div>
                <div class="stat-box">
                    <h3>登録顧客数</h3>
                    <div class="value">{len(customers)}</div>
                </div>
                <div class="stat-box">
                    <h3>登録メニュー数</h3>
                    <div class="value">{len(menus)}</div>
                </div>
            </div>
            
            <!-- 予約を追加（顧客選択型） -->
            <div class="section">
                <h2>📅 予約を追加（LINE登録顧客）</h2>
                
                <div style="background: #f0f4ff; padding: 20px; border-radius: 5px; margin-bottom: 20px;">
                    <h3>LINE で接触済みの顧客から選択</h3>
                    <form id="addBookingWithCustomerForm" style="display: grid; gap: 15px;">
                        
                        <div class="form-group">
                            <label for="customerSelect">顧客を選択 *</label>
                            <select id="customerSelect" name="customer_id" required>
                                <option value="">-- 顧客を選択 --</option>
                                {customer_options}
                            </select>
                            <small>LINE で一度でもメッセージを送信した顧客のみ表示されます</small>
                        </div>
                        
                        <div class="form-grid">
                            <div class="form-group">
                                <label for="bookingDate">予約日 *</label>
                                <input type="date" id="bookingDate" name="booking_date" required>
                            </div>
                            <div class="form-group">
                                <label for="bookingTime">予約時間 *</label>
                                <input type="time" id="bookingTime" name="booking_time" required>
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label for="menuSelect">メニュー *</label>
                            <select id="menuSelect" name="menu_id" required>
                                <option value="">メニューを選択</option>
                                {menu_options}
                            </select>
                        </div>
                        
                        <div class="form-group">
                            <label for="bookingNotes">メモ（オプション）</label>
                            <textarea id="bookingNotes" name="notes" placeholder="例: 初回来店、敏感肌" style="height: 80px;"></textarea>
                        </div>
                        
                        <div style="display: flex; gap: 10px;">
                            <button type="submit">➕ 予約を追加</button>
                            <button type="button" onclick="document.getElementById('addBookingWithCustomerForm').reset();" style="background: #999;">リセット</button>
                        </div>
                    </form>
                    <div id="bookingMessage"></div>
                </div>
                
                <div style="background: #fff5e6; padding: 15px; border-left: 4px solid #ff9800; border-radius: 5px;">
                    <strong>📌 注意</strong><br>
                    <small>新しい顧客は、まず LINE で BOT に何かメッセージを送信してもらう必要があります。その後、上のドロップダウンに表示されます。</small>
                </div>
            </div>
            
            <!-- 本日の予約 -->
            <div class="section">
                <h2>📅 本日の予約</h2>
                <table>
                    <thead>
                        <tr>
                            <th>時間</th>
                            <th>顧客</th>
                            <th>メニュー</th>
                            <th>ステータス</th>
                        </tr>
                    </thead>
                    <tbody>
                        {bookings_html if bookings_html else '<tr><td colspan="4" style="text-align: center; color: #999;">本日の予約はありません</td></tr>'}
                    </tbody>
                </table>
            </div>
            
            <!-- メニュー管理 -->
            <div class="section">
                <h2>🎨 メニュー管理</h2>
                
                <div style="background: #f0f4ff; padding: 20px; border-radius: 5px; margin-bottom: 20px;">
                    <h3>新しいメニューを追加</h3>
                    <form id="addMenuForm" style="display: grid; gap: 15px;">
                        <div class="form-grid">
                            <div class="form-group">
                                <label for="menuName">メニュー名 *</label>
                                <input type="text" id="menuName" name="name" placeholder="例: カット" required>
                            </div>
                            <div class="form-group">
                                <label for="menuPrice">価格 (¥) *</label>
                                <input type="number" id="menuPrice" name="price" placeholder="例: 3000" required>
                            </div>
                        </div>
                        <div class="form-group">
                            <label for="menuDuration">施術時間 (分) *</label>
                            <input type="number" id="menuDuration" name="duration_minutes" placeholder="例: 60" required>
                        </div>
                        <button type="submit">➕ メニューを追加</button>
                    </form>
                </div>
                
                <h3>現在のメニュー一覧</h3>
                <div id="menuList">
                    {menus_html if menus_html else '<p style="color: #999;">メニューがまだ追加されていません</p>'}
                </div>
            </div>
        </div>
        
        <script>
            // 顧客選択型の予約追加
            document.getElementById('addBookingWithCustomerForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                
                const messageDiv = document.getElementById('bookingMessage');
                messageDiv.innerHTML = '<p style="color: #999;">予約を追加中...</p>';
                
                const customerId = document.getElementById('customerSelect').value;
                
                if (!customerId) {{
                    messageDiv.innerHTML = '<div class="message error">❌ 顧客を選択してください</div>';
                    return;
                }}
                
                const data = {{
                    customer_id: customerId,
                    booking_date: document.getElementById('bookingDate').value,
                    booking_time: document.getElementById('bookingTime').value,
                    menu_id: parseInt(document.getElementById('menuSelect').value),
                    notes: document.getElementById('bookingNotes').value
                }};
                
                try {{
                    const response = await fetch('/api/booking/add-with-customer', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    
                    const result = await response.json();
                    
                    if (response.ok) {{
                        messageDiv.innerHTML = '<div class="message success">✅ ' + result.message + '</div>';
                        document.getElementById('addBookingWithCustomerForm').reset();
                        setTimeout(() => {{
               
