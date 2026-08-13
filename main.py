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
from linebot.models import MessageEvent, TextMessage, PostbackEvent, FollowEvent, TextSendMessage
from pydantic import BaseModel
from typing import Optional
import os
import uuid
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
line_handler = LineHandler(line_bot_api, db, owner_user_id=OWNER_USER_ID)

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
    notes: Optional[str] = None

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
    furigana: Optional[str] = None
    gender: Optional[str] = None
    birthdate: Optional[str] = None
    phone: Optional[str] = None

class ManualBookingRequest(BaseModel):
    """ダッシュボードからの手動予約登録（紙の予約帳からの移行用。LINE未連携でも登録可能）"""
    name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_id: int
    note: Optional[str] = None

class BookingUpdateRequest(BaseModel):
    """ダッシュボードからの予約変更"""
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None
    menu_id: Optional[int] = None

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

            <!-- 予約を手動登録（紙の予約帳からの移行用） -->
            <div class="section">
                <h2>✍️ 予約を手動登録</h2>
                <p style="color: #999; font-size: 0.9em; margin-bottom: 15px;">
                    紙の予約帳のお客様など、LINE未連携でも登録できます。<br>
                    ※LINE未連携のお客様にはリマインダー・変更通知は届きません。
                </p>
                <div style="background: #f0f4ff; padding: 20px; border-radius: 5px;">
                    <form id="manualBookingForm" style="display: grid; gap: 15px;">
                        <div class="form-grid">
                            <div class="form-group">
                                <label for="manualName">お客様名 *</label>
                                <input type="text" id="manualName" required>
                            </div>
                            <div class="form-group">
                                <label for="manualPhone">電話番号</label>
                                <input type="text" id="manualPhone">
                            </div>
                            <div class="form-group">
                                <label for="manualDate">日付 *</label>
                                <input type="date" id="manualDate" required>
                            </div>
                            <div class="form-group">
                                <label for="manualTime">時間 *</label>
                                <input type="time" id="manualTime" required>
                            </div>
                            <div class="form-group">
                                <label for="manualMenu">メニュー *</label>
                                <select id="manualMenu" required>
                                    <option value="">選択してください</option>
                                    {menu_options}
                                </select>
                            </div>
                            <div class="form-group">
                                <label for="manualNote">メモ</label>
                                <input type="text" id="manualNote">
                            </div>
                        </div>
                        <button type="submit">この内容で登録する</button>
                    </form>
                    <div id="manualBookingMessage"></div>
                </div>
            </div>

            <!-- 今後の予約一覧（変更・キャンセル） -->
            <div class="section">
                <h2>📋 今後の予約一覧</h2>
                <p style="color: #999; font-size: 0.9em; margin-bottom: 15px;">本日以降の確定予約です。変更・キャンセルができます。</p>
                <table>
                    <thead>
                        <tr>
                            <th>日付</th>
                            <th>時間</th>
                            <th>顧客</th>
                            <th>電話番号</th>
                            <th>メニュー</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody id="upcomingBookingsBody">
                        <tr><td colspan="6" style="text-align: center; color: #999;">読み込み中…</td></tr>
                    </tbody>
                </table>
            </div>

            <!-- 変更・キャンセル履歴 -->
            <div class="section">
                <h2>🕓 変更・キャンセル履歴</h2>
                <table>
                    <thead>
                        <tr>
                            <th>日時</th>
                            <th>種別</th>
                            <th>顧客</th>
                            <th>変更前</th>
                            <th>変更後</th>
                            <th>備考</th>
                        </tr>
                    </thead>
                    <tbody id="historyBody">
                        <tr><td colspan="6" style="text-align: center; color: #999;">読み込み中…</td></tr>
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
                                <label for="menuPrice">価格 (B) *</label>
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
                            location.reload();
                        }}, 2000);
                    }} else {{
                        messageDiv.innerHTML = '<div class="message error">❌ エラー: ' + result.detail + '</div>';
                    }}
                }} catch (error) {{
                    messageDiv.innerHTML = '<div class="message error">❌ エラー: ' + error + '</div>';
                }}
            }});
            
            // メニュー追加
            document.getElementById('addMenuForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                const data = {{
                    name: document.getElementById('menuName').value,
                    price: parseInt(document.getElementById('menuPrice').value),
                    duration_minutes: parseInt(document.getElementById('menuDuration').value)
                }};
                
                try {{
                    const response = await fetch('/api/menu/add', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    
                    if (response.ok) {{
                        alert('メニューを追加しました！');
                        location.reload();
                    }} else {{
                        alert('エラーが発生しました');
                    }}
                }} catch (error) {{
                    alert('エラー: ' + error);
                }}
            }});
            
            // メニュー削除
            async function deleteMenu(menuId) {{
                if (confirm('このメニューを削除しますか？')) {{
                    try {{
                        const response = await fetch(`/api/menu/delete/${{menuId}}`, {{method: 'DELETE'}});
                        if (response.ok) {{
                            alert('削除しました！');
                            location.reload();
                        }}
                    }} catch (error) {{
                        alert('エラー: ' + error);
                    }}
                }}
            }}

            // 手動予約登録
            document.getElementById('manualBookingForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                const messageDiv = document.getElementById('manualBookingMessage');
                messageDiv.innerHTML = '<p style="color: #999;">登録中...</p>';

                const data = {{
                    name: document.getElementById('manualName').value,
                    phone: document.getElementById('manualPhone').value || null,
                    booking_date: document.getElementById('manualDate').value,
                    booking_time: document.getElementById('manualTime').value,
                    menu_id: parseInt(document.getElementById('manualMenu').value),
                    note: document.getElementById('manualNote').value || null
                }};

                try {{
                    const response = await fetch('/api/booking/manual', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify(data)
                    }});
                    if (response.ok) {{
                        messageDiv.innerHTML = '<div class="message success">✅ 登録しました</div>';
                        document.getElementById('manualBookingForm').reset();
                        loadUpcomingBookings();
                        loadHistory();
                    }} else {{
                        const result = await response.json();
                        const errText = Array.isArray(result.detail)
                            ? result.detail.map(d => d.msg || JSON.stringify(d)).join(' / ')
                            : (result.detail || '不明なエラー');
                        messageDiv.innerHTML = '<div class="message error">❌ エラー: ' + errText + '</div>';
                    }}
                }} catch (error) {{
                    messageDiv.innerHTML = '<div class="message error">❌ エラー: ' + error + '</div>';
                }}
            }});

            // 今後の予約一覧を読み込み
            async function loadUpcomingBookings() {{
                const tbody = document.getElementById('upcomingBookingsBody');
                try {{
                    const res = await fetch('/api/bookings/upcoming/all');
                    const data = await res.json();
                    if (!data.bookings.length) {{
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #999;">今後の予約はありません</td></tr>';
                        return;
                    }}
                    tbody.innerHTML = data.bookings.map(b => `
                        <tr>
                            <td>${{b.booking_date}}</td>
                            <td>${{b.booking_time}}</td>
                            <td>${{b.customer_name || '不明'}}</td>
                            <td>${{b.customer_phone || '-'}}</td>
                            <td>${{b.menu_name || '不明'}}</td>
                            <td>
                                <button onclick="editBooking(${{b.id}}, '${{b.booking_date}}', '${{b.booking_time}}')" style="padding: 6px 10px; font-size: 0.85em;">変更</button>
                                <button class="danger" onclick="cancelBookingFromDashboard(${{b.id}})" style="padding: 6px 10px; font-size: 0.85em;">キャンセル</button>
                            </td>
                        </tr>
                    `).join('');
                }} catch (error) {{
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #c00;">読み込みに失敗しました</td></tr>';
                }}
            }}

            // 予約変更
            async function editBooking(bookingId, currentDate, currentTime) {{
                const newDate = prompt('新しい日付 (YYYY-MM-DD)', currentDate);
                if (newDate === null) return;
                const newTime = prompt('新しい時間 (HH:MM)', currentTime);
                if (newTime === null) return;

                try {{
                    const response = await fetch(`/api/booking/${{bookingId}}`, {{
                        method: 'PUT',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{ booking_date: newDate, booking_time: newTime }})
                    }});
                    if (response.ok) {{
                        alert('変更しました');
                        loadUpcomingBookings();
                        loadHistory();
                    }} else {{
                        alert('エラーが発生しました');
                    }}
                }} catch (error) {{
                    alert('エラー: ' + error);
                }}
            }}

            // 予約キャンセル
            async function cancelBookingFromDashboard(bookingId) {{
                if (!confirm('この予約をキャンセルしますか？')) return;
                try {{
                    const response = await fetch(`/api/booking/${{bookingId}}/cancel`, {{ method: 'POST' }});
                    if (response.ok) {{
                        alert('キャンセルしました');
                        loadUpcomingBookings();
                        loadHistory();
                    }} else {{
                        alert('エラーが発生しました');
                    }}
                }} catch (error) {{
                    alert('エラー: ' + error);
                }}
            }}

            // 変更・キャンセル履歴を読み込み
            async function loadHistory() {{
                const tbody = document.getElementById('historyBody');
                try {{
                    const res = await fetch('/api/bookings/history');
                    const data = await res.json();
                    if (!data.history.length) {{
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #999;">履歴はまだありません</td></tr>';
                        return;
                    }}
                    const actionLabels = {{ created: '🆕 新規', modified: '📝 変更', cancelled: '❌ キャンセル' }};
                    tbody.innerHTML = data.history.map(h => `
                        <tr>
                            <td>${{h.created_at}}</td>
                            <td>${{actionLabels[h.action] || h.action}}</td>
                            <td>${{h.customer_name || '不明'}}</td>
                            <td>${{h.before_date ? h.before_date + ' ' + (h.before_time || '') : '-'}}</td>
                            <td>${{h.after_date ? h.after_date + ' ' + (h.after_time || '') : '-'}}</td>
                            <td>${{h.note || '-'}}</td>
                        </tr>
                    `).join('');
                }} catch (error) {{
                    tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #c00;">読み込みに失敗しました</td></tr>';
                }}
            }}

            loadUpcomingBookings();
            loadHistory();
        </script>
    </body>
    </html>
    """
    return html

# ===============================
# API エンドポイント
# ===============================

@app.post("/api/booking/add-with-customer")
async def add_booking_with_customer(data: BookingAddWithCustomerRequest):
    """
    Web管理画面から顧客を選択して予約を追加
    その顧客の LINE アカウントに自動紐付け
    1週間前に自動リマインダー送信
    """
    try:
        # 顧客が存在するか確認
        customer = db.get_customer(data.customer_id)
        if not customer:
            raise Exception("顧客が見つかりません")
        
        customer_name = customer[2]
        
        # 予約を保存
        booking_id = db.add_booking(
            user_id=data.customer_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id,
            notes=data.notes or ""
        )
        
        if not booking_id:
            raise Exception("予約の保存に失敗しました")
        
        # 顧客に確認メッセージを送信
        menu = db.get_menu(data.menu_id)
        menu_name = menu[1] if menu else "不明"
        
        confirmation_message = f"""
📅 予約が追加されました

予約日時: {data.booking_date} {data.booking_time}
メニュー: {menu_name}
予約ID: {booking_id}

7日前にリマインダーをお送りいたします。
ご不明な点はお気軽にお問い合わせください。
"""
        
        try:
            from linebot.models import TextSendMessage
            line_bot_api.push_message(data.customer_id, TextSendMessage(text=confirmation_message))
        except Exception as e:
            logger.warning(f"Failed to send confirmation message: {e}")
        
        logger.info(f"Booking added with customer selection: {booking_id} ({customer_name})")
        
        return JSONResponse({
            "status": "ok",
            "booking_id": booking_id,
            "message": f"予約を追加しました。顧客に LINE で通知しました。"
        })
    
    except Exception as e:
        logger.error(f"Error adding booking with customer: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bookings/upcoming/all")
async def get_all_upcoming_bookings():
    """今後すべての確定予約を取得（ダッシュボードの一覧表示用）"""
    try:
        bookings = db.get_all_upcoming_bookings()
        result = [dict(b) for b in bookings]
        return JSONResponse({"bookings": result})
    except Exception as e:
        logger.error(f"Error getting all upcoming bookings: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bookings/history")
async def get_booking_history(limit: int = 50):
    """予約の変更・キャンセル履歴を取得"""
    try:
        history = db.get_booking_history(limit=limit)
        result = [dict(h) for h in history]
        return JSONResponse({"history": result})
    except Exception as e:
        logger.error(f"Error getting booking history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/booking/manual")
async def create_manual_booking(data: ManualBookingRequest):
    """
    ダッシュボードからの手動予約登録
    紙の予約帳のお客様など、LINE未連携でも登録できる（仮のuser_idを発行する）
    """
    try:
        manual_user_id = f"manual-{uuid.uuid4().hex[:16]}"
        db.save_customer_profile(user_id=manual_user_id, name=data.name, phone=data.phone)

        booking_id = db.add_booking(
            user_id=manual_user_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id,
            notes=data.note
        )
        if not booking_id:
            raise Exception("予約の保存に失敗しました")

        db.add_booking_history(
            booking_id=booking_id, action="created", user_id=manual_user_id,
            after_date=data.booking_date, after_time=data.booking_time, note="手動登録"
        )

        return JSONResponse({"status": "ok", "booking_id": booking_id})
    except Exception as e:
        logger.error(f"Error creating manual booking: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/booking/{booking_id}")
async def update_booking_from_dashboard(booking_id: int, data: BookingUpdateRequest):
    """ダッシュボードからの予約変更"""
    try:
        booking = db.get_booking(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="予約が見つかりません")

        before_date = booking["booking_date"]
        before_time = booking["booking_time"]

        db.update_booking(
            booking_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id
        )

        db.add_booking_history(
            booking_id=booking_id, action="modified", user_id=booking["user_id"],
            before_date=before_date, before_time=before_time,
            after_date=data.booking_date or before_date,
            after_time=data.booking_time or before_time,
            note="ダッシュボードから変更"
        )

        # LINE連携済みのお客様には通知する（手動登録で未連携の場合はスキップされる）
        try:
            line_bot_api.push_message(
                booking["user_id"],
                TextSendMessage(text=f"📝 ご予約内容が変更されました\n\n変更後の日時: {data.booking_date or before_date} {data.booking_time or before_time}\n予約ID: {booking_id}")
            )
        except Exception:
            pass  # LINE未連携（手動登録）のお客様は送信できないため無視

        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating booking from dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/booking/{booking_id}/cancel")
async def cancel_booking_from_dashboard(booking_id: int):
    """ダッシュボードからの予約キャンセル"""
    try:
        booking = db.get_booking(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="予約が見つかりません")

        db.cancel_booking(booking_id)

        db.add_booking_history(
            booking_id=booking_id, action="cancelled", user_id=booking["user_id"],
            before_date=booking["booking_date"], before_time=booking["booking_time"],
            note="ダッシュボードからキャンセル"
        )

        try:
            line_bot_api.push_message(
                booking["user_id"],
                TextSendMessage(text=f"❌ ご予約がキャンセルされました\n\n予約日時: {booking['booking_date']} {booking['booking_time']}\n予約ID: {booking_id}\n\nご不明な点があればお問い合わせください。")
            )
        except Exception:
            pass  # LINE未連携（手動登録）のお客様は送信できないため無視

        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling booking from dashboard: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/customer/{user_id}")
async def get_customer_profile(user_id: str):
    """お客様の登録済み情報を取得（2回目以降の予約フォーム自動入力用）"""
    try:
        customer = db.get_customer(user_id)
        if not customer:
            return JSONResponse({"customer": None})
        return JSONResponse({
            "customer": {
                "name": customer["name"],
                "furigana": customer["furigana"] if "furigana" in customer.keys() else None,
                "gender": customer["gender"] if "gender" in customer.keys() else None,
                "birthdate": customer["birthdate"] if "birthdate" in customer.keys() else None,
                "phone": customer["phone"],
            }
        })
    except Exception as e:
        logger.error(f"Error getting customer profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/menus")
async def get_menus():
    """メニュー一覧取得（LIFF画面用）"""
    try:
        menus = db.get_all_menus()
        result = [
            {"id": m["id"], "name": m["name"], "price": m["price"], "duration_minutes": m["duration_minutes"]}
            for m in menus
        ]
        return JSONResponse({"menus": result})
    except Exception as e:
        logger.error(f"Error getting menus: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/booking/create")
async def create_booking_from_liff(data: BookingCreateFromLiffRequest):
    """LIFFの予約フォームから送信された内容で予約を確定する"""
    try:
        # お客様情報を保存（新規 or 更新）
        db.save_customer_profile(
            user_id=data.user_id,
            name=data.name,
            furigana=data.furigana,
            gender=data.gender,
            birthdate=data.birthdate,
            phone=data.phone
        )

        # 予約を保存
        booking_id = db.add_booking(
            user_id=data.user_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id
        )

        if not booking_id:
            raise Exception("予約の保存に失敗しました")

        db.add_booking_history(
            booking_id=booking_id, action="created", user_id=data.user_id,
            after_date=data.booking_date, after_time=data.booking_time, note="Web予約"
        )

        menu = db.get_menu(data.menu_id)
        menu_name = menu["name"] if menu else "不明"

        confirmation_message = f"""ご予約ありがとうございます！

📅 予約日時: {data.booking_date} {data.booking_time}
💇 メニュー: {menu_name}
予約番号: {booking_id}

当日のご来店をお待ちしております。
"""
        try:
            from linebot.models import TextSendMessage
            line_bot_api.push_message(data.user_id, TextSendMessage(text=confirmation_message))
        except Exception as e:
            logger.warning(f"Failed to send confirmation message: {e}")

        # オーナーへ通知
        if OWNER_USER_ID:
            try:
                owner_message = (
                    f"🆕 新規予約が入りました（Web予約）\n\n"
                    f"お客様: {data.name}\n"
                    f"予約日時: {data.booking_date} {data.booking_time}\n"
                    f"メニュー: {menu_name}\n"
                    f"電話番号: {data.phone or '未入力'}\n"
                    f"予約ID: {booking_id}"
                )
                line_bot_api.push_message(OWNER_USER_ID, TextSendMessage(text=owner_message))
            except Exception as e:
                logger.warning(f"Failed to notify owner: {e}")

        logger.info(f"Booking created via LIFF: {booking_id} ({data.user_id})")

        return JSONResponse({
            "status": "ok",
            "booking_id": booking_id
        })
    except Exception as e:
        logger.error(f"Error creating booking from LIFF: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/menu/add")
async def add_menu(data: MenuAddRequest):
    """メニュー追加"""
    try:
        db.add_menu(data.name, data.price, data.duration_minutes)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error(f"Error adding menu: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/menu/delete/{menu_id}")
async def delete_menu(menu_id: int):
    """メニュー削除"""
    try:
        db.delete_menu(menu_id)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error(f"Error deleting menu: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bookings/{date_str}")
async def get_bookings_by_date(date_str: str):
    """指定日の予約取得"""
    try:
        date = datetime.strptime(date_str, "%Y-%m-%d").date()
        bookings = db.get_bookings_by_date(date)
        return JSONResponse({"bookings": bookings})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def generate_time_slots():
    """営業時間・予約間隔から時間枠のリストを生成"""
    slots = []
    start = datetime.strptime(BUSINESS_HOURS_START, "%H:%M")
    end = datetime.strptime(BUSINESS_HOURS_END, "%H:%M")
    current = start
    while current < end:
        slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=SLOT_INTERVAL_MINUTES)
    return slots

WEEKDAY_LABELS_JA = ["月", "火", "水", "木", "金", "土", "日"]

@app.get("/api/availability")
async def get_availability(start_date: str, days: int = 7):
    """
    指定日から指定日数分の空き状況を返す
    定休日・営業時間・既存予約を踏まえて ○/✕/- を判定する
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = start + timedelta(days=days - 1)

        booked_times = db.get_booked_times_in_range(
            start.isoformat(), end.isoformat()
        )
        time_slots = generate_time_slots()

        date_list = []
        availability = {}

        for i in range(days):
            current_date = start + timedelta(days=i)
            date_str = current_date.isoformat()
            weekday_index = current_date.weekday()

            date_list.append({
                "date": date_str,
                "day": current_date.day,
                "weekday": WEEKDAY_LABELS_JA[weekday_index],
                "is_closed": weekday_index in CLOSED_WEEKDAYS
            })

            if weekday_index in CLOSED_WEEKDAYS:
                availability[date_str] = {slot: "closed" for slot in time_slots}
            else:
                booked_for_date = booked_times.get(date_str, [])
                availability[date_str] = {
                    slot: ("booked" if slot in booked_for_date else "available")
                    for slot in time_slots
                }

        return JSONResponse({
            "time_slots": time_slots,
            "dates": date_list,
            "availability": availability
        })
    except Exception as e:
        logger.error(f"Error getting availability: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ===============================
# オーナーコマンド処理
# ===============================

def handle_owner_command(user_id: str, text: str):
    """オーナー向けコマンド処理"""
    
    # 本日の予約確認
    if text in ["/today", "今日", "本日"]:
        today = datetime.now().date()
        bookings = db.get_bookings_by_date(today)
        
        if not bookings:
            line_handler.send_text(user_id, "本日の予約はありません")
            return
        
        message = "📅 本日の予約\n\n"
        for booking in bookings:
            customer = db.get_customer(booking[2])
            menu = db.get_menu(booking[4])
            status = "✅" if booking[6] == "confirmed" else "❌"
            
            message += f"{status} {booking[3]} - {customer[1]} ({menu[1]})\n"
        
        line_handler.send_text(user_id, message)
    
    # 明日の予約確認
    elif text in ["/tomorrow", "明日"]:
        tomorrow = datetime.now().date() + timedelta(days=1)
        bookings = db.get_bookings_by_date(tomorrow)
        
        if not bookings:
            line_handler.send_text(user_id, "明日の予約はありません")
            return
        
        message = "📅 明日の予約\n\n"
        for booking in bookings:
            customer = db.get_customer(booking[2])
            menu = db.get_menu(booking[4])
            message += f"⏰ {booking[3]} - {customer[1]} ({menu[1]})\n"
        
        line_handler.send_text(user_id, message)
    
    # ヘルプ
    elif text in ["/help"]:
        help_text = """
📋 オーナーコマンド一覧

/today (または「今日」「本日」)
→ 本日の予約を表示

/tomorrow (または「明日」)
→ 明日の予約を表示

Web管理画面にアクセス:
ブラウザで http://your-url/ を開く
"""
        line_handler.send_text(user_id, help_text)
    
    else:
        line_handler.send_text(user_id, "わかりません。/help でコマンド一覧を見てください")

# ===============================
# ヘルスチェック
# ===============================

@app.get("/health")
async def health():
    """ヘルスチェック"""
    return JSONResponse({"status": "ok"})

# ===============================
# サーバー起動
# ===============================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
