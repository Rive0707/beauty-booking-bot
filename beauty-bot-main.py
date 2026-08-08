"""
LINE美容室予約BOT メインアプリケーション
FastAPI + LINE Messaging API + SQLite + APScheduler
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent, FollowEvent
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
    hours=1,  # 1時間ごとにチェック
    id='reminder_job'
)
scheduler.start()

# プロセス終了時にスケジューラーを停止
atexit.register(lambda: scheduler.shutdown())

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
    # 形式: action=xxx&key=value
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
# Web管理画面 エンドポイント
# ===============================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """管理ダッシュボード"""
    # 本日の予約
    today = datetime.now().date()
    bookings = db.get_bookings_by_date(today)
    
    bookings_html = ""
    for booking in bookings:
        customer = db.get_customer(booking[2])  # user_id
        menu = db.get_menu(booking[4])  # menu_id
        status = "✅ 確定" if booking[6] == "confirmed" else "❌ キャンセル"
        
        bookings_html += f"""
        <tr>
            <td>{booking[3]}</td>
            <td>{customer[1] if customer else "不明"}</td>
            <td>{menu[1] if menu else "不明"}</td>
            <td>{status}</td>
        </tr>
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
            
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
            table thead {{ background: #f9f9f9; }}
            table th, table td {{ padding: 15px; text-align: left; border-bottom: 1px solid #e0e0e0; }}
            table th {{ font-weight: 600; color: #333; }}
            table tr:hover {{ background: #f5f5f5; }}
            
            .button-group {{ display: flex; gap: 10px; flex-wrap: wrap; }}
            button, input[type="text"], input[type="number"], select {{ 
                padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; 
                font-size: 1em; transition: all 0.3s ease;
            }}
            button {{ background: #667eea; color: white; font-weight: 600; }}
            button:hover {{ background: #764ba2; transform: translateY(-2px); box-shadow: 0 4px 8px rgba(102, 126, 234, 0.3); }}
            button.danger {{ background: #e74c3c; }}
            button.danger:hover {{ background: #c0392b; }}
            
            input[type="text"], input[type="number"], select {{ 
                border: 1px solid #ddd; background: white; color: #333;
            }}
            input[type="text"]:focus, input[type="number"]:focus, select:focus {{ 
                outline: none; border-color: #667eea; box-shadow: 0 0 5px rgba(102, 126, 234, 0.3);
            }}
            
            .form-group {{ margin-bottom: 20px; }}
            label {{ display: block; margin-bottom: 8px; font-weight: 600; color: #333; }}
            
            .message {{ padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
            .message.success {{ background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }}
            .message.error {{ background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }}
            
            .menu-item {{ background: #f9f9f9; padding: 15px; border-radius: 5px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }}
            .menu-item-info {{ flex: 1; }}
            .menu-item-info strong {{ display: block; font-size: 1.1em; margin-bottom: 5px; }}
            .menu-item-info small {{ color: #666; }}
            
            @media (max-width: 768px) {{
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
                    <div class="value">{len(db.get_all_customers())}</div>
                </div>
                <div class="stat-box">
                    <h3>登録メニュー数</h3>
                    <div class="value">{len(db.get_all_menus())}</div>
                </div>
            </div>
            
            <!-- 本日の予約 -->
            <div class="section">
                <h2>📅 本日の予約</h2>
                {f'''
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
                ''' if bookings_html else '<p style="color: #999;">本日の予約はありません</p>'}
            </div>
            
            <!-- メニュー管理 -->
            <div class="section">
                <h2>🎨 メニュー管理</h2>
                
                <div style="background: #f0f4ff; padding: 20px; border-radius: 5px; margin-bottom: 20px;">
                    <h3 style="margin-bottom: 15px;">新しいメニューを追加</h3>
                    <form id="addMenuForm" style="display: grid; gap: 15px;">
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                            <div class="form-group">
                                <label for="menuName">メニュー名 *</label>
                                <input type="text" id="menuName" name="name" placeholder="例: カット" required style="width: 100%;">
                            </div>
                            <div class="form-group">
                                <label for="menuPrice">価格 (¥) *</label>
                                <input type="number" id="menuPrice" name="price" placeholder="例: 3000" required style="width: 100%;">
                            </div>
                        </div>
                        <div class="form-group">
                            <label for="menuDuration">施術時間 (分) *</label>
                            <input type="number" id="menuDuration" name="duration_minutes" placeholder="例: 60" required style="width: 100%;">
                        </div>
                        <button type="submit">➕ メニューを追加</button>
                    </form>
                </div>
                
                <h3 style="margin-bottom: 15px;">現在のメニュー一覧</h3>
                <div id="menuList">
                    {self._render_menus()}
                </div>
            </div>
        </div>
        
        <script>
            // メニュー追加フォーム
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
        </script>
    </body>
    </html>
    """
    return html

def _render_menus():
    """メニュー一覧をHTML生成"""
    menus = db.get_all_menus()
    if not menus:
        return '<p style="color: #999;">メニューがまだ追加されていません</p>'
    
    html = ""
    for menu in menus:
        menu_id, name, price, duration_minutes = menu[0], menu[1], menu[2], menu[3]
        html += f"""
        <div class="menu-item">
            <div class="menu-item-info">
                <strong>{name}</strong>
                <small>¥{price:,} • {duration_minutes}分</small>
            </div>
            <button class="danger" onclick="deleteMenu({menu_id})" style="padding: 8px 15px; font-size: 0.9em;">削除</button>
        </div>
        """
    return html

# API エンドポイント
@app.post("/api/menu/add")
async def add_menu(data: dict):
    """メニュー追加"""
    try:
        db.add_menu(data['name'], data['price'], data['duration_minutes'])
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

Web管理画面: https://[your-replit-url]/
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
