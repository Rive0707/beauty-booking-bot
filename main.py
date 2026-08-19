"""
LINE美容室予約BOT メインアプリケーション - 完全版
FastAPI + LINE Messaging API + SQLite + APScheduler
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
LIFF_ID = os.getenv("LIFF_ID")

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
    existing_user_id: Optional[str] = None

class BookingUpdateRequest(BaseModel):
    """ダッシュボードからの予約変更"""
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None
    menu_id: Optional[int] = None

class DashboardBookingCreate(BaseModel):
    """新管理画面用予約登録"""
    customer_name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_id: int
    notes: Optional[str] = None
    existing_user_id: Optional[str] = None

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
    
    if text in ["予約", "予約する"]:
        line_handler.start_booking(user_id)
    elif text in ["予約確認", "マイページ", "履歴"]:
        line_handler.show_my_page(user_id)
    elif text in ["ヘルプ", "メニュー"]:
        line_handler.show_help(user_id)
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
    
    params = {}
    for param in postback_data.split("&"):
        if "=" in param:
            k, v = param.split("=", 1)
            params[k] = v
    
    action = params.get("action")
    
    if action == "select_date":
        date_str = event.postback.params.get("date")
        line_handler.on_date_selected(user_id, date_str)
    elif action == "select_time":
        time_str = event.postback.params.get("time") or params.get("time")
        line_handler.on_time_selected(user_id, time_str)
    elif action == "select_menu":
        menu_id = params.get("menu_id")
        line_handler.on_menu_selected(user_id, menu_id)
    elif action == "confirm_booking":
        line_handler.confirm_booking(user_id)
    elif action == "cancel_booking":
        booking_id = params.get("booking_id")
        line_handler.cancel_booking(user_id, booking_id)
    elif action == "modify_booking":
        booking_id = params.get("booking_id")
        line_handler.start_modify_booking(user_id, booking_id)
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
        <a href="/static/menu.html" class="btn" style="margin-right:8px">お客様画面</a>
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
    <button class="tab" onclick="switchTab('menus')" id="tab-menus">メニュー管理</button>
    <button class="tab" onclick="switchTab('customers')" id="tab-customers">顧客管理</button>
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

  <div class="panel" id="panel-menus">
    <div style="display:grid; grid-template-columns: 280px 1fr; gap: 20px;">
      <div>
        <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px;">
          <div style="font-weight:600; margin-bottom:12px;">新規メニュー追加</div>
          <div class="form-group">
            <label>メニュー名 <span class="req">*</span></label>
            <input type="text" id="menu-name" placeholder="カット">
          </div>
          <div class="form-group">
            <label>料金（B） <span class="req">*</span></label>
            <input type="number" id="menu-price" placeholder="5500">
          </div>
          <div class="form-group">
            <label>時間（分） <span class="req">*</span></label>
            <input type="number" id="menu-duration" placeholder="60">
          </div>
          <button class="btn btn-primary" onclick="addMenu()" style="width:100%; justify-content:center;">追加する</button>
        </div>
      </div>
      <div>
        <table class="data-table">
          <thead>
            <tr>
              <th>メニュー名</th>
              <th>料金</th>
              <th>時間</th>
              <th style="width:60px;"></th>
            </tr>
          </thead>
          <tbody id="menu-body"></tbody>
        </table>
        <div id="menu-empty" class="empty" style="display:none;">メニューが登録されていません</div>
      </div>
    </div>
  </div>

  <div class="panel" id="panel-customers">
    <div class="toolbar">
      <div class="search-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="position:absolute;left:9px;top:50%;transform:translateY(-50%);color:#c7c7cc;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        <input type="text" id="customer-search" placeholder="名前・電話番号で検索" oninput="renderCustomers()">
      </div>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>お名前</th>
          <th>電話番号</th>
          <th style="width:60px;"></th>
        </tr>
      </thead>
      <tbody id="customer-body"></tbody>
    </table>
    <div id="customer-empty" class="empty" style="display:none;">お客様が登録されていません</div>
  </div>

<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">予約を登録</span>
      <button class="icon-btn" onclick="closeModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
    <div class="form-group">
        <label>既存のお客様から選ぶ（名前・電話番号で検索）</label>
        <input type="text" id="form-customer-input" list="customer-list" placeholder="名前または電話番号を入力..." oninput="onCustomerInput()">
        <datalist id="customer-list"></datalist>
        <div id="customer-info-badge" style="display:none; margin-top:6px; padding:6px 10px; background:#e8f4fd; border-radius:6px; font-size:12px; color:#007aff; font-weight:500;"></div>
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

<!-- LINE連携（ガッチャンコ）モーダル -->
<div class="modal-overlay" id="modal-customer-merge" onclick="if(event.target===this)closeCustomerMergeModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">LINEアカウントとの連携（ガッチャンコ）</span>
      <button class="icon-btn" onclick="closeCustomerMergeModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <p style="font-size:13px; color:#8e8e93; margin-bottom:12px;">
        手動登録されたお客様の予約データを、LINEで登録されたアカウントに引き継ぎます。
      </p>
      <div class="form-group">
        <label>手動登録のお客さま</label>
        <input type="text" id="merge-manual-name" readonly style="background:#f2f2f7;">
      </div>
      <div class="form-group">
        <label>紐付けるLINEアカウントを選択 <span class="req">*</span></label>
        <select id="merge-line-user-select">
          <option value="">選択してください</option>
        </select>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeCustomerMergeModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitCustomerMerge()">連携を確定する</button>
    </div>
  </div>
</div>

<!-- LINE個別メッセージ送信モーダル -->
<div class="modal-overlay" id="modal-send-message" onclick="if(event.target===this)closeSendMessageModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="message-modal-title">LINEメッセージ送信</span>
      <button class="icon-btn" onclick="closeSendMessageModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group">
        <label>送信先のお客さま</label>
        <input type="text" id="message-target-name" readonly style="background:#f2f2f7;">
      </div>
      <div class="form-group">
        <label>メッセージ内容 <span class="req">*</span></label>
        <textarea id="message-text" rows="5" placeholder="例: 明日のご予約時間の確認でお送りいたしました。"></textarea>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeSendMessageModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitDirectMessage()">送信する</button>
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
  for (let h = 9; h < 19; h++) { times.push(h + ":00"); times.push(h + ":30"); }
　times.push("19:00");
  function fmtDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth()+1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}
  function fmtDateJp(d) {
    const dt = new Date(d + 'T00:00:00');
    return (dt.getMonth()+1) + "月" + dt.getDate() + "日 (" + ['日','月','火','水','木','金','土'][dt.getDay()] + ")";
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
    c.appendChild(el); setTimeout(function(){ el.remove(); }, 3000);
  }

  document.getElementById('board-date').value = fmtDate(currentDate);
  document.getElementById('form-date').value = fmtDate(currentDate);
  const timeSel = document.getElementById('form-time');
  times.forEach(function(t) { const o = document.createElement('option'); o.value = t; o.textContent = t; timeSel.appendChild(o); });

  function populateMenus() {
    const sel = document.getElementById('form-menu');
    sel.innerHTML = '<option value="">選択</option>';
    menus.forEach(function(m) { const o = document.createElement('option'); o.value = m.id; o.textContent = m.name + ' (¥' + m.price.toLocaleString() + ', ' + m.duration_minutes + '分)'; sel.appendChild(o); });
  }
  
  let selectedUserId = null;

  function populateCustomers() {
    const list = document.getElementById('customer-list');
    list.innerHTML = '';
    customers.forEach(function(c) {
      const opt = document.createElement('option');
      const lastVisit = c.last_visit ? '最終来店: ' + c.last_visit.replace(/-/g, '/') : '来店履歴なし';
      const phoneStr = c.phone ? maskPhone(c.phone) : '電話なし';
      opt.value = `${c.name || '(名前なし)'} (${phoneStr}) [${lastVisit}]`;
      list.appendChild(opt);
    });
  }

  function onCustomerInput() {
    const val = document.getElementById('form-customer-input').value;
    const badge = document.getElementById('customer-info-badge');
    
    const matched = customers.find(function(c) {
      const lastVisit = c.last_visit ? '最終来店: ' + c.last_visit.replace(/-/g, '/') : '来店履歴なし';
      const phoneStr = c.phone ? maskPhone(c.phone) : '電話なし';
      const label = `${c.name || '(名前なし)'} (${phoneStr}) [${lastVisit}]`;
      return label === val || c.name === val || c.phone === val;
    });

    if (matched) {
      selectedUserId = matched.user_id;
      document.getElementById('form-name').value = matched.name || '';
      document.getElementById('form-phone').value = matched.phone || '';
      
      const lastVisitStr = matched.last_visit ? matched.last_visit.replace(/-/g, '/') : 'なし';
      badge.style.display = 'block';
      badge.innerHTML = `👤 <b>${matched.name}</b> 様 | TEL: ${matched.phone || '未登録'} | 最終来店: <b>${lastVisitStr}</b>`;
    } else {
      selectedUserId = null;
      badge.style.display = 'none';
      if (val && !val.includes('(')) {
        document.getElementById('form-name').value = val;
      }
    }
  }
  function updateStats() {
    const today = fmtDate(new Date());
    document.getElementById('stat-today').textContent = bookings.filter(function(b){ return b.booking_date === today && b.status === 'confirmed'; }).length;
    document.getElementById('stat-upcoming').textContent = bookings.filter(function(b){ return b.booking_date >= today && b.status === 'confirmed'; }).length;
    document.getElementById('stat-menus').textContent = menus.length;
    document.getElementById('stat-customers').textContent = customers.length;
  }

  function switchTab(name) {
    document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
    document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
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
    const dayBookings = bookings.filter(function(b){ return b.booking_date === date; }).sort(function(a,b){ return a.booking_time.localeCompare(b.booking_time); });

    times.forEach(function(time) {
      const slot = document.createElement('div'); slot.className = 'time-slot';
      const label = document.createElement('div'); label.className = 'time-label'; label.textContent = time;
      const content = document.createElement('div'); content.className = 'time-content';
      const bs = dayBookings.filter(function(b){ return b.booking_time === time; });
      bs.forEach(function(b) {
        const menu = menus.find(function(m){ return m.id === b.menu_id; });
        const c = customers.find(function(x){ return x.user_id === b.user_id; });
        const card = document.createElement('div');
        card.className = 'booking-card ' + (b.status || 'confirmed');
        card.innerHTML = '<div class="booking-name">' + (c ? c.name : b.user_id) + '</div>' +
          '<div class="booking-meta">' +
            '<span class="booking-tag">' + (menu ? menu.name : '不明') + '</span>' +
            (b.notes ? '<span class="booking-tag">' + b.notes + '</span>' : '') +
          '</div>' +
          '<div class="booking-actions">' +
            '<button class="icon-btn" onclick="editBooking(' + b.id + ')">✏️</button>' +
            '<button class="icon-btn danger" onclick="deleteBooking(' + b.id + ')">🗑</button>' +
          '</div>';
        content.appendChild(card);
      });
      slot.appendChild(label); slot.appendChild(content); container.appendChild(slot);
    });
  }

  function renderList() {
    const search = document.getElementById('list-search').value.toLowerCase();
    const status = document.getElementById('list-status').value;
    const sort = document.getElementById('list-sort').value;
    let data = bookings.filter(function(b) {
      if (status && b.status !== status) return false;
      if (!search) return true;
      const c = customers.find(function(x){ return x.user_id === b.user_id; });
      const m = menus.find(function(x){ return x.id === b.menu_id; });
      return (c && c.name && c.name.toLowerCase().includes(search)) || (c && c.phone && c.phone.includes(search)) || (m && m.name.toLowerCase().includes(search));
    });
    data.sort(function(a,b) {
      const da = a.booking_date + ' ' + a.booking_time;
      const db = b.booking_date + ' ' + b.booking_time;
      return sort === 'date-desc' ? db.localeCompare(da) : da.localeCompare(db);
    });
    const tbody = document.getElementById('list-body');
    tbody.innerHTML = '';
    data.forEach(function(b) {
      const c = customers.find(function(x){ return x.user_id === b.user_id; });
      const m = menus.find(function(x){ return x.id === b.menu_id; });
      const tr = document.createElement('tr');
      const statusClass = 'status-' + (b.status || 'confirmed');
      const statusLabel = b.status === 'confirmed' ? '確定' : b.status === 'tentative' ? '仮' : b.status === 'cancelled' ? 'キャンセル' : '確定';
      tr.innerHTML = '<td>' + b.booking_date.replace(/-/g,'/') + '</td>' +
        '<td>' + b.booking_time + '</td>' +
        '<td style="font-weight:500">' + (c ? c.name : b.user_id) + '</td>' +
        '<td style="font-variant-numeric:tabular-nums">' + maskPhone(c ? c.phone : '') + '</td>' +
        '<td>' + (m ? m.name : '不明') + '</td>' +
        '<td><span class="status-badge ' + statusClass + '">' + statusLabel + '</span></td>' +
        '<td>' +
          '<div class="row-actions">' +
            '<button class="icon-btn" onclick="editBooking(' + b.id + ')">✏️</button>' +
            '<button class="icon-btn danger" onclick="deleteBooking(' + b.id + ')">🗑</button>' +
          '</div>' +
        '</td>';
      tbody.appendChild(tr);
    });
    document.getElementById('list-empty').style.display = data.length ? 'none' : 'block';
  }

  function renderHistory() {
    const search = document.getElementById('history-search').value.toLowerCase();
    const type = document.getElementById('history-type').value;
    let data = histories.filter(function(h) {
      if (type && h.action !== type) return false;
      if (search && !(h.customer_name && h.customer_name.toLowerCase().includes(search))) return false;
      return true;
    });
    const container = document.getElementById('history-list');
    container.innerHTML = '';
    data.forEach(function(h) {
      const item = document.createElement('div'); item.className = 'history-item';
      const dt = new Date(h.created_at);
      const timeStr = (dt.getMonth()+1) + '/' + dt.getDate() + ' ' + String(dt.getHours()).padStart(2,'0') + ':' + String(dt.getMinutes()).padStart(2,'0');
      const isChange = h.action === 'modified' || h.action === 'created';
      item.innerHTML = '<div class="history-dot ' + (isChange ? 'change' : 'cancel') + '"></div>' +
        '<div class="history-content">' +
          '<div class="history-header">' +
            '<span class="history-type ' + (isChange ? 'change' : 'cancel') + '">' + (h.action === 'created' ? '新規' : h.action === 'modified' ? '変更' : 'キャンセル') + '</span>' +
            '<span class="history-time">' + timeStr + '</span>' +
          '</div>' +
          '<div class="history-customer">' + (h.customer_name || '不明') + '</div>' +
          '<div class="history-detail">' +
            (h.before_date ? '<span class="from">' + h.before_date + ' ' + (h.before_time || '') + '</span> → ' : '') +
            '<span class="to">' + (h.after_date || '') + ' ' + (h.after_time || '') + '</span>' +
            (h.note ? '<div style="margin-top:4px;color:#c7c7cc">備考: ' + h.note + '</div>' : '') +
          '</div>' +
        '</div>';
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
    selectedUserId = null;
    const inputEl = document.getElementById('form-customer-input');
    if (inputEl) inputEl.value = '';
    const badgeEl = document.getElementById('customer-info-badge');
    if (badgeEl) badgeEl.style.display = 'none';
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
      existing_user_id: selectedUserId
    };
    const url = editingId ? '/api/bookings/' + editingId : '/api/bookings';
    const method = editingId ? 'PUT' : 'POST';
    const res = await fetch(url, { method: method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) { toast(editingId ? '予約を更新しました' : '予約を登録しました'); closeModal(); loadData(); }
    else { toast('エラーが発生しました'); }
  }
  function editBooking(id) {
    const b = bookings.find(function(x){ return x.id === id; });
    if (!b) return;
    editingId = id;
    const c = customers.find(function(x){ return x.user_id === b.user_id; });
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
    const res = await fetch('/api/bookings/' + id, { method: 'DELETE' });
    if (res.ok) { toast('予約をキャンセルしました'); loadData(); }
  }
  
function renderCustomers() {
    const q = (document.getElementById('customer-search').value || '').toLowerCase();
    const filtered = customers.filter(function(c) {
      const name = (c.name || '').toLowerCase();
      const phone = (c.phone || '').toLowerCase();
      return name.includes(q) || phone.includes(q);
    });
    const body = document.getElementById('customer-body');
    
    body.innerHTML = filtered.map(function(c) {
      const id = c.user_id || '';
      const isLine = !id.startsWith('manual_');
      const badge = isLine 
        ? `<span class="status-badge status-confirmed">LINE</span>` 
        : `<span class="status-badge" style="background:#e5e5ea;color:#8e8e93;">手動</span>`;
      
      const customerName = c.name ? c.name : '(名前未登録)';
      const customerPhone = c.phone ? c.phone : '-';
      const lastVisit = c.last_visit ? String(c.last_visit).replace(/-/g, '/') : '-';

      const lineBtn = isLine 
        ? `<button class="icon-btn" title="LINE送信" onclick="openSendMessageModal('${id}')">✉️</button>`
        : `<button class="icon-btn" title="LINE連携" onclick="openCustomerMergeModal('${id}')">🔗</button>`;

      return `<tr>
        <td>${badge}</td>
        <td style="font-weight:500">${customerName}</td>
        <td>${customerPhone}</td>
        <td>${lastVisit}</td>
        <td>
          <div class="row-actions" style="opacity:1;">
            ${lineBtn}
            <button class="icon-btn" title="来店履歴" onclick="showCustomerHistory('${id}')">📋</button>
            <button class="icon-btn" title="編集" onclick="openCustomerEditModal('${id}')">✏️</button>
            <button class="icon-btn danger" title="削除" onclick="deleteCustomer('${id}')">🗑️</button>
          </div>
        </td>
      </tr>`;
    }).join('');
    
    document.getElementById('customer-empty').style.display = filtered.length ? 'none' : 'block';
  }

  // LINEメッセージ送信制御
  let sendingMessageUserId = null;
  function openSendMessageModal(userId) {
    const c = customers.find(x => x.user_id === userId);
    if (!c) return;
    sendingMessageUserId = userId;
    document.getElementById('message-target-name').value = `${c.name || 'LINEユーザー'} 様`;
    document.getElementById('message-text').value = '';
    document.getElementById('modal-send-message').classList.add('open');
  }
  function closeSendMessageModal() {
    document.getElementById('modal-send-message').classList.remove('open');
    sendingMessageUserId = null;
  }
  async function submitDirectMessage() {
    const text = document.getElementById('message-text').value.trim();
    if (!sendingMessageUserId || !text) { toast('メッセージを入力してください'); return; }
    const res = await fetch('/api/customers/send-message', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ user_id: sendingMessageUserId, message: text })
    });
    if (res.ok) { toast('LINEメッセージを送信しました！'); closeSendMessageModal(); }
    else { toast('送信に失敗しました'); }
  }

  // 顧客編集モーダル制御
  let editingCustomerUserId = null;
  function openCustomerEditModal(userId) {
    const c = customers.find(x => x.user_id === userId);
    if (!c) return;
    editingCustomerUserId = userId;
    document.getElementById('edit-customer-name').value = c.name || '';
    document.getElementById('edit-customer-phone').value = c.phone || '';
    document.getElementById('modal-customer-edit').classList.add('open');
  }
  function closeCustomerEditModal() {
    document.getElementById('modal-customer-edit').classList.remove('open');
    editingCustomerUserId = null;
  }
  async function saveCustomerEdit() {
    if (!editingCustomerUserId) return;
    const name = document.getElementById('edit-customer-name').value.trim();
    const phone = document.getElementById('edit-customer-phone').value.trim();
    if (!name) { toast('お名前を入力してください'); return; }
    const res = await fetch('/api/customers/' + encodeURIComponent(editingCustomerUserId), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name: name, phone: phone })
    });
    if (res.ok) { toast('お客様情報を更新しました'); closeCustomerEditModal(); loadData(); }
    else { toast('更新に失敗しました'); }
  }

  // 来店履歴モーダル表示
  function showCustomerHistory(userId) {
    const c = customers.find(x => x.user_id === userId);
    if (!c) return;
    document.getElementById('history-modal-title').textContent = `${c.name || 'お客様'} 様の来店・予約履歴`;
    const userBookings = bookings.filter(b => b.user_id === userId)
      .sort((a,b) => (b.booking_date + b.booking_time).localeCompare(a.booking_date + a.booking_time));
    const container = document.getElementById('customer-history-body');
    if (userBookings.length === 0) {
      container.innerHTML = '<div class="empty">予約・来店履歴がありません</div>';
    } else {
      container.innerHTML = userBookings.map(b => {
        const m = menus.find(x => x.id === b.menu_id);
        const statusLabel = b.status === 'confirmed' ? '確定' : b.status === 'cancelled' ? 'キャンセル' : '仮';
        const statusClass = 'status-' + (b.status || 'confirmed');
        return `<div style="border-bottom:1px solid #e5e5ea; padding:10px 0;">
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <b>${b.booking_date.replace(/-/g, '/')} ${b.booking_time}</b>
            <span class="status-badge ${statusClass}">${statusLabel}</span>
          </div>
          <div style="font-size:13px; color:#1d1d1f; margin-top:4px;">メニュー: ${m ? m.name : '不明'}</div>
          ${b.notes ? `<div style="font-size:12px; color:#8e8e93; margin-top:2px;">メモ: ${b.notes}</div>` : ''}
        </div>`;
      }).join('');
    }
    document.getElementById('modal-customer-history').classList.add('open');
  }
  function closeCustomerHistoryModal() {
    document.getElementById('modal-customer-history').classList.remove('open');
  }

  // LINE連携（ガッチャンコ）制御
  let mergingManualUserId = null;
  function openCustomerMergeModal(manualUserId) {
    const manualCustomer = customers.find(x => x.user_id === manualUserId);
    if (!manualCustomer) return;
    mergingManualUserId = manualUserId;
    document.getElementById('merge-manual-name').value = `${manualCustomer.name || '名前なし'} (${manualCustomer.phone || '電話なし'})`;
    const lineUsers = customers.filter(x => !x.user_id.startsWith('manual_'));
    const sel = document.getElementById('merge-line-user-select');
    sel.innerHTML = '<option value="">選択してください</option>';
    lineUsers.forEach(function(u) {
      const opt = document.createElement('option');
      opt.value = u.user_id;
      opt.textContent = `${u.name || 'LINEユーザー'} (${u.phone || 'TEL未登録'})`;
      sel.appendChild(opt);
    });
    document.getElementById('modal-customer-merge').classList.add('open');
  }
  function closeCustomerMergeModal() {
    document.getElementById('modal-customer-merge').classList.remove('open');
    mergingManualUserId = null;
  }
  async function submitCustomerMerge() {
    const lineUserId = document.getElementById('merge-line-user-select').value;
    if (!mergingManualUserId || !lineUserId) { toast('連携するLINEアカウントを選択してください'); return; }
    if (!confirm('この手動登録データを指定のLINEアカウントに統合しますか？')) return;
    const res = await fetch('/api/customers/merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ manual_user_id: mergingManualUserId, line_user_id: lineUserId })
    });
    if (res.ok) { toast('LINEアカウントとの連携が完了しました！'); closeCustomerMergeModal(); loadData(); }
    else { toast('連携処理に失敗しました'); }
  }

  // 顧客削除
  async function deleteCustomer(userId) {
    if (!confirm('このお客様を削除しますか？')) return;
    const res = await fetch('/api/customers/' + encodeURIComponent(userId), { method: 'DELETE' });
    const data = await res.json();
    if (res.ok) { toast('お客様を削除しました'); loadData(); }
    else { toast(data.error || '削除に失敗しました'); }
  }

  // メニュー操作
  function renderMenus() {
    const tbody = document.getElementById('menu-body');
    tbody.innerHTML = '';
    menus.forEach(function(m) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td style="font-weight:500">' + m.name + '</td>' +
        '<td>¥' + m.price.toLocaleString() + '</td>' +
        '<td>' + m.duration_minutes + '分</td>' +
        '<td><button class="icon-btn danger" onclick="deleteMenu(' + m.id + ')">🗑️</button></td>';
      tbody.appendChild(tr);
    });
    document.getElementById('menu-empty').style.display = menus.length ? 'none' : 'block';
  }
  async function addMenu() {
    const name = document.getElementById('menu-name').value.trim();
    const price = parseInt(document.getElementById('menu-price').value);
    const duration = parseInt(document.getElementById('menu-duration').value);
    if (!name || !price || !duration) { toast('全項目を入力してください'); return; }
    const res = await fetch('/api/menus', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name: name, price: price, duration_minutes: duration})
    });
    if (res.ok) {
      toast('メニューを追加しました');
      document.getElementById('menu-name').value = '';
      document.getElementById('menu-price').value = '';
      document.getElementById('menu-duration').value = '';
      loadData();
    } else { toast('追加に失敗しました'); }
  }
  async function deleteMenu(id) {
    if (!confirm('このメニューを削除しますか？')) return;
    const res = await fetch('/api/menus/' + id, {method: 'DELETE'});
    if (res.ok) { toast('メニューを削除しました'); loadData(); }
  }

  // 休業日描画・操作
// 休業日描画・操作（修正後）
  let holidays = [];
  function renderHolidays() {
    const tbody = document.getElementById('holiday-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    holidays.forEach(function(h) {
      const tr = document.createElement('tr');
      const dateStr = h.closed_date ? String(h.closed_date).replace(/-/g, '/') : '-';
      const noteStr = h.note ? h.note : '-';
      const closedDate = h.closed_date || '';

      tr.innerHTML = `<tr>
        <td style="font-weight:500">${dateStr}</td>
        <td>${noteStr}</td>
        <td><button class="icon-btn danger" onclick="deleteHoliday('${closedDate}')">🗑️</button></td>
      </tr>`;
      tbody.appendChild(tr);
    });
    const emptyEl = document.getElementById('holiday-empty');
    if (emptyEl) emptyEl.style.display = holidays.length ? 'none' : 'block';
  }
  async function addHoliday() {
    const date = document.getElementById('holiday-date').value;
    const note = document.getElementById('holiday-note').value.trim();
    if (!date) { toast('日付を選択してください'); return; }
    const res = await fetch('/api/closed-days', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ closed_date: date, note: note })
    });
    if (res.ok) {
      toast('休業日を登録しました');
      document.getElementById('holiday-date').value = '';
      document.getElementById('holiday-note').value = '';
      loadData();
    } else { toast('登録に失敗しました'); }
  }
  async function deleteHoliday(closedDate) {
    if (!confirm(closedDate + ' の休業設定を解除しますか？')) return;
    const res = await fetch('/api/closed-days/' + closedDate, { method: 'DELETE' });
    if (res.ok) { toast('休業設定を解除しました'); loadData(); }
  }

  // データ一括ロード
  async function loadData() {
    const [bRes, hRes, cRes, mRes, holRes] = await Promise.all([
      fetch('/api/bookings/all').then(function(r){ return r.json(); }),
      fetch('/api/history').then(function(r){ return r.json(); }),
      fetch('/api/customers').then(function(r){ return r.json(); }),
      fetch('/api/menus').then(function(r){ return r.json(); }),
      fetch('/api/closed-days').then(function(r){ return r.json(); }).catch(function(){ return {closed_days:[]}; })
    ]);
    bookings = bRes; histories = hRes; customers = cRes; menus = mRes.menus || [];
    holidays = (holRes && holRes.closed_days) ? holRes.closed_days : [];
    populateMenus(); populateCustomers(); updateStats(); renderBoard(); renderList(); renderHistory(); renderMenus(); renderCustomers(); renderHolidays();
  }
  loadData();
</script>
</div>
</body>
</html>
"""

# ===============================
# API エンドポイント（既存）
# ===============================

@app.post("/api/booking/add-with-customer")
async def add_booking_with_customer(data: BookingAddWithCustomerRequest):
    """
    Web管理画面から顧客を選択して予約を追加
    その顧客の LINE アカウントに自動紐付け
    1週間前に自動リマインダー送信
    """
    try:
        booking_id = db.add_booking(
            user_id=data.customer_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id,
            notes=data.notes
        )
        if not booking_id:
            raise HTTPException(status_code=500, detail="予約の追加に失敗しました")

        db.add_booking_history(
            booking_id=booking_id,
            action="created",
            user_id=data.customer_id,
            after_date=data.booking_date,
            after_time=data.booking_time
        )

        customer = db.get_customer(data.customer_id)
        menu = db.get_menu(data.menu_id)

        line_handler.notify_owner(
            f"🆕 新規予約（管理画面から）\n\n"
            f"お客様: {customer['name'] if customer else '不明'}\n"
            f"日時: {data.booking_date} {data.booking_time}\n"
            f"メニュー: {menu['name'] if menu else '不明'}"
        )

        return JSONResponse({
            "status": "ok",
            "booking_id": booking_id,
            "message": "予約を追加しました"
        })

    except Exception as e:
        logger.error(f"Error adding booking with customer: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/booking")
async def add_booking_manual(data: ManualBookingRequest):
    """
    ダッシュボードからの手動予約登録
    LINE未連携のお客様でも登録可能
    """
    try:
        user_id = data.existing_user_id
        if not user_id:
            user_id = f"manual_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"
            db.save_customer_profile(
                user_id=user_id,
                name=data.name,
                phone=data.phone
            )

        booking_id = db.add_booking(
            user_id=user_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id,
            notes=data.note
        )

        if not booking_id:
            raise HTTPException(status_code=500, detail="予約の追加に失敗しました")

        db.add_booking_history(
            booking_id=booking_id,
            action="created",
            user_id=user_id,
            after_date=data.booking_date,
            after_time=data.booking_time,
            note="管理画面から手動登録"
        )

        return JSONResponse({
            "status": "ok",
            "booking_id": booking_id
        })

    except Exception as e:
        logger.error(f"Error adding manual booking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/booking/{booking_id}")
async def update_booking(booking_id: int, data: BookingUpdateRequest):
    """予約変更（日付・時間・メニュー）"""
    try:
        booking = db.get_booking(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="予約が見つかりません")

        original_date = booking["booking_date"]
        original_time = booking["booking_time"]

        db.update_booking(
            booking_id=booking_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id
        )

        db.add_booking_history(
            booking_id=booking_id,
            action="modified",
            user_id=booking["user_id"],
            before_date=original_date,
            before_time=original_time,
            after_date=data.booking_date,
            after_time=data.booking_time
        )

        return JSONResponse({"status": "ok", "message": "予約を変更しました"})

    except Exception as e:
        logger.error(f"Error updating booking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/booking/{booking_id}")
async def delete_booking(booking_id: int):
    """予約キャンセル"""
    try:
        booking = db.get_booking(booking_id)
        if not booking:
            raise HTTPException(status_code=404, detail="予約が見つかりません")

        db.cancel_booking(booking_id)

        db.add_booking_history(
            booking_id=booking_id,
            action="cancelled",
            user_id=booking["user_id"],
            before_date=booking["booking_date"],
            before_time=booking["booking_time"]
        )

        return JSONResponse({"status": "ok", "message": "予約をキャンセルしました"})

    except Exception as e:
        logger.error(f"Error cancelling booking: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/menus")
async def add_menu(data: MenuAddRequest):
    """メニュー追加"""
    try:
        menu_id = db.add_menu(data.name, data.price, data.duration_minutes)
        if not menu_id:
            raise HTTPException(status_code=500, detail="メニューの追加に失敗しました")
        return JSONResponse({
            "status": "ok",
            "menu_id": menu_id,
            "message": "メニューを追加しました"
        })
    except Exception as e:
        logger.error(f"Error adding menu: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/menus/{menu_id}")
async def delete_menu(menu_id: int):
    """メニュー削除"""
    try:
        db.delete_menu(menu_id)
        return JSONResponse({"status": "ok", "message": "メニューを削除しました"})
    except Exception as e:
        logger.error(f"Error deleting menu: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/bookings/upcoming/all")
async def get_all_upcoming_bookings_api():
    """今後の全予約取得（管理画面用）"""
    try:
        bookings = db.get_all_upcoming_bookings()
        return JSONResponse({
            "bookings": [dict(b) for b in bookings]
        })
    except Exception as e:
        logger.error(f"Error getting upcoming bookings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/availability")
async def get_availability(start_date: str, days: int = 7, duration_minutes: int = None):
    """
    指定期間の予約可能状況を取得（LIFFカレンダー用）
    """
    try:
        from datetime import datetime, timedelta, timezone

        JST_TH = timezone(timedelta(hours=7))
        now = datetime.now(JST_TH)
        today_str = now.date().isoformat()
        cutoff_minutes = now.hour * 60 + now.minute + 30

        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = start + timedelta(days=days - 1)

        business_start_h, business_start_m = map(int, BUSINESS_HOURS_START.split(":"))
        business_end_h, business_end_m = map(int, BUSINESS_HOURS_END.split(":"))
        business_start_minutes = business_start_h * 60 + business_start_m
        business_end_minutes = business_end_h * 60 + business_end_m

        time_slots = []
        current_minutes = business_start_minutes
        while current_minutes < business_end_minutes:
            h, m = divmod(current_minutes, 60)
            time_slots.append(f"{h:02d}:{m:02d}")
            current_minutes += SLOT_INTERVAL_MINUTES

        weekday_labels = ['月', '火', '水', '木', '金', '土', '日']

        date_list = []
        availability = {}
        current = start
        while current <= end:
            date_str = current.isoformat()
            weekday_index = current.weekday()
            date_list.append({
                "date": date_str,
                "day": current.day,
                "weekday": weekday_labels[weekday_index]
            })

            booked_times = db.get_booked_times_in_range(date_str, date_str)

            if duration_minutes:
                max_duration = duration_minutes
            else:
                all_menus = db.get_all_menus()
                max_duration = max((m[3] for m in all_menus), default=60)

            last_valid_start = None
            if business_end_minutes - max_duration >= business_start_minutes:
                last_valid_start_minutes = business_end_minutes - max_duration
                last_valid_hour, last_valid_minute = divmod(last_valid_start_minutes, 60)
                last_valid_start = f"{last_valid_hour:02d}:{last_valid_minute:02d}"

            if weekday_index in CLOSED_WEEKDAYS:
                availability[date_str] = {slot: "closed" for slot in time_slots}
            else:
                occupied_slots = set()
                for booking_time in booked_times.get(date_str, []):
                    b_h, b_m = map(int, booking_time.split(":"))
                    b_start_minutes = b_h * 60 + b_m
                    b_end_minutes = b_start_minutes + 60
                    for slot in time_slots:
                        s_h, s_m = map(int, slot.split(":"))
                        s_minutes = s_h * 60 + s_m
                        if b_start_minutes <= s_minutes < b_end_minutes:
                            occupied_slots.add(slot)

                day_avail = {}
                for slot in time_slots:
                    s_h, s_m = map(int, slot.split(":"))
                    s_minutes = s_h * 60 + s_m
                    if slot in occupied_slots:
                        day_avail[slot] = "booked"
                    elif date_str == today_str and s_minutes < cutoff_minutes:
                        day_avail[slot] = "too_late"
                    elif last_valid_start and slot > last_valid_start:
                        day_avail[slot] = "too_late"
                    else:
                        day_avail[slot] = "available"
                availability[date_str] = day_avail

            current += timedelta(days=1)

        return JSONResponse({
            "time_slots": time_slots,
            "dates": date_list,
            "availability": availability
        })
    except Exception as e:
        logger.error(f"Error getting availability: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/booking/create")
async def create_booking_from_liff(data: BookingCreateFromLiffRequest):
    """LIFFからの予約確定（お客様情報付き）"""
    try:
        if not db.is_slot_available(data.booking_date, data.booking_time):
            raise HTTPException(status_code=409, detail="その時間帯はすでに予約が入っています")

        db.save_customer_profile(
            user_id=data.user_id,
            name=data.name,
            furigana=data.furigana,
            gender=data.gender,
            birthdate=data.birthdate,
            phone=data.phone
        )

        booking_id = db.add_booking(
            user_id=data.user_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time,
            menu_id=data.menu_id
        )

        if not booking_id:
            raise HTTPException(status_code=500, detail="予約に失敗しました")

        db.add_booking_history(
            booking_id=booking_id,
            action="created",
            user_id=data.user_id,
            after_date=data.booking_date,
            after_time=data.booking_time
        )

        menu = db.get_menu(data.menu_id)
        menu_name = menu["name"] if menu else "不明"

        line_handler.send_text(data.user_id, f"""
🎉 ご予約ありがとうございます！

📅 {data.booking_date} {data.booking_time}
📍 予約ID: {booking_id}
✂️ メニュー: {menu_name}

ご来店の7日前・3日前にリマインダーをお送りいたします。

📞 ご質問やご変更は、いつでもお気軽にご連絡ください。
""")

        line_handler.notify_owner(
            f"🆕 LIFFから新規予約\n\n"
            f"お客様: {data.name}\n"
            f"日時: {data.booking_date} {data.booking_time}\n"
            f"メニュー: {menu_name}"
        )

        return JSONResponse({
            "status": "ok",
            "booking_id": booking_id
        })

    except Exception as e:
        logger.error(f"Error creating booking from LIFF: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/booking/reschedule")
async def reschedule_from_liff(data: RescheduleRequest):
    """LIFFからの予約変更"""
    try:
        if not db.is_slot_available(data.booking_date, data.booking_time, exclude_booking_id=data.booking_id):
            raise HTTPException(status_code=409, detail="その時間帯はすでに予約が入っています")
        booking = db.get_booking(data.booking_id)
        if not booking or booking["user_id"] != data.user_id:
            raise HTTPException(status_code=404, detail="予約が見つかりません")

        original_date = booking["booking_date"]
        original_time = booking["booking_time"]

        db.update_booking(
            booking_id=data.booking_id,
            booking_date=data.booking_date,
            booking_time=data.booking_time
        )

        db.add_booking_history(
            booking_id=data.booking_id,
            action="modified",
            user_id=data.user_id,
            before_date=original_date,
            before_time=original_time,
            after_date=data.booking_date,
            after_time=data.booking_time
        )

        menu = db.get_menu(booking["menu_id"])
        menu_name = menu["name"] if menu else "不明"

        line_handler.send_text(data.user_id, f"""
✅ ご予約を変更しました

📅 変更後の日時: {data.booking_date} {data.booking_time}
📍 予約ID: {data.booking_id}

ご来店をお待ちしております！
""")

        line_handler.notify_owner(
            f"📝 予約変更がありました\n\n"
            f"変更前: {original_date} {original_time}\n"
            f"変更後: {data.booking_date} {data.booking_time}\n"
            f"メニュー: {menu_name}\n"
            f"予約ID: {data.booking_id}"
        )

        return JSONResponse({"status": "ok", "message": "予約を変更しました"})

    except Exception as e:
        logger.error(f"Error rescheduling from LIFF: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ===============================
# 新管理画面用API
# ===============================

@app.get("/api/bookings/all")
async def api_get_all_bookings():
    """全予約（キャンセル含む）を顧客名・メニュー名付きで取得"""
    rows = db.get_all_bookings_with_details()
    return [dict(r) for r in rows]

@app.get("/api/history")
async def api_get_history(limit: int = 50):
    """変更・キャンセル履歴"""
    rows = db.get_booking_history(limit=limit)
    return [dict(r) for r in rows]

@app.get("/api/customers")
async def api_get_customers():
    """全顧客"""
    rows = db.get_all_customers()
    return [dict(r) for r in rows]

@app.delete("/api/customers/{user_id}")
async def api_delete_customer(user_id: str):
    """顧客削除（予約が残っている場合は削除不可）"""
    customer = db.get_customer(user_id)
    if not customer:
        return JSONResponse({"error": "not found"}, status_code=404)
    if db.has_active_bookings(user_id):
        return JSONResponse({"error": "この顧客には現在有効な予約が残っているため削除できません"}, status_code=400)
    if db.delete_customer(user_id):
        return {"status": "ok"}
    return JSONResponse({"error": "削除に失敗しました"}, status_code=500)


class CustomerMergeRequest(BaseModel):
    manual_user_id: str
    line_user_id: str

@app.post("/api/customers/merge")
async def api_merge_customer(data: CustomerMergeRequest):
    """手動顧客とLINE顧客の統合"""
    success = db.merge_customers(data.manual_user_id, data.line_user_id)
    if success:
        return {"status": "ok", "message": "LINE連携が完了しました"}
    return JSONResponse({"error": "連携処理に失敗しました"}, status_code=500)


class DirectMessageRequest(BaseModel):
    user_id: str
    message: str


@app.post("/api/customers/send-message")
async def api_send_direct_message(data: DirectMessageRequest):
    """顧客へLINE直接メッセージを送信"""
    if data.user_id.startswith("manual_"):
        return JSONResponse({"error": "手動登録の顧客にはLINE送信できません"}, status_code=400)
    
    try:
        line_handler.send_text(data.user_id, data.message)
        return {"status": "ok", "message": "メッセージを送信しました"}
    except Exception as e:
        logger.error(f"Error sending direct message: {e}")
        return JSONResponse({"error": "送信に失敗しました"}, status_code=500)

@app.get("/api/menus")
async def api_get_menus():
    """全メニュー"""
    rows = db.get_all_menus()
    return {"menus": [dict(r) for r in rows]}

@app.post("/api/bookings")
async def api_create_booking(data: DashboardBookingCreate):
    """新規予約（管理画面からの手動登録）"""
    if not db.is_slot_available(data.booking_date, data.booking_time):
        return JSONResponse({"error": "その時間帯はすでに予約が入っています"}, status_code=409)

    user_id = data.existing_user_id
    if not user_id:
        user_id = f"manual_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"
        db.save_customer_profile(user_id, data.customer_name, phone=data.phone)
    booking_id = db.add_booking(user_id, data.booking_date, data.booking_time, data.menu_id, data.notes)
    if booking_id:
        db.add_booking_history(
            booking_id=booking_id, action="created", user_id=user_id,
            after_date=data.booking_date, after_time=data.booking_time,
            note="管理画面から手動登録"
        )
        return {"id": booking_id, "status": "ok"}
    return JSONResponse({"error": "failed"}, status_code=500)

@app.put("/api/bookings/{booking_id}")

class CompleteBookingRequest(BaseModel):
    notes: Optional[str] = None

@app.post("/api/bookings/{booking_id}/complete")
async def api_complete_booking(booking_id: int, data: CompleteBookingRequest):
    """予約を「来店完了」にし、来店履歴（カルテ）に記録する"""
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "予約が見つかりません"}, status_code=404)
    
    # 予約ステータスを completed に更新
    db.update_booking_status(booking_id, "completed")
    
    # 来店履歴（カルテ）を登録
    db.add_visit_history(
        user_id=booking['user_id'],
        booking_id=booking_id,
        visited_date=booking['booking_date'],
        notes=data.notes
    )
    
    return {"status": "ok", "message": "来店完了を記録しました"}

async def api_update_booking(booking_id: int, data: DashboardBookingCreate):
    """予約更新"""
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not db.is_slot_available(data.booking_date, data.booking_time, exclude_booking_id=booking_id):
        return JSONResponse({"error": "その時間帯はすでに予約が入っています"}, status_code=409)
    original = dict(booking)
    db.update_booking(booking_id, data.booking_date, data.booking_time, data.menu_id)
    db.save_customer_profile(booking["user_id"], data.customer_name, phone=data.phone)
    db.add_booking_history(
        booking_id=booking_id, action="modified", user_id=booking["user_id"],
        before_date=original.get("booking_date"), before_time=original.get("booking_time"),
        after_date=data.booking_date, after_time=data.booking_time,
        note="管理画面から変更"
    )
    return {"status": "ok"}

@app.delete("/api/bookings/{booking_id}")
async def api_delete_booking(booking_id: int):
    """予約キャンセル（管理画面から）"""
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.cancel_booking(booking_id)
    db.add_booking_history(
        booking_id=booking_id, action="cancelled", user_id=booking["user_id"],
        before_date=booking["booking_date"], before_time=booking["booking_time"],
        note="管理画面からキャンセル"
    )
    return {"status": "ok"}

# ===============================
# オーナーコマンド処理
# ===============================

def handle_owner_command(user_id: str, text: str):
    """オーナー向けコマンド処理"""
    
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
