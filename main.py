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
LIFF_ID = os.getenv("LIFF_ID")  # 予約変更用LIFF（未設定でも動作するが、変更ボタンが表示されなくなる）

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
line_handler = LineHandler(line_bot_api, db, owner_user_id=OWNER_USER_ID, liff_id=LIFF_ID)

# リマインダー初期化
reminder_scheduler = ReminderScheduler(line_bot_api, db, liff_id=LIFF_ID)

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

class RescheduleRequest(BaseModel):
    """LIFF経由での予約変更（お客様自身が変更する場合）"""
    user_id: str
    booking_id: int
    booking_date: str
    booking_time: str

class ManualBookingRequest(BaseModel):
    """ダッシュボードからの手動予約登録（紙の予約帳からの移行用。LINE未連携でも登録可能）"""
    name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_id: int
    note: Optional[str] = None
    existing_user_id: Optional[str] = None  # 既存のLINE連携済みお客様を選んだ場合に指定

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
    elif text in ["予約確認", "マイページ", "履歴"]:
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
    
    # 予約変更（今はLIFFカレンダーで直接行うため、このpostbackは古いリンクの互換用）
    elif action == "modify_booking":
        booking_id = params.get("booking_id")
        line_handler.start_modify_booking(user_id, booking_id)

    # 「予約変更なし」確認 → 3日前リマインドを省略
    elif action == "confirm_no_change":
        booking_id = params.get("booking_id")
        line_handler.db.confirm_no_change(int(booking_id))
        line_handler.send_text(user_id, "承知しました。ご来店をお待ちしております！")

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
    return HTMLResponse(content=dashboard_html)

dashboard_html = """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>美容室予約管理</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #f5f5f7; color: #1d1d1f; line-height: 1.5; padding: 16px;
  }
  .wrap { max-width: 960px; margin: 0 auto; }
  .header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 20px; flex-wrap: wrap; gap: 12px;
  }
  .title { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 14px; border-radius: 10px; border: 1px solid #d1d1d6;
    background: #fff; color: #1d1d1f; font-size: 14px; font-weight: 500;
    cursor: pointer; text-decoration: none;
  }
  .btn-primary { background: #1d1d1f; color: #fff; border-color: #1d1d1f; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid #d1d1d6; margin-bottom: 20px; }
  .tab {
    padding: 10px 16px; font-size: 14px; font-weight: 500;
    color: #8e8e93; cursor: pointer; border-bottom: 2px solid transparent;
    margin-bottom: -1px; background: none; border: none;
  }
  .tab.active { color: #1d1d1f; border-bottom-color: #1d1d1f; }
  .panel { display: none; }
  .panel.active { display: block; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat-card { background: #fff; border-radius: 12px; padding: 16px; border: 1px solid #e5e5ea; }
  .stat-label { font-size: 12px; color: #8e8e93; margin-bottom: 4px; }
  .stat-value { font-size: 28px; font-weight: 600; }

  .date-nav { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .date-nav input[type="date"] {
    padding: 6px 10px; border-radius: 8px; border: 1px solid #d1d1d6;
    font-size: 14px; font-family: inherit;
  }
  .date-label { font-size: 15px; font-weight: 500; }

  .timeline {
    display: grid; grid-template-columns: 56px 1fr; gap: 0;
    border: 1px solid #d1d1d6; border-radius: 12px; overflow: hidden; background: #fff;
  }
  .time-slot { display: contents; }
  .time-label {
    padding: 12px 8px; font-size: 12px; color: #8e8e93; text-align: right;
    border-right: 1px solid #e5e5ea; border-bottom: 1px solid #e5e5ea;
    background: #fafafa; font-variant-numeric: tabular-nums;
  }
  .time-content {
    padding: 8px; border-bottom: 1px solid #e5e5ea; min-height: 48px; position: relative;
  }
  .time-slot:last-child .time-label, .time-slot:last-child .time-content { border-bottom: none; }
  .booking-card {
    background: #e8f4fd; border-left: 3px solid #007aff; border-radius: 8px;
    padding: 8px 10px; margin-bottom: 4px; cursor: pointer; position: relative;
  }
  .booking-card.tentative { border-left-color: #ff9500; background: #fff4e5; }
  .booking-card.cancelled { border-left-color: #ff3b30; background: #ffe5e5; opacity: 0.7; }
  .booking-name { font-weight: 500; font-size: 13px; }
  .booking-meta { font-size: 12px; color: #8e8e93; margin-top: 2px; display: flex; gap: 8px; flex-wrap: wrap; }
  .booking-tag {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 11px; padding: 1px 6px; border-radius: 4px;
    background: #fff; color: #8e8e93;
  }
  .booking-actions {
    position: absolute; top: 6px; right: 6px; display: flex; gap: 4px;
    opacity: 0; transition: opacity 0.15s;
  }
  .booking-card:hover .booking-actions { opacity: 1; }
  .icon-btn {
    width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center;
    border-radius: 6px; border: none; background: #fff; color: #8e8e93; cursor: pointer; padding: 0;
  }
  .icon-btn:hover { background: #f2f2f7; color: #1d1d1f; }
  .icon-btn.danger:hover { color: #ff3b30; }

  .toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .search-box { position: relative; flex: 1; min-width: 200px; }
  .search-box input {
    width: 100%; padding: 8px 10px 8px 32px; border-radius: 10px;
    border: 1px solid #d1d1d6; font-size: 14px; font-family: inherit; background: #fff;
  }
  .filter-select {
    padding: 8px 10px; border-radius: 10px; border: 1px solid #d1d1d6;
    font-size: 14px; font-family: inherit; background: #fff; min-width: 120px;
  }
  .data-table { width: 100%; border-collapse: collapse; font-size: 14px; background: #fff; border-radius: 12px; overflow: hidden; }
  .data-table th {
    text-align: left; padding: 10px 12px; font-weight: 500; color: #8e8e93;
    border-bottom: 1px solid #e5e5ea; font-size: 12px; white-space: nowrap; background: #fafafa;
  }
  .data-table td { padding: 10px 12px; border-bottom: 1px solid #e5e5ea; vertical-align: middle; }
  .data-table tbody tr { transition: background 0.15s; }
  .data-table tbody tr:hover { background: #f5f5f7; }
  .status-badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 500;
  }
  .status-confirmed { background: #e8f5e9; color: #2e7d32; }
  .status-tentative { background: #fff3e0; color: #ef6c00; }
  .status-cancelled { background: #ffebee; color: #c62828; }
  .row-actions { display: flex; gap: 4px; opacity: 0; transition: opacity 0.15s; }
  .data-table tbody tr:hover .row-actions { opacity: 1; }

  .history-item {
    display: flex; gap: 12px; padding: 12px; border-radius: 10px;
    border: 1px solid #e5e5ea; margin-bottom: 8px; background: #fff;
    transition: background 0.15s;
  }
  .history-item:hover { background: #f5f5f7; }
  .history-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }
  .history-dot.change { background: #007aff; }
  .history-dot.cancel { background: #ff3b30; }
  .history-content { flex: 1; }
  .history-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
  .history-type { font-size: 12px; font-weight: 500; padding: 2px 8px; border-radius: 6px; }
  .history-type.change { background: #e8f4fd; color: #007aff; }
  .history-type.cancel { background: #ffe5e5; color: #ff3b30; }
  .history-time { font-size: 12px; color: #8e8e93; }
  .history-customer { font-weight: 500; font-size: 14px; margin-bottom: 4px; }
  .history-detail { font-size: 13px; color: #8e8e93; }
  .history-detail .from { color: #ff3b30; }
  .history-detail .to { color: #2e7d32; }

  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.35);
    display: none; align-items: center; justify-content: center; z-index: 100; padding: 16px;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: #fff; border-radius: 16px; width: 100%; max-width: 480px;
    max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.15);
  }
  .modal-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; border-bottom: 1px solid #e5e5ea;
  }
  .modal-title { font-size: 17px; font-weight: 600; }
  .modal-body { padding: 20px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: #8e8e93; }
  .form-group label .req { color: #ff3b30; margin-left: 2px; }
  .form-group input, .form-group select, .form-group textarea {
    width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #d1d1d6;
    font-size: 14px; font-family: inherit; background: #fff; color: #1d1d1f;
  }
  .form-group input:focus, .form-group select:focus, .form-group textarea:focus {
    outline: none; border-color: #007aff;
  }
  .form-group textarea { resize: vertical; min-height: 60px; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .modal-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px 16px; }

  .empty { text-align: center; padding: 40px 20px; color: #c7c7cc; font-size: 14px; }
  .toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 200; display: flex; flex-direction: column; gap: 8px; }
  .toast {
    background: #1d1d1f; color: #fff; padding: 10px 16px; border-radius: 10px;
    font-size: 13px; font-weight: 500; box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    animation: slideIn 0.25s ease-out; display: flex; align-items: center; gap: 8px;
  }
  @keyframes slideIn { from { transform: translateX(20px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

  @media (max-width: 640px) {
    .form-row { grid-template-columns: 1fr; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .timeline { grid-template-columns: 48px 1fr; }
    .data-table { font-size: 13px; }
    .data-table th, .data-table td { padding: 8px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="title">📅 美容室予約管理</div>
    <div>
      <a href="/static/menu.html" class="btn" style="margin-right:8px">メニュー管理</a>
      <button class="btn btn-primary" onclick="openModal()">＋ 新規予約</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat-card">
      <div class="stat-label">本日の予約</div>
      <div class="stat-value" id="stat-today">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">今後の予約</div>
      <div class="stat-value" id="stat-upcoming">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">登録メニュー</div>
      <div class="stat-value" id="stat-menus">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">登録顧客</div>
      <div class="stat-value" id="stat-customers">0</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('board')" id="tab-board">予約ボード</button>
    <button class="tab" onclick="switchTab('list')" id="tab-list">予約一覧</button>
    <button class="tab" onclick="switchTab('history')" id="tab-history">変更・キャンセル履歴</button>
  </div>

  <div class="panel active" id="panel-board">
    <div class="date-nav">
      <button class="btn" onclick="changeDate(-1)">‹</button>
      <input type="date" id="board-date" onchange="renderBoard()">
      <button class="btn" onclick="changeDate(1)">›</button>
      <span class="date-label" id="board-date-label"></span>
    </div>
    <div class="timeline" id="timeline"></div>
  </div>

  <div class="panel" id="panel-list">
    <div class="toolbar">
      <div class="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="position:absolute;left:9px;top:50%;transform:translateY(-50%);color:#c7c7cc;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <input type="text" id="list-search" placeholder="顧客名・電話番号・メニューで検索" oninput="renderList()">
      </div>
      <select class="filter-select" id="list-status" onchange="renderList()">
        <option value="">すべて</option>
        <option value="confirmed">確定</option>
        <option value="tentative">仮予約</option>
        <option value="cancelled">キャンセル</option>
      </select>
      <select class="filter-select" id="list-sort" onchange="renderList()">
        <option value="date-asc">日付（近い順）</option>
        <option value="date-desc">日付（遠い順）</option>
      </select>
    </div>
    <div style="overflow-x:auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th>日付</th>
            <th>時間</th>
            <th>顧客</th>
            <th>電話番号</th>
            <th>メニュー</th>
            <th>ステータス</th>
            <th style="width:80px;"></th>
          </tr>
        </thead>
        <tbody id="list-body"></tbody>
      </table>
    </div>
    <div id="list-empty" class="empty" style="display:none;">該当する予約がありません</div>
  </div>

  <div class="panel" id="panel-history">
    <div class="toolbar">
      <div class="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="position:absolute;left:9px;top:50%;transform:translateY(-50%);color:#c7c7cc;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <input type="text" id="history-search" placeholder="顧客名で検索" oninput="renderHistory()">
      </div>
      <select class="filter-select" id="history-type" onchange="renderHistory()">
        <option value="">すべて</option>
        <option value="change">変更</option>
        <option value="cancel">キャンセル</option>
      </select>
    </div>
    <div id="history-list"></div>
    <div id="history-empty" class="empty" style="display:none;">該当する履歴がありません</div>
  </div>
</div>

<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">予約を登録</span>
      <button class="icon-btn" onclick="closeModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group">
        <label>既存のお客様から選ぶ（任意）</label>
        <select id="form-customer-select" onchange="fillCustomer()">
          <option value="">新規お客様</option>
        </select>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>お客様名 <span class="req">*</span></label>
          <input type="text" id="form-name" placeholder="山田 花子">
        </div>
        <div class="form-group">
          <label>電話番号</label>
          <input type="tel" id="form-phone" placeholder="090-1234-5678" oninput="formatPhone(this)">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>日付 <span class="req">*</span></label>
          <input type="date" id="form-date">
        </div>
        <div class="form-group">
          <label>時間 <span class="req">*</span></label>
          <select id="form-time"><option value="">選択</option></select>
        </div>
      </div>
      <div class="form-group">
        <label>メニュー <span class="req">*</span></label>
        <select id="form-menu"><option value="">選択</option></select>
      </div>
      <div class="form-group">
        <label>メモ</label>
        <textarea id="form-memo" placeholder="要望・注意事項など"></textarea>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitBooking()">登録する</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toasts"></div>

<script>
  let bookings = [];
  let histories = [];
  let customers = [];
  let menus = [];
  let currentDate = new Date();
  let editingId = null;
  const times = [];
  for (let h = 10; h <= 19; h++) { times.push(`${h}:00`); times.push(`${h}:30`); }

  function fmtDate(d) { return d.toISOString().split('T')[0]; }
  function fmtDateJp(d) {
    const dt = new Date(d + 'T00:00:00');
    return `${dt.getMonth()+1}月${dt.getDate()}日 (${['日','月','火','水','木','金','土'][dt.getDay()]})`;
  }
  function formatPhone(el) {
    const n = el.value.replace(/\\D/g,'');
    if (n.length === 11) el.value = n.replace(/(\\d{3})(\\d{4})(\\d{4})/,'$1-$2-$3');
    else if (n.length === 10) el.value = n.replace(/(\\d{3})(\\d{3})(\\d{4})/,'$1-$2-$3');
  }
  function maskPhone(p) {
    if (!p || p.length < 8) return p;
    return p.replace(/(\\d{3})-(\\d{4})-(\\d{4})/,'$1-****-$3');
  }
  function toast(msg) {
    const c = document.getElementById('toasts');
    const el = document.createElement('div'); el.className = 'toast'; el.textContent = msg;
    c.appendChild(el); setTimeout(() => el.remove(), 3000);
  }

  // Init
  document.getElementById('board-date').value = fmtDate(currentDate);
  document.getElementById('form-date').value = fmtDate(currentDate);
  const timeSel = document.getElementById('form-time');
  times.forEach(t => { const o = document.createElement('option'); o.value = t; o.textContent = t; timeSel.appendChild(o); });

  async function loadData() {
    const [bRes, hRes, cRes, mRes] = await Promise.all([
      fetch('/api/bookings/all').then(r => r.json()),
      fetch('/api/history').then(r => r.json()),
      fetch('/api/customers').then(r => r.json()),
      fetch('/api/menus').then(r => r.json())
    ]);
    bookings = bRes; histories = hRes; customers = cRes; menus = mRes;
    populateMenus(); populateCustomers(); updateStats(); renderBoard(); renderList(); renderHistory();
  }
  function populateMenus() {
    const sel = document.getElementById('form-menu');
    sel.innerHTML = '<option value="">選択</option>';
    menus.forEach(m => { const o = document.createElement('option'); o.value = m.id; o.textContent = `${m.name} (¥${m.price.toLocaleString()}, ${m.duration_minutes}分)`; sel.appendChild(o); });
  }
  function populateCustomers() {
    const sel = document.getElementById('form-customer-select');
    sel.innerHTML = '<option value="">新規お客様</option>';
    customers.forEach(c => { const o = document.createElement('option'); o.value = c.user_id; o.textContent = c.name || c.user_id; sel.appendChild(o); });
  }
  function fillCustomer() {
    const uid = document.getElementById('form-customer-select').value;
    if (!uid) { document.getElementById('form-name').value = ''; document.getElementById('form-phone').value = ''; return; }
    const c = customers.find(x => x.user_id === uid);
    if (c) { document.getElementById('form-name').value = c.name || ''; document.getElementById('form-phone').value = c.phone || ''; }
  }
  function updateStats() {
    const today = fmtDate(new Date());
    document.getElementById('stat-today').textContent = bookings.filter(b => b.booking_date === today && b.status === 'confirmed').length;
    document.getElementById('stat-upcoming').textContent = bookings.filter(b => b.booking_date >= today && b.status === 'confirmed').length;
    document.getElementById('stat-menus').textContent = menus.length;
    document.getElementById('stat-customers').textContent = customers.length;
  }

  function switchTab(name) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
  }

  function changeDate(delta) {
    currentDate.setDate(currentDate.getDate() + delta);
    document.getElementById('board-date').value = fmtDate(currentDate);
    renderBoard();
  }
  function renderBoard() {
    const date = document.getElementById('board-date').value;
    currentDate = new Date(date + 'T00:00:00');
    document.getElementById('board-date-label').textContent = fmtDateJp(date);
    const container = document.getElementById('timeline');
    container.innerHTML = '';
    const dayBookings = bookings.filter(b => b.booking_date === date).sort((a,b) => a.booking_time.localeCompare(b.booking_time));

    times.forEach(time => {
      const slot = document.createElement('div'); slot.className = 'time-slot';
      const label = document.createElement('div'); label.className = 'time-label'; label.textContent = time;
      const content = document.createElement('div'); content.className = 'time-content';
      const bs = dayBookings.filter(b => b.booking_time === time);
      bs.forEach(b => {
        const menu = menus.find(m => m.id === b.menu_id);
        const c = customers.find(x => x.user_id === b.user_id);
        const card = document.createElement('div');
        card.className = 'booking-card ' + (b.status || 'confirmed');
        card.innerHTML = `
          <div class="booking-name">${c ? c.name : b.user_id}</div>
          <div class="booking-meta">
            <span class="booking-tag">${menu ? menu.name : '不明'}</span>
            ${b.notes ? `<span class="booking-tag">${b.notes}</span>` : ''}
          </div>
          <div class="booking-actions">
            <button class="icon-btn" onclick="editBooking(${b.id})">✏️</button>
            <button class="icon-btn danger" onclick="deleteBooking(${b.id})">🗑</button>
          </div>`;
        content.appendChild(card);
      });
      slot.appendChild(label); slot.appendChild(content); container.appendChild(slot);
    });
  }

  function renderList() {
    const search = document.getElementById('list-search').value.toLowerCase();
    const status = document.getElementById('list-status').value;
    const sort = document.getElementById('list-sort').value;
    let data = bookings.filter(b => {
      if (status && b.status !== status) return false;
      if (!search) return true;
      const c = customers.find(x => x.user_id === b.user_id);
      const m = menus.find(x => x.id === b.menu_id);
      return (c && c.name && c.name.toLowerCase().includes(search)) || (c && c.phone && c.phone.includes(search)) || (m && m.name.toLowerCase().includes(search));
    });
    data.sort((a,b) => {
      const da = a.booking_date + ' ' + a.booking_time;
      const db = b.booking_date + ' ' + b.booking_time;
      return sort === 'date-desc' ? db.localeCompare(da) : da.localeCompare(db);
    });
    const tbody = document.getElementById('list-body');
    tbody.innerHTML = '';
    data.forEach(b => {
      const c = customers.find(x => x.user_id === b.user_id);
      const m = menus.find(x => x.id === b.menu_id);
      const tr = document.createElement('tr');
      const statusClass = 'status-' + (b.status || 'confirmed');
      const statusLabel = b.status === 'confirmed' ? '確定' : b.status === 'tentative' ? '仮' : b.status === 'cancelled' ? 'キャンセル' : '確定';
      tr.innerHTML = `
        <td>${b.booking_date.replace(/-/g,'/')}</td>
        <td>${b.booking_time}</td>
        <td style="font-weight:500">${c ? c.name : b.user_id}</td>
        <td style="font-variant-numeric:tabular-nums">${maskPhone(c ? c.phone : '')}</td>
        <td>${m ? m.name : '不明'}</td>
        <td><span class="status-badge ${statusClass}">${statusLabel}</span></td>
        <td>
          <div class="row-actions">
            <button class="icon-btn" onclick="editBooking(${b.id})">✏️</button>
            <button class="icon-btn danger" onclick="deleteBooking(${b.id})">🗑</button>
          </div>
        </td>`;
      tbody.appendChild(tr);
    });
    document.getElementById('list-empty').style.display = data.length ? 'none' : 'block';
  }

  function renderHistory() {
    const search = document.getElementById('history-search').value.toLowerCase();
    const type = document.getElementById('history-type').value;
    let data = histories.filter(h => {
      if (type && h.action !== type) return false;
      if (search && !(h.customer_name && h.customer_name.toLowerCase().includes(search))) return false;
      return true;
    });
    const container = document.getElementById('history-list');
    container.innerHTML = '';
    data.forEach(h => {
      const item = document.createElement('div'); item.className = 'history-item';
      const dt = new Date(h.created_at);
      const timeStr = `${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
      const isChange = h.action === 'modified' || h.action === 'created';
      item.innerHTML = `
        <div class="history-dot ${isChange ? 'change' : 'cancel'}"></div>
        <div class="history-content">
          <div class="history-header">
            <span class="history-type ${isChange ? 'change' : 'cancel'}">${h.action === 'created' ? '新規' : h.action === 'modified' ? '変更' : 'キャンセル'}</span>
            <span class="history-time">${timeStr}</span>
          </div>
          <div class="history-customer">${h.customer_name || '不明'}</div>
          <div class="history-detail">
            ${h.before_date ? `<span class="from">${h.before_date} ${h.before_time || ''}</span> → ` : ''}
            <span class="to">${h.after_date || ''} ${h.after_time || ''}</span>
            ${h.note ? `<div style="margin-top:4px;color:#c7c7cc">備考: ${h.note}</div>` : ''}
          </div>
        </div>`;
      container.appendChild(item);
    });
    document.getElementById('history-empty').style.display = data.length ? 'none' : 'block';
  }

  function openModal() {
    editingId = null;
    document.getElementById('modal-title').textContent = '予約を登録';
    clearForm();
    document.getElementById('modal').classList.add('open');
  }
  function closeModal() { document.getElementById('modal').classList.remove('open'); editingId = null; }
  function clearForm() {
    document.getElementById('form-customer-select').value = '';
    document.getElementById('form-name').value = '';
    document.getElementById('form-phone').value = '';
    document.getElementById('form-date').value = fmtDate(currentDate);
    document.getElementById('form-time').value = '';
    document.getElementById('form-menu').value = '';
    document.getElementById('form-memo').value = '';
  }
  async function submitBooking() {
    const name = document.getElementById('form-name').value.trim();
    const date = document.getElementById('form-date').value;
    const time = document.getElementById('form-time').value;
    const menuId = document.getElementById('form-menu').value;
    if (!name || !date || !time || !menuId) { toast('必須項目を入力してください'); return; }

    const payload = {
      customer_name: name,
      phone: document.getElementById('form-phone').value.trim(),
      booking_date: date,
      booking_time: time,
      menu_id: parseInt(menuId),
      notes: document.getElementById('form-memo').value.trim(),
      existing_user_id: document.getElementById('form-customer-select').value || null
    };
    const url = editingId ? `/api/bookings/${editingId}` : '/api/bookings';
    const method = editingId ? 'PUT' : 'POST';
    const res = await fetch(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) { toast(editingId ? '予約を更新しました' : '予約を登録しました'); closeModal(); loadData(); }
    else { toast('エラーが発生しました'); }
  }
  function editBooking(id) {
    const b = bookings.find(x => x.id === id);
    if (!b) return;
    editingId = id;
    const c = customers.find(x => x.user_id === b.user_id);
    document.getElementById('modal-title').textContent = '予約を編集';
    document.getElementById('form-name').value = c ? c.name : '';
    document.getElementById('form-phone').value = c ? c.phone : '';
    document.getElementById('form-date').value = b.booking_date;
    document.getElementById('form-time').value = b.booking_time;
    document.getElementById('form-menu').value = b.menu_id;
    document.getElementById('form-memo').value = b.notes || '';
    document.getElementById('modal').classList.add('open');
  }
  async function deleteBooking(id) {
    if (!confirm('この予約をキャンセルしますか？')) return;
    const res = await fetch(`/api/bookings/${id}`, { method: 'DELETE' });
    if (res.ok) { toast('予約をキャンセルしました'); loadData(); }
  }

  loadData();
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

@app.post("/api/booking/reschedule")
async def reschedule_booking_from_liff(data: RescheduleRequest):
    """
    お客様自身がLIFFカレンダーから予約日時を変更する
    本人の予約かどうかを必ず確認してから更新する
    """
    try:
        booking = db.get_booking(data.booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="予約が見つかりません")
        if booking["user_id"] != data.user_id:
            raise HTTPException(status_code=403, detail="この予約を変更する権限がありません")

        before_date = booking["booking_date"]
        before_time = booking["booking_time"]

        db.update_booking(data.booking_id, booking_date=data.booking_date, booking_time=data.booking_time)

        db.add_booking_history(
            booking_id=data.booking_id, action="modified", user_id=data.user_id,
            before_date=before_date, before_time=before_time,
            after_date=data.booking_date, after_time=data.booking_time,
            note="お客様がLIFFから変更"
        )

        menu = db.get_menu(booking["menu_id"])
        menu_name = menu["name"] if menu else "不明"

        # お客様への確認
        try:
            line_bot_api.push_message(
                data.user_id,
                TextSendMessage(text=f"✅ ご予約を変更しました\n\n📅 変更後: {data.booking_date} {data.booking_time}\n🎨 メニュー: {menu_name}\n予約ID: {data.booking_id}")
            )
        except Exception as e:
            logger.warning(f"Failed to send reschedule confirmation: {e}")

        # オーナーへ通知
        if OWNER_USER_ID:
            try:
                customer = db.get_customer(data.user_id)
                customer_name = customer["name"] if customer and customer["name"] else data.user_id[:10] + "..."
                line_bot_api.push_message(
                    OWNER_USER_ID,
                    TextSendMessage(text=f"📝 予約変更がありました\n\nお客様: {customer_name}\n変更前: {before_date} {before_time}\n変更後: {data.booking_date} {data.booking_time}\nメニュー: {menu_name}\n予約ID: {data.booking_id}")
                )
            except Exception as e:
                logger.warning(f"Failed to notify owner of reschedule: {e}")

        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rescheduling booking: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/board")
async def get_booking_board(start_date: str, days: int = 7):
    """
    予約ボード表示用：日付×時間のマス目に、予約が入っていれば顧客名・メニュー名を含めて返す
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = start + timedelta(days=days - 1)

        detailed_bookings = db.get_bookings_with_details_in_range(start.isoformat(), end.isoformat())
        booking_map = {}
        for b in detailed_bookings:
            key = f"{b['booking_date']}_{b['booking_time']}"
            booking_map[key] = {
                "booking_id": b["id"],
                "customer_name": b["customer_name"] or "不明",
                "customer_phone": b["customer_phone"],
                "menu_name": b["menu_name"] or "不明",
            }

        time_slots = generate_time_slots()
        date_list = []
        board = {}

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

            board[date_str] = {}
            for slot in time_slots:
                key = f"{date_str}_{slot}"
                if weekday_index in CLOSED_WEEKDAYS:
                    board[date_str][slot] = {"status": "closed"}
                elif key in booking_map:
                    board[date_str][slot] = {"status": "booked", **booking_map[key]}
                else:
                    board[date_str][slot] = {"status": "available"}

        return JSONResponse({"time_slots": time_slots, "dates": date_list, "board": board})
    except Exception as e:
        logger.error(f"Error getting booking board: {e}")
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

@app.get("/api/customers")
async def get_all_customers():
    """
    登録済みの全お客様を取得（手動登録フォームの「既存客から選ぶ」用）
    LINE連携済み(本物のuser_id)・未連携(manual-から始まるuser_id)の両方を含む
    """
    try:
        customers = db.get_all_customers()
        result = [
            {
                "user_id": c["user_id"],
                "name": c["name"],
                "phone": c["phone"],
                "is_line_linked": not c["user_id"].startswith("manual-")
            }
            for c in customers if c["name"]  # 名前未登録のデータは除外
        ]
        return JSONResponse({"customers": result})
    except Exception as e:
        logger.error(f"Error getting customers: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/booking/manual")
async def create_manual_booking(data: ManualBookingRequest):
    """
    ダッシュボードからの手動予約登録
    既存客(existing_user_id指定あり)ならその人に紐付け、
    新規なら仮のuser_id（manual-から始まる）を発行してLINE未連携として登録する
    """
    try:
        if data.existing_user_id:
            # 既存のお客様（LINE連携済み含む）に予約を追加する
            existing_customer = db.get_customer(data.existing_user_id)
            if not existing_customer:
                raise HTTPException(status_code=404, detail="指定されたお客様が見つかりません")
            target_user_id = data.existing_user_id
            # 電話番号だけ、入力があれば更新しておく（名前は変更しない）
            if data.phone:
                db.update_customer(target_user_id, phone=data.phone)
        else:
            # 新規のお客様（LINE未連携の仮アカウント）として登録する
            target_user_id = f"manual-{uuid.uuid4().hex[:16]}"
            db.save_customer_profile(user_id=target_user_id, name=data.name, phone=data.phone)

        booking_id = db.add_booking(
            user_id=target_user_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id,
            notes=data.note
        )
        if not booking_id:
            raise Exception("予約の保存に失敗しました")

        db.add_booking_history(
            booking_id=booking_id, action="created", user_id=target_user_id,
            after_date=data.booking_date, after_time=data.booking_time,
            note="手動登録（既存客）" if data.existing_user_id else "手動登録（新規）"
        )

        return JSONResponse({"status": "ok", "booking_id": booking_id})
    except HTTPException:
        raise
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
async def get_availability(start_date: str, days: int = 7, duration_minutes: int = None):
    """
    指定日から指定日数分の空き状況を返す
    定休日・営業時間・既存予約・メニューの所要時間（最終受付時刻）を踏まえて判定する
    """
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = start + timedelta(days=days - 1)

        booked_times = db.get_booked_times_in_range(
            start.isoformat(), end.isoformat()
        )
        time_slots = generate_time_slots()

        # メニューの所要時間を踏まえた最終受付時刻を計算
        # （所要時間が営業終了時刻をまたぐ枠は選べないようにする）
        business_end = datetime.strptime(BUSINESS_HOURS_END, "%H:%M")
        last_valid_start = None
        if duration_minutes:
            last_valid_start = (business_end - timedelta(minutes=duration_minutes)).strftime("%H:%M")

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
                day_avail = {}
                for slot in time_slots:
                    if slot in booked_for_date:
                        day_avail[slot] = "booked"
                    elif last_valid_start and slot > last_valid_start:
                        # 所要時間を踏まえると営業終了までに終わらない枠
                        day_avail[slot] = "too_late"
                    else:
                        day_avail[slot] = "available"
                availability[date_str] = day_avail

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
