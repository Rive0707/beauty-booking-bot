"""
LINE美容室予約BOT メインアプリケーション - 完全統合版
FastAPI + LINE Messaging API + SQLite + APScheduler
"""

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, PostbackEvent, FollowEvent
from pydantic import BaseModel
from typing import Optional
import os
import uuid
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

# 自作モジュール
from database import Database
from line_handler import LineHandler
from reminder import ReminderScheduler
from config import BUSINESS_HOURS_START, BUSINESS_HOURS_END, SLOT_INTERVAL_MINUTES, CLOSED_WEEKDAYS, LAST_BOOKING_BUFFER_MINUTES

# ロギング設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 環境変数から取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OWNER_USER_ID = os.getenv("OWNER_USER_ID")
LIFF_ID = os.getenv("LIFF_ID")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")

security = HTTPBasic()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    is_user = secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
    is_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (is_user and is_pass):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

if not all([LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, OWNER_USER_ID]):
    raise ValueError("必須環境変数が設定されていません")

# FastAPI初期化
app = FastAPI(title="Beauty Booking Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

db = Database()
db.init_db()

line_handler = LineHandler(line_bot_api, db, owner_user_id=OWNER_USER_ID, liff_id=LIFF_ID)
reminder_scheduler = ReminderScheduler(line_bot_api, db, liff_id=LIFF_ID)

scheduler = BackgroundScheduler()
scheduler.add_job(
    reminder_scheduler.check_and_send_reminders,
    'interval',
    hours=1,
    id='reminder_job'
)
scheduler.start()
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
    menu_ids: list[int]  # ★ int から list[int] に変更
    booking_date: str
    booking_time: str
    name: str
    furigana: Optional[str] = None
    gender: Optional[str] = None
    birthdate: Optional[str] = None
    phone: Optional[str] = None

class DashboardDashboardBookingCreate(BaseModel):
    customer_name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_ids: list[int]  # ★ int から list[int] に変更
    notes: Optional[str] = None
    existing_user_id: Optional[str] = None

class RescheduleRequest(BaseModel):
    user_id: str
    booking_id: int
    booking_date: str
    booking_time: str

class ManualBookingRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_id: int
    note: Optional[str] = None
    existing_user_id: Optional[str] = None

class BookingUpdateRequest(BaseModel):
    booking_date: Optional[str] = None
    booking_time: Optional[str] = None
    menu_id: Optional[int] = None

class DashboardBookingCreate(BaseModel):
    customer_name: str
    phone: Optional[str] = None
    booking_date: str
    booking_time: str
    menu_id: int
    notes: Optional[str] = None
    existing_user_id: Optional[str] = None

class CustomerUpdateRequest(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None

class VisitNotesUpdateRequest(BaseModel):
    notes: str

class CustomerMergeRequest(BaseModel):
    manual_user_id: str
    line_user_id: str

class DirectMessageRequest(BaseModel):
    user_id: str
    message: str

class ClosedDayRequest(BaseModel):
    closed_date: str
    note: Optional[str] = None

class CompleteBookingRequest(BaseModel):
    notes: Optional[str] = None

class MonthlyReportQuery:
    def __init__(self, year: int, month: int):
        self.year = year
        self.month = month

# ===============================
# LINE Webhook & ハンドラー
# ===============================

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return JSONResponse({"status": "ok"})

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text
    if text in ["予約", "予約する"]:
        line_handler.start_booking(user_id)
    elif text in ["予約確認", "マイページ", "履歴"]:
        line_handler.show_my_page(user_id)
    elif user_id == OWNER_USER_ID:
        handle_owner_command(user_id, text)
    else:
        # メッセージ入力時は予約案内を送信
        line_handler.start_booking(user_id)

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    postback_data = event.postback.data
    params = dict(p.split("=", 1) for p in postback_data.split("&") if "=" in p)
    action = params.get("action")
    
    if action == "cancel_booking":
        line_handler.cancel_booking(user_id, params.get("booking_id"))

@handler.add(FollowEvent)
def handle_follow(event):
    line_handler.on_user_follow(event.source.user_id)

# ===============================
# Web管理画面 HTML生成
# ===============================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=get_dashboard_html())

def get_dashboard_html():
    return """<!DOCTYPE html>
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
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
  .title { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 14px; border-radius: 10px; border: 1px solid #d1d1d6; background: #fff; color: #1d1d1f; font-size: 14px; font-weight: 500; cursor: pointer; text-decoration: none; }
  .btn-primary { background: #1d1d1f; color: #fff; border-color: #1d1d1f; }
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid #d1d1d6; margin-bottom: 20px; overflow-x: auto; }
  .tab { padding: 10px 16px; font-size: 14px; font-weight: 500; color: #8e8e93; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; border: none; white-space: nowrap; }
  .tab.active { color: #1d1d1f; border-bottom-color: #1d1d1f; }
  .panel { display: none; }
  .panel.active { display: block; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .stat-card { background: #fff; border-radius: 12px; padding: 16px; border: 1px solid #e5e5ea; }
  .stat-label { font-size: 12px; color: #8e8e93; margin-bottom: 4px; }
  .stat-value { font-size: 28px; font-weight: 600; }

  .date-nav { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .date-nav input[type="date"] { padding: 6px 10px; border-radius: 8px; border: 1px solid #d1d1d6; font-size: 14px; }
  .date-label { font-size: 15px; font-weight: 500; }

  .timeline { display: grid; grid-template-columns: 56px 1fr; border: 1px solid #d1d1d6; border-radius: 12px; overflow: hidden; background: #fff; }
  .time-slot { display: contents; }
  .time-label { padding: 12px 8px; font-size: 12px; color: #8e8e93; text-align: right; border-right: 1px solid #e5e5ea; border-bottom: 1px solid #e5e5ea; background: #fafafa; font-variant-numeric: tabular-nums; }
  .time-content { padding: 8px; border-bottom: 1px solid #e5e5ea; min-height: 48px; position: relative; transition: background 0.15s; }
  .time-slot:last-child .time-label, .time-slot:last-child .time-content { border-bottom: none; }
  
  .booking-card { background: #e8f4fd; border-left: 3px solid #007aff; border-radius: 8px; padding: 8px 10px; margin-bottom: 4px; cursor: grab; position: relative; }
  .booking-card.tentative { border-left-color: #ff9500; background: #fff4e5; }
  .booking-card.cancelled { border-left-color: #ff3b30; background: #ffe5e5; opacity: 0.7; cursor: default; }
  .booking-card.completed { border-left-color: #8e8e93; background: #f2f2f7; opacity: 0.8; cursor: default; }
  .booking-name { font-weight: 500; font-size: 13px; }
  .booking-meta { font-size: 12px; color: #8e8e93; margin-top: 2px; display: flex; gap: 8px; flex-wrap: wrap; }
  .booking-tag { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #fff; color: #8e8e93; }
  .booking-actions { position: absolute; top: 6px; right: 6px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.15s; }
  .booking-card:hover .booking-actions { opacity: 1; }
  .icon-btn { width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center; border-radius: 6px; border: none; background: #fff; color: #8e8e93; cursor: pointer; padding: 0; }
  .icon-btn:hover { background: #f2f2f7; color: #1d1d1f; }
  .icon-btn.danger:hover { color: #ff3b30; }

  .toolbar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .search-box { position: relative; flex: 1; min-width: 200px; }
  .search-box input { width: 100%; padding: 8px 10px 8px 12px; border-radius: 10px; border: 1px solid #d1d1d6; font-size: 14px; background: #fff; }
  .filter-select { padding: 8px 10px; border-radius: 10px; border: 1px solid #d1d1d6; font-size: 14px; background: #fff; min-width: 120px; }
  
  .data-table { width: 100%; border-collapse: collapse; font-size: 14px; background: #fff; border-radius: 12px; overflow: hidden; }
  .data-table th { text-align: left; padding: 10px 12px; font-weight: 500; color: #8e8e93; border-bottom: 1px solid #e5e5ea; font-size: 12px; background: #fafafa; }
  .data-table td { padding: 10px 12px; border-bottom: 1px solid #e5e5ea; vertical-align: middle; }
  .data-table tbody tr:hover { background: #f5f5f7; }
  
  .status-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 6px; font-size: 12px; font-weight: 500; }
  .status-confirmed { background: #e8f5e9; color: #2e7d32; }
  .status-cancelled { background: #ffebee; color: #c62828; }
  .row-actions { display: flex; gap: 4px; opacity: 0; transition: opacity 0.15s; }
  .data-table tbody tr:hover .row-actions { opacity: 1; }

  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.35); display: none; align-items: center; justify-content: center; z-index: 100; padding: 16px; }
  .modal-overlay.open { display: flex; }
  .modal { background: #fff; border-radius: 16px; width: 100%; max-width: 480px; max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 60px rgba(0,0,0,0.15); }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid #e5e5ea; }
  .modal-title { font-size: 17px; font-weight: 600; }
  .modal-body { padding: 20px; }
  .form-group { margin-bottom: 16px; }
  .form-group label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: #8e8e93; }
  .form-group label .req { color: #ff3b30; }
  .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #d1d1d6; font-size: 14px; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .modal-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 20px 16px; }

  .empty { text-align: center; padding: 40px 20px; color: #c7c7cc; font-size: 14px; }
  .toast-container { position: fixed; bottom: 20px; right: 20px; z-index: 200; display: flex; flex-direction: column; gap: 8px; }
  .toast { background: #1d1d1f; color: #fff; padding: 10px 16px; border-radius: 10px; font-size: 13px; font-weight: 500; box-shadow: 0 4px 16px rgba(0,0,0,0.12); }
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
    <div class="stat-card"><div class="stat-label">本日の予約</div><div class="stat-value" id="stat-today">0</div></div>
    <div class="stat-card"><div class="stat-label">今後の予約</div><div class="stat-value" id="stat-upcoming">0</div></div>
    <div class="stat-card"><div class="stat-label">登録メニュー</div><div class="stat-value" id="stat-menus">0</div></div>
    <div class="stat-card"><div class="stat-label">登録顧客</div><div class="stat-value" id="stat-customers">0</div></div>
  </div>

  <div class="tabs">
    <button class="tab active" onclick="switchTab('board')" id="tab-board">予約ボード</button>
    <button class="tab" onclick="switchTab('list')" id="tab-list">予約一覧</button>
    <button class="tab" onclick="switchTab('customers')" id="tab-customers">顧客管理</button>
    <button class="tab" onclick="switchTab('holidays')" id="tab-holidays">臨時休業日</button>
    <button class="tab" onclick="switchTab('menus')" id="tab-menus">メニュー管理</button>
    <button class="tab" onclick="switchTab('reports')" id="tab-reports">レポート</button>
    <button class="tab" onclick="switchTab('history')" id="tab-history">履歴</button>
  </div>

  <!-- 予約ボード（タイムライン） -->
  <div class="panel active" id="panel-board">
    <div class="date-nav">
      <button class="btn" onclick="changeDate(-1)">‹</button>
      <input type="date" id="board-date" onchange="renderBoard()">
      <button class="btn" onclick="changeDate(1)">›</button>
      <span class="date-label" id="board-date-label"></span>
    </div>
    <div class="timeline" id="timeline"></div>
  </div>

  <!-- 予約一覧 -->
  <div class="panel" id="panel-list">
    <div class="toolbar">
      <div class="search-box">
        <input type="text" id="list-search" placeholder="顧客名・電話番号で検索" oninput="renderList()">
      </div>
      <select class="filter-select" id="list-status" onchange="renderList()">
        <option value="">すべて</option>
        <option value="confirmed">確定</option>
        <option value="completed">来店済み</option>
        <option value="cancelled">キャンセル</option>
      </select>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>日時</th><th>顧客</th><th>メニュー</th><th>ステータス</th><th style="width:100px;">操作</th></tr>
      </thead>
      <tbody id="list-body"></tbody>
    </table>
    <div id="list-empty" class="empty" style="display:none;">予約がありません</div>
  </div>

  <!-- 履歴 -->
  <div class="panel" id="panel-history">
    <div id="history-list"></div>
    <div id="history-empty" class="empty" style="display:none;">履歴がありません</div>
  </div>

  <!-- 顧客管理 -->
  <div class="panel" id="panel-customers">
    <div class="toolbar">
      <div class="search-box">
        <input type="text" id="customer-search" placeholder="名前・電話番号で検索" oninput="renderCustomers()">
      </div>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>種別</th><th>お名前</th><th>電話番号</th><th>最終来店</th><th style="width:160px;">操作</th></tr>
      </thead>
      <tbody id="customer-body"></tbody>
    </table>
    <div id="customer-empty" class="empty" style="display:none;">お客様が登録されていません</div>
  </div>

  <!-- 臨時休業日管理 -->
  <div class="panel" id="panel-holidays">
    <div style="display:grid; grid-template-columns: 280px 1fr; gap: 20px;">
      <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px;">
        <div style="font-weight:600; margin-bottom:12px;">臨時休業日の追加</div>
        <div class="form-group">
          <label>日付 <span class="req">*</span></label>
          <input type="date" id="holiday-date">
        </div>
        <div class="form-group">
          <label>メモ</label>
          <input type="text" id="holiday-note" placeholder="社員研修、臨時休業など">
        </div>
        <button class="btn btn-primary" onclick="addHoliday()" style="width:100%; justify-content:center;">休業日を設定</button>
      </div>
      <div>
        <table class="data-table">
          <thead><tr><th>休業日</th><th>メモ</th><th style="width:60px;"></th></tr></thead>
          <tbody id="holiday-body"></tbody>
        </table>
        <div id="holiday-empty" class="empty" style="display:none;">休業日は設定されていません</div>
      </div>
    </div>
  </div>

  <!-- メニュー管理 -->
  <div class="panel" id="panel-menus">
    <div style="display:grid; grid-template-columns: 280px 1fr; gap: 20px;">
      <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px;">
        <div style="font-weight:600; margin-bottom:12px;">新規メニュー追加</div>
        <div class="form-group"><label>メニュー名 <span class="req">*</span></label><input type="text" id="menu-name" placeholder="カット"></div>
        <div class="form-group"><label>料金（B） <span class="req">*</span></label><input type="number" id="menu-price" placeholder="500"></div>
        <div class="form-group"><label>時間（分） <span class="req">*</span></label><input type="number" id="menu-duration" placeholder="60"></div>
        <button class="btn btn-primary" onclick="addMenu()" style="width:100%; justify-content:center;">追加する</button>
      </div>
      <div>
        <table class="data-table">
          <thead><tr><th>メニュー名</th><th>料金</th><th>時間</th><th style="width:60px;"></th></tr></thead>
          <tbody id="menu-body"></tbody>
        </table>
        <div id="menu-empty" class="empty" style="display:none;">メニューが登録されていません</div>
      </div>
    </div>
  </div>
</div>

  <div class="panel" id="panel-reports">
    <div class="toolbar" style="align-items:center;">
      <input type="month" id="report-month" class="filter-select" style="width:160px;" onchange="renderReport()">
      <button class="btn btn-primary" onclick="renderReport()">表示</button>
    </div>
    
    <div class="stats" style="margin-top:16px;">
      <div class="stat-card">
        <div class="stat-label">月間売上</div>
        <div class="stat-value" id="r-revenue">¥0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">予約総数</div>
        <div class="stat-value" id="r-total">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">キャンセル率</div>
        <div class="stat-value" id="r-cancel">0%</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">平均単価</div>
        <div class="stat-value" id="r-avg">¥0</div>
      </div>
    </div>

    <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 20px;">
      <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px;">
        <div style="font-weight:600; margin-bottom:12px; font-size:14px;">🏆 メニュー別売上</div>
        <table class="data-table" style="font-size:13px;">
          <thead><tr><th>メニュー</th><th style="text-align:right;">件数</th><th style="text-align:right;">売上</th></tr></thead>
          <tbody id="report-menu-body"></tbody>
        </table>
      </div>
      
      <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px;">
        <div style="font-weight:600; margin-bottom:12px; font-size:14px;">👑 顧客ランキング</div>
        <table class="data-table" style="font-size:13px;">
          <thead><tr><th>お客様</th><th style="text-align:right;">来店</th><th style="text-align:right;">総額</th></tr></thead>
          <tbody id="report-customer-body"></tbody>
        </table>
      </div>
    </div>

    <div style="background:#fff; border-radius:12px; border:1px solid #e5e5ea; padding:16px; margin-top:16px;">
      <div style="font-weight:600; margin-bottom:12px; font-size:14px;">⏰ 時間帯別予約分布</div>
      <div id="report-time-chart" style="display:flex; align-items:flex-end; gap:4px; height:120px; padding-top:10px;"></div>
    </div>
  </div>

<!-- 新規・編集モーダル -->
<div class="modal-overlay" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">予約を登録</span>
      <button class="icon-btn" onclick="closeModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group">
        <label>既存のお客様から選ぶ</label>
        <input type="text" id="form-customer-input" list="customer-list" placeholder="名前または電話番号を入力..." oninput="onCustomerInput()">
        <datalist id="customer-list"></datalist>
        <div id="customer-info-badge" style="display:none; margin-top:6px; padding:6px 10px; background:#e8f4fd; border-radius:6px; font-size:12px; color:#007aff; font-weight:500;"></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>お客様名 <span class="req">*</span></label><input type="text" id="form-name" placeholder="山田 花子"></div>
        <div class="form-group"><label>電話番号</label><input type="tel" id="form-phone" placeholder="090-1234-5678" oninput="formatPhone(this)"></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>日付 <span class="req">*</span></label><input type="date" id="form-date"></div>
        <div class="form-group"><label>時間 <span class="req">*</span></label><select id="form-time"><option value="">選択</option></select></div>
      </div>
      <div class="form-group"><label>メニュー <span class="req">*</span></label><select id="form-menu"><option value="">選択</option></select></div>
      <div class="form-group"><label>メモ</label><textarea id="form-memo" placeholder="要望・注意事項など"></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitBooking()">登録する</button>
    </div>
  </div>
</div>

<!-- 来店完了・カルテ記録モーダル -->
<div class="modal-overlay" id="modal-complete-booking" onclick="if(event.target===this)closeCompleteBookingModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">来店完了・カルテ記録</span>
      <button class="icon-btn" onclick="closeCompleteBookingModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group"><label>お客さま</label><input type="text" id="complete-customer-name" readonly style="background:#f2f2f7;"></div>
      <div class="form-group"><label>施術内容・カルテメモ</label><textarea id="complete-notes" rows="4" placeholder="例: アッシュ8トーン、オキシ6%、サイド少し長め"></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeCompleteBookingModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitCompleteBooking()">来店完了として保存</button>
    </div>
  </div>
</div>

<!-- 来店履歴・カルテ閲覧モーダル -->
<div class="modal-overlay" id="modal-customer-history" onclick="if(event.target===this)closeCustomerHistoryModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="history-modal-title">来店履歴・カルテ</span>
      <button class="icon-btn" onclick="closeCustomerHistoryModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body" id="customer-history-body" style="max-height:60vh; overflow-y:auto;"></div>
    <div class="modal-footer"><button class="btn" onclick="closeCustomerHistoryModal()">閉じる</button></div>
  </div>
</div>

<!-- 顧客編集モーダル -->
<div class="modal-overlay" id="modal-customer-edit" onclick="if(event.target===this)closeCustomerEditModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">お客様情報の編集</span>
      <button class="icon-btn" onclick="closeCustomerEditModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group"><label>お名前 <span class="req">*</span></label><input type="text" id="edit-customer-name"></div>
      <div class="form-group"><label>電話番号</label><input type="tel" id="edit-customer-phone" oninput="formatPhone(this)"></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeCustomerEditModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="saveCustomerEdit()">保存する</button>
    </div>
  </div>
</div>

<!-- LINE連携（ガッチャンコ）モーダル -->
<div class="modal-overlay" id="modal-customer-merge" onclick="if(event.target===this)closeCustomerMergeModal()">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title">LINEアカウントとの連携</span>
      <button class="icon-btn" onclick="closeCustomerMergeModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group"><label>手動登録のお客さま</label><input type="text" id="merge-manual-name" readonly style="background:#f2f2f7;"></div>
      <div class="form-group"><label>紐付けるLINEアカウントを選択 <span class="req">*</span></label><select id="merge-line-user-select"><option value="">選択してください</option></select></div>
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
      <span class="modal-title">LINEメッセージ送信</span>
      <button class="icon-btn" onclick="closeSendMessageModal()" style="width:32px;height:32px;">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-group"><label>送信先のお客さま</label><input type="text" id="message-target-name" readonly style="background:#f2f2f7;"></div>
      <div class="form-group"><label>メッセージ内容 <span class="req">*</span></label><textarea id="message-text" rows="5" placeholder="例: 明日のご予約確認メッセージです。"></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeSendMessageModal()">キャンセル</button>
      <button class="btn btn-primary" onclick="submitDirectMessage()">送信する</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toasts"></div>

<script>
  var bookings = [];
  var histories = [];
  var customers = [];
  var menus = [];
  var holidays = [];
  var currentDate = new Date();
  var editingId = null;
  var selectedUserId = null;
  var times = [];
  for (var h = 9; h < 19; h++) { 
  var hh = (h < 10 ? "0" + h : h);
  times.push(hh + ":00"); 
  times.push(hh + ":30"); 
}
  times.push("19:00");

  function fmtDate(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth()+1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }
  function fmtDateJp(d) {
    var dt = new Date(d + "T00:00:00");
    return (dt.getMonth()+1) + "月" + dt.getDate() + "日 (" + ["日","月","火","水","木","金","土"][dt.getDay()] + ")";
  }
  function formatPhone(el) {
    var n = el.value.replace(/\\D/g,"");
    if (n.length === 11) el.value = n.replace(/(\\d{3})(\\d{4})(\\d{4})/,"$1-$2-$3");
    else if (n.length === 10) el.value = n.replace(/(\\d{3})(\\d{3})(\\d{4})/,"$1-$2-$3");
  }
  function maskPhone(p) {
    if (!p || p.length < 8) return p;
    return p.replace(/(\\d{3})-(\\d{4})-(\\d{4})/,"$1-****-$3");
  }
  function toast(msg) {
    var c = document.getElementById("toasts");
    var el = document.createElement("div"); el.className = "toast"; el.textContent = msg;
    c.appendChild(el); setTimeout(function(){ el.remove(); }, 3000);
  }
  function escapeHtml(str) {
    if (!str) return "";
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  document.getElementById("board-date").value = fmtDate(currentDate);
  document.getElementById("form-date").value = fmtDate(currentDate);
  var timeSel = document.getElementById("form-time");
  times.forEach(function(t) { var o = document.createElement("option"); o.value = t; o.textContent = t; timeSel.appendChild(o); });

  function populateMenus() {
    var sel = document.getElementById("form-menu");
    sel.innerHTML = '<option value="">選択</option>';
    menus.forEach(function(m) { var o = document.createElement("option"); o.value = m.id; o.textContent = m.name + " (฿" + m.price.toLocaleString() + ", " + m.duration_minutes + "分)"; sel.appendChild(o); });
  }

  function populateCustomers() {
    var list = document.getElementById("customer-list"); list.innerHTML = "";
    customers.forEach(function(c) {
      var opt = document.createElement("option");
      var lastVisit = c.last_visit ? "最終来店: " + c.last_visit.replace(/-/g, "/") : "来店履歴なし";
      opt.value = (c.name || "(名前なし)") + " (" + (c.phone ? maskPhone(c.phone) : "電話なし") + ") [" + lastVisit + "]";
      list.appendChild(opt);
    });
  }

  function onCustomerInput() {
    var val = document.getElementById("form-customer-input").value;
    var badge = document.getElementById("customer-info-badge");
    var matched = customers.find(function(c) {
      var lastVisit = c.last_visit ? "最終来店: " + c.last_visit.replace(/-/g, "/") : "来店履歴なし";
      var label = (c.name || "(名前なし)") + " (" + (c.phone ? maskPhone(c.phone) : "電話なし") + ") [" + lastVisit + "]";
      return label === val || c.name === val || c.phone === val;
    });

    if (matched) {
      selectedUserId = matched.user_id;
      document.getElementById("form-name").value = matched.name || "";
      document.getElementById("form-phone").value = matched.phone || "";
      badge.style.display = "block";
      badge.innerHTML = "👤 <b>" + matched.name + "</b> 様 | TEL: " + (matched.phone || "未登録") + " | 最終来店: <b>" + (matched.last_visit ? matched.last_visit.replace(/-/g, "/") : "なし") + "</b>";
    } else {
      selectedUserId = null;
      badge.style.display = "none";
      if (val && !val.includes("(")) document.getElementById("form-name").value = val;
    }
  }

  function updateStats() {
    var today = fmtDate(new Date());
    document.getElementById("stat-today").textContent = bookings.filter(function(b){ return b.booking_date === today && b.status !== "cancelled"; }).length;
    document.getElementById("stat-upcoming").textContent = bookings.filter(function(b){ return b.booking_date >= today && b.status === "confirmed"; }).length;
    document.getElementById("stat-menus").textContent = menus.length;
    document.getElementById("stat-customers").textContent = customers.length;
  }

  function switchTab(name) {
    document.querySelectorAll(".tab").forEach(function(t){ t.classList.remove("active"); });
    document.querySelectorAll(".panel").forEach(function(p){ p.classList.remove("active"); });
    document.getElementById("tab-" + name).classList.add("active");
    document.getElementById("panel-" + name).classList.add("active");
  }

  function changeDate(delta) {
    currentDate.setDate(currentDate.getDate() + delta);
    document.getElementById("board-date").value = fmtDate(currentDate);
    renderBoard();
  }

  /* --- ① ドラッグ＆ドロップ ＋ ② 時間指定ダイレクト登録 ＋ ⑤ ボード上来店処理 --- */
  function renderBoard() {
    var date = document.getElementById("board-date").value;
    currentDate = new Date(date + "T00:00:00");
    document.getElementById("board-date-label").textContent = fmtDateJp(date);
    var container = document.getElementById("timeline"); container.innerHTML = "";
    var dayBookings = bookings.filter(function(b){ return b.booking_date === date; }).sort(function(a,b){ return a.booking_time.localeCompare(b.booking_time); });

    times.forEach(function(time) {
      var slot = document.createElement("div"); slot.className = "time-slot";
      var label = document.createElement("div"); label.className = "time-label"; label.textContent = time; label.style.cursor = "pointer";
      label.onclick = function() { openModalWithTime(time); };

      var content = document.createElement("div"); content.className = "time-content"; content.style.cursor = "pointer";
      content.onclick = function(e) { if (e.target === content) openModalWithTime(time); };

      // ドラッグ＆ドロップ受け入れイベント
      content.ondragover = function(e) { e.preventDefault(); content.style.background = "#e8f4fd"; };
      content.ondragleave = function() { content.style.background = ""; };
      content.ondrop = async function(e) {
        e.preventDefault(); content.style.background = "";
        var bookingId = e.dataTransfer.getData("text/plain");
        if (bookingId) handleCardDrop(parseInt(bookingId), date, time);
      };

      var bs = dayBookings.filter(function(b){ return b.booking_time === time; });
      bs.forEach(function(b) {
        var menu = menus.find(function(m){ return m.id === b.menu_id; });
        var c = customers.find(function(x){ return x.user_id === b.user_id; });
        var card = document.createElement("div");
        var statusClass = b.status === "completed" ? "completed" : (b.status || "confirmed");
        card.className = "booking-card " + statusClass;

        // ドラッグ可能設定
        if (b.status !== "completed" && b.status !== "cancelled") {
          card.draggable = true;
          card.ondragstart = function(e) { e.dataTransfer.setData("text/plain", b.id); card.style.opacity = "0.5"; };
          card.ondragend = function() { card.style.opacity = "1"; };
        }

        var completeBtn = (b.status !== "completed" && b.status !== "cancelled")
          ? '<button class="icon-btn" title="来店完了" onclick="event.stopPropagation(); openCompleteBookingModal(' + b.id + ')">✅</button>' : "";

        card.innerHTML = '<div class="booking-name">' + (c ? c.name : b.user_id) + (b.status === "completed" ? ' <span style="font-size:11px;color:#8e8e93;">(来店済)</span>' : "") + '</div>' +
          '<div class="booking-meta"><span class="booking-tag">' + (menu ? menu.name : "不明") + '</span>' + (b.notes ? '<span class="booking-tag">' + b.notes + '</span>' : '') + '</div>' +
          '<div class="booking-actions">' + completeBtn +
            '<button class="icon-btn" title="編集" onclick="event.stopPropagation(); editBooking(' + b.id + ')">✏️</button>' +
            '<button class="icon-btn danger" title="削除" onclick="event.stopPropagation(); deleteBooking(' + b.id + ')">🗑</button>' +
          '</div>';
        content.appendChild(card);
      });
      slot.appendChild(label); slot.appendChild(content); container.appendChild(slot);
    });
  }

  async function handleCardDrop(bookingId, targetDate, targetTime) {
    var b = bookings.find(function(x){ return x.id === bookingId; });
    if (!b || b.booking_time === targetTime) return;
    if (!confirm((b.customer_name || "お客様") + " 様の予約時間を " + targetTime + " に変更しますか？")) return;

    var res = await fetch("/api/bookings/" + bookingId, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ booking_date: targetDate, booking_time: targetTime, menu_id: b.menu_id })
    });
    if (res.ok) { toast("時間を " + targetTime + " に変更しました"); loadData(); }
    else { toast("時間の変更に失敗しました"); }
  }

  function openModalWithTime(timeStr) {
    editingId = null;
    document.getElementById("modal-title").textContent = timeStr + " の予約を登録";
    clearForm();
    document.getElementById("form-date").value = document.getElementById("board-date").value;
    document.getElementById("form-time").value = timeStr;
    document.getElementById("modal").classList.add("open");
  }

  /* --- 予約一覧描画 --- */
  function renderList() {
    var search = document.getElementById("list-search").value.toLowerCase();
    var status = document.getElementById("list-status").value;
    var data = bookings.filter(function(b) {
      if (status && b.status !== status) return false;
      if (!search) return true;
      var c = customers.find(function(x){ return x.user_id === b.user_id; });
      return (c && c.name && c.name.toLowerCase().includes(search)) || (c && c.phone && c.phone.includes(search));
    });
    var tbody = document.getElementById("list-body"); tbody.innerHTML = "";
    data.forEach(function(b) {
      var c = customers.find(function(x){ return x.user_id === b.user_id; });
      var m = menus.find(function(x){ return x.id === b.menu_id; });
      var tr = document.createElement("tr");
      var statusBadge = b.status === "completed" ? '<span class="status-badge" style="background:#e5e5ea;color:#3a3a3c;">来店済み</span>'
        : (b.status === "cancelled" ? '<span class="status-badge status-cancelled">キャンセル</span>' : '<span class="status-badge status-confirmed">確定</span>');
      var completeBtn = (b.status !== "completed" && b.status !== "cancelled")
        ? '<button class="icon-btn" title="来店完了" onclick="openCompleteBookingModal(' + b.id + ')">✅</button>' : '';

      tr.innerHTML = "<td>" + b.booking_date.replace(/-/g,"/") + " " + b.booking_time + "</td>" +
        "<td style='font-weight:500'>" + (c ? c.name : b.user_id) + "</td>" +
        "<td>" + (m ? m.name : "不明") + "</td>" +
        "<td>" + statusBadge + "</td>" +
        "<td><div class='row-actions' style='opacity:1;'>" + completeBtn + "<button class='icon-btn' onclick='editBooking(" + b.id + ")'>✏️</button><button class='icon-btn danger' onclick='deleteBooking(" + b.id + ")'>🗑</button></div></td>";
      tbody.appendChild(tr);
    });
    document.getElementById("list-empty").style.display = data.length ? "none" : "block";
  }

  /* --- ③ 顧客情報の編集・安全削除 --- */
  function renderCustomers() {
    var q = (document.getElementById("customer-search").value || "").toLowerCase();
    var filtered = customers.filter(function(c) {
      return (c.name || "").toLowerCase().includes(q) || (c.phone || "").toLowerCase().includes(q);
    });
    var body = document.getElementById("customer-body"); body.innerHTML = "";
    filtered.forEach(function(c) {
      var id = c.user_id || "";
      var isLine = !id.startsWith("manual_");
      var tr = document.createElement("tr");
      tr.innerHTML = "<td>" + (isLine ? "<span class='status-badge status-confirmed'>LINE</span>" : "<span class='status-badge' style='background:#e5e5ea;'>手動</span>") + "</td>" +
        "<td style='font-weight:500'>" + (c.name || "(名前未登録)") + "</td>" +
        "<td>" + (c.phone || "-") + "</td>" +
        "<td>" + (c.last_visit ? String(c.last_visit).replace(/-/g, "/") : "-") + "</td>" +
        "<td><div class='row-actions' style='opacity:1;'>" +
          (isLine ? "<button class='icon-btn' title='LINE送信' onclick='openSendMessageModal(\\"" + id + "\\")'>✉️</button>" : "<button class='icon-btn' title='LINE連携' onclick='openCustomerMergeModal(\\"" + id + "\\")'>🔗</button>") +
          "<button class='icon-btn' title='来店履歴・カルテ' onclick='showCustomerHistory(\\"" + id + "\\")'>📋</button>" +
          "<button class='icon-btn' title='編集' onclick='openCustomerEditModal(\\"" + id + "\\")'>✏️</button>" +
          "<button class='icon-btn danger' title='削除' onclick='deleteCustomer(\\"" + id + "\\")'>🗑️</button>" +
        "</div></td>";
      body.appendChild(tr);
    });
    document.getElementById("customer-empty").style.display = filtered.length ? "none" : "block";
  }

  var editingCustomerUserId = null;
  function openCustomerEditModal(userId) {
    var c = customers.find(function(x){ return x.user_id === userId; }); if (!c) return;
    editingCustomerUserId = userId;
    document.getElementById("edit-customer-name").value = c.name || "";
    document.getElementById("edit-customer-phone").value = c.phone || "";
    document.getElementById("modal-customer-edit").classList.add("open");
  }
  function closeCustomerEditModal() { document.getElementById("modal-customer-edit").classList.remove("open"); editingCustomerUserId = null; }
  async function saveCustomerEdit() {
    if (!editingCustomerUserId) return;
    var name = document.getElementById("edit-customer-name").value.trim();
    var phone = document.getElementById("edit-customer-phone").value.trim();
    if (!name) { toast("お名前を入力してください"); return; }
    var res = await fetch("/api/customers/" + encodeURIComponent(editingCustomerUserId), { method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ name: name, phone: phone }) });
    if (res.ok) { toast("お客様情報を更新しました"); closeCustomerEditModal(); loadData(); } else { toast("更新に失敗しました"); }
  }

  async function deleteCustomer(userId) {
    if (!confirm("このお客様を削除しますか？")) return;
    var res = await fetch("/api/customers/" + encodeURIComponent(userId), { method: "DELETE" });
    var data = await res.json();
    if (res.ok) { toast("お客様を削除しました"); loadData(); } else { toast(data.error || "削除に失敗しました"); }
  }

  /* --- ④ カルテ（過去履歴・メモ）の表示 --- */
  var currentHistoryUserId = null;
  var visitNotesMap = {};

  async function showCustomerHistory(userId) {
    currentHistoryUserId = userId;
    var c = customers.find(function(x){ return x.user_id === userId; }); if (!c) return;
    document.getElementById("history-modal-title").textContent = (c.name || "お客様") + " 様の来店履歴・カルテ";
    var container = document.getElementById("customer-history-body"); container.innerHTML = '<div class="empty">読み込み中…</div>';

    try {
      var results = await Promise.all([
        bookings.filter(function(b){ return b.user_id === userId; }).sort(function(a,b){ return (b.booking_date + b.booking_time).localeCompare(a.booking_date + a.booking_time); }),
        fetch("/api/customers/" + encodeURIComponent(userId) + "/visits").then(function(r){ return r.json(); }).catch(function(){ return []; })
      ]);
      var userBookings = results[0]; var visitRes = results[1];

      if (userBookings.length === 0 && visitRes.length === 0) {
        container.innerHTML = '<div class="empty">履歴・カルテ情報がありません</div>';
        document.getElementById("modal-customer-history").classList.add("open");
        return;
      }

      container.innerHTML = "";
      visitNotesMap = {};
      userBookings.forEach(function(b) {
        var m = menus.find(function(x){ return x.id === b.menu_id; });
        var statusLabel = (b.status === "completed") ? "来店済み" : ((b.status === "confirmed") ? "確定" : ((b.status === "cancelled") ? "キャンセル" : "仮"));
        var v = visitRes.find(function(x){ return x.booking_id === b.id; });
        var karteMemo = (v && v.notes) ? v.notes : (b.notes || "");
        var formattedMemo = karteMemo ? escapeHtml(karteMemo).split("\\n").join("<br>") : "";
        var editLink = "";
        if (v && v.id) {
          visitNotesMap[v.id] = karteMemo;
          editLink = " <a href='javascript:void(0)' onclick='editKarteMemo(" + v.id + ")' style='color:#007aff;'>✏️編集</a>";
        }

        var item = document.createElement("div"); item.style.cssText = "border-bottom:1px solid #e5e5ea; padding:12px 0;";
        item.innerHTML = "<div style='display:flex;justify-content:space-between;'><b>📅 " + b.booking_date.replace(/-/g,"/") + " " + b.booking_time + "</b><span class='status-badge'>" + statusLabel + "</span></div>" +
          "<div style='font-size:13px;margin-top:4px;'>✂️ メニュー: " + (m ? m.name : "不明") + "</div>" +
          (formattedMemo ? "<div style='font-size:13px;background:#fafafa;border-left:3px solid #007aff;padding:6px 10px;margin-top:6px;'>📝 <b>カルテメモ:</b><br>" + formattedMemo + editLink + "</div>" : "");
        container.appendChild(item);
      });
      document.getElementById("modal-customer-history").classList.add("open");
    } catch (e) {
      container.innerHTML = '<div class="empty">データの取得に失敗しました</div>';
    }
  }

  async function editKarteMemo(visitId) {
    var current = visitNotesMap[visitId] || "";
    var updated = prompt("カルテメモを編集", current);
    if (updated === null) return;
    var res = await fetch("/api/visits/" + visitId, { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ notes: updated }) });
    var data = await res.json().catch(function(){ return {}; });
    if (res.ok) { toast("カルテメモを更新しました"); if (currentHistoryUserId) showCustomerHistory(currentHistoryUserId); }
    else { toast(data.error || "更新に失敗しました"); }
  }
  function closeCustomerHistoryModal() { document.getElementById("modal-customer-history").classList.remove("open"); }

  /* --- ⑥ 臨時休業日の登録・解除 --- */
  function renderHolidays() {
    var tbody = document.getElementById("holiday-body"); if (!tbody) return;
    tbody.innerHTML = "";
    holidays.forEach(function(h) {
      var tr = document.createElement("tr");
      tr.innerHTML = "<td><b>" + (h.closed_date ? h.closed_date.replace(/-/g,"/") : "") + "</b></td>" +
        "<td>" + (h.note || "-") + "</td>" +
        "<td><button class='icon-btn danger' onclick='deleteHoliday(\\"" + h.closed_date + "\\")'>🗑️</button></td>";
      tbody.appendChild(tr);
    });
    document.getElementById("holiday-empty").style.display = holidays.length ? "none" : "block";
  }
  async function addHoliday() {
    var date = document.getElementById("holiday-date").value;
    var note = document.getElementById("holiday-note").value.trim();
    if (!date) { toast("日付を選択してください"); return; }
    var res = await fetch("/api/closed-days", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ closed_date: date, note: note }) });
    if (res.ok) { toast("休業日を設定しました"); document.getElementById("holiday-date").value = ""; document.getElementById("holiday-note").value = ""; loadData(); }
    else { toast("設定に失敗しました"); }
  }
  async function deleteHoliday(closedDate) {
    if (!confirm(closedDate + " の休業設定を解除しますか？")) return;
    var res = await fetch("/api/closed-days/" + closedDate, { method: "DELETE" });
    if (res.ok) { toast("休業設定を解除しました"); loadData(); }
  }

  /* --- その他のモーダル制御 ＆ 一括ロード ＆ ⑦ スマートポーリング --- */
  function openModal() { editingId = null; document.getElementById("modal-title").textContent = "予約を登録"; clearForm(); document.getElementById("modal").classList.add("open"); }
  function closeModal() { document.getElementById("modal").classList.remove("open"); editingId = null; }
  function clearForm() {
    selectedUserId = null;
    var inputEl = document.getElementById("form-customer-input"); if (inputEl) inputEl.value = "";
    var badgeEl = document.getElementById("customer-info-badge"); if (badgeEl) badgeEl.style.display = "none";
    document.getElementById("form-name").value = ""; document.getElementById("form-phone").value = "";
    document.getElementById("form-date").value = fmtDate(currentDate); document.getElementById("form-time").value = "";
    document.getElementById("form-menu").value = ""; document.getElementById("form-memo").value = "";
  }

  async function submitBooking() {
    var name = document.getElementById("form-name").value.trim();
    var date = document.getElementById("form-date").value;
    var time = document.getElementById("form-time").value;
    var menuId = document.getElementById("form-menu").value;
    if (!name || !date || !time || !menuId) { toast("必須項目を入力してください"); return; }

    var payload = { customer_name: name, phone: document.getElementById("form-phone").value.trim(), booking_date: date, booking_time: time, menu_id: parseInt(menuId), notes: document.getElementById("form-memo").value.trim(), existing_user_id: selectedUserId };
    var url = editingId ? "/api/bookings/" + editingId : "/api/bookings";
    var method = editingId ? "PUT" : "POST";
    var res = await fetch(url, { method: method, headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload) });
    if (res.ok) { toast(editingId ? "予約を更新しました" : "予約を登録しました"); closeModal(); loadData(); }
    else { toast("エラーが発生しました"); }
  }

  function editBooking(id) {
    var b = bookings.find(function(x){ return x.id === id; }); if (!b) return;
    editingId = id;
    var c = customers.find(function(x){ return x.user_id === b.user_id; });
    document.getElementById("modal-title").textContent = "予約を編集";
    document.getElementById("form-name").value = c ? c.name : "";
    document.getElementById("form-phone").value = c ? c.phone : "";
    document.getElementById("form-date").value = b.booking_date;
    document.getElementById("form-time").value = b.booking_time;
    document.getElementById("form-menu").value = b.menu_id;
    document.getElementById("form-memo").value = b.notes || "";
    document.getElementById("modal").classList.add("open");
  }

  async function deleteBooking(id) {
    if (!confirm("この予約をキャンセルしますか？")) return;
    var res = await fetch("/api/bookings/" + id, { method: "DELETE" });
    if (res.ok) { toast("予約をキャンセルしました"); loadData(); }
  }

  var completingBookingId = null;
  function openCompleteBookingModal(bookingId) {
    var b = bookings.find(function(x){ return x.id === bookingId; }); if (!b) return;
    completingBookingId = bookingId;
    document.getElementById("complete-customer-name").value = (b.customer_name || "お客様") + " 様 (" + b.booking_date + " " + b.booking_time + ")";
    document.getElementById("complete-notes").value = b.notes || "";
    document.getElementById("modal-complete-booking").classList.add("open");
  }
  function closeCompleteBookingModal() { document.getElementById("modal-complete-booking").classList.remove("open"); completingBookingId = null; }
  async function submitCompleteBooking() {
    if (!completingBookingId) return;
    var notes = document.getElementById("complete-notes").value.trim();
    var res = await fetch("/api/bookings/" + completingBookingId + "/complete", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ notes: notes }) });
    if (res.ok) { toast("来店完了として記録しました！"); closeCompleteBookingModal(); loadData(); } else { toast("処理に失敗しました"); }
  }

  function renderHistory() {
    var container = document.getElementById("history-list"); container.innerHTML = "";
    histories.forEach(function(h) {
      var item = document.createElement("div"); item.className = "history-item";
      item.innerHTML = "<div style='font-size:13px;'><b>" + (h.customer_name || "不明") + "</b> - " + (h.action === "created" ? "新規" : h.action === "modified" ? "変更" : "キャンセル") + " (" + h.created_at + ")</div>";
      container.appendChild(item);
    });
    document.getElementById("history-empty").style.display = histories.length ? "none" : "block";
  }

  function renderMenus() {
    var tbody = document.getElementById("menu-body"); tbody.innerHTML = "";
    menus.forEach(function(m) {
      var tr = document.createElement("tr");
      tr.innerHTML = "<td style='font-weight:500'>" + m.name + "</td><td>฿" + m.price.toLocaleString() + "</td><td>" + m.duration_minutes + "分</td><td><button class='icon-btn danger' onclick='deleteMenu(" + m.id + ")'>🗑️</button></td>";
      tbody.appendChild(tr);
    });
    document.getElementById("menu-empty").style.display = menus.length ? "none" : "block";
  }
  async function addMenu() {
    var name = document.getElementById("menu-name").value.trim();
    var price = parseInt(document.getElementById("menu-price").value);
    var duration = parseInt(document.getElementById("menu-duration").value);
    if (!name || !price || !duration) { toast("全項目を入力してください"); return; }
    var res = await fetch("/api/menus", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({name: name, price: price, duration_minutes: duration}) });
    if (res.ok) { toast("メニューを追加しました"); document.getElementById("menu-name").value = ""; document.getElementById("menu-price").value = ""; document.getElementById("menu-duration").value = ""; loadData(); }
  }
  async function deleteMenu(id) {
    if (!confirm("このメニューを削除しますか？")) return;
    var res = await fetch("/api/menus/" + id, {method: "DELETE"}); if (res.ok) loadData();
  }

  var mergingManualUserId = null;
  function openCustomerMergeModal(manualUserId) {
    var manualCustomer = customers.find(function(x){ return x.user_id === manualUserId; }); if (!manualCustomer) return;
    mergingManualUserId = manualUserId;
    document.getElementById("merge-manual-name").value = (manualCustomer.name || "名前なし") + " (" + (manualCustomer.phone || "電話なし") + ")";
    var lineUsers = customers.filter(function(x){ return !x.user_id.startsWith("manual_"); });
    var sel = document.getElementById("merge-line-user-select"); sel.innerHTML = '<option value="">選択してください</option>';
    lineUsers.forEach(function(u) {
      var opt = document.createElement("option"); opt.value = u.user_id; opt.textContent = (u.name || "LINEユーザー") + " (" + (u.phone || "TEL未登録") + ")"; sel.appendChild(opt);
    });
    document.getElementById("modal-customer-merge").classList.add("open");
  }
  function closeCustomerMergeModal() { document.getElementById("modal-customer-merge").classList.remove("open"); }
  async function submitCustomerMerge() {
    var lineUserId = document.getElementById("merge-line-user-select").value;
    if (!mergingManualUserId || !lineUserId) return;
    var res = await fetch("/api/customers/merge", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ manual_user_id: mergingManualUserId, line_user_id: lineUserId }) });
    if (res.ok) { toast("LINE連携が完了しました！"); closeCustomerMergeModal(); loadData(); }
  }

  var sendingMessageUserId = null;
  function openSendMessageModal(userId) {
    var c = customers.find(function(x){ return x.user_id === userId; }); if (!c) return;
    sendingMessageUserId = userId;
    document.getElementById("message-target-name").value = (c.name || "LINEユーザー") + " 様";
    document.getElementById("message-text").value = "";
    document.getElementById("modal-send-message").classList.add("open");
  }
  function closeSendMessageModal() { document.getElementById("modal-send-message").classList.remove("open"); }
  async function submitDirectMessage() {
    var text = document.getElementById("message-text").value.trim();
    if (!sendingMessageUserId || !text) return;
    var res = await fetch("/api/customers/send-message", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ user_id: sendingMessageUserId, message: text }) });
    if (res.ok) { toast("LINEメッセージを送信しました！"); closeSendMessageModal(); }
  }

    async function renderReport() {
    var monthInput = document.getElementById("report-month").value;
    if (!monthInput) {
      var now = new Date();
      monthInput = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
      document.getElementById("report-month").value = monthInput;
    }
    var parts = monthInput.split("-");
    var year = parseInt(parts[0]);
    var month = parseInt(parts[1]);

    try {
      var res = await fetch("/api/reports/monthly?year=" + year + "&month=" + month);
      if (!res.ok) throw new Error("failed");
      var data = await res.json();

      // サマリー
      document.getElementById("r-revenue").textContent = "¥" + (data.summary.revenue || 0).toLocaleString();
      document.getElementById("r-total").textContent = (data.summary.total_bookings || 0).toLocaleString();
      document.getElementById("r-cancel").textContent = (data.summary.cancellation_rate || 0) + "%";
      document.getElementById("r-avg").textContent = "¥" + (data.summary.avg_price || 0).toLocaleString();

      // メニュー別
      var mBody = document.getElementById("report-menu-body");
      mBody.innerHTML = "";
      if (data.menu_stats && data.menu_stats.length) {
        data.menu_stats.forEach(function(m) {
          var tr = document.createElement("tr");
          tr.innerHTML = "<td>" + escapeHtml(m.name) + "</td>" +
            "<td style='text-align:right'>" + m.count + "</td>" +
            "<td style='text-align:right'>¥" + (m.revenue || 0).toLocaleString() + "</td>";
          mBody.appendChild(tr);
        });
      } else {
        mBody.innerHTML = "<tr><td colspan='3' class='empty'>データがありません</td></tr>";
      }

      // 顧客ランキング
      var cBody = document.getElementById("report-customer-body");
      cBody.innerHTML = "";
      if (data.customer_stats && data.customer_stats.length) {
        data.customer_stats.forEach(function(c) {
          var tr = document.createElement("tr");
          tr.innerHTML = "<td>" + escapeHtml(c.name) + "</td>" +
            "<td style='text-align:right'>" + c.visits + "回</td>" +
            "<td style='text-align:right'>¥" + (c.total_spent || 0).toLocaleString() + "</td>";
          cBody.appendChild(tr);
        });
      } else {
        cBody.innerHTML = "<tr><td colspan='3' class='empty'>データがありません</td></tr>";
      }

      // 時間帯チャート（簡易バー）
      var chart = document.getElementById("report-time-chart");
      chart.innerHTML = "";
      if (data.time_stats && data.time_stats.length) {
        var maxCount = Math.max.apply(null, data.time_stats.map(function(t){ return t.count; }));
        data.time_stats.forEach(function(t) {
          var barWrap = document.createElement("div");
          barWrap.style.cssText = "flex:1; display:flex; flex-direction:column; align-items:center; gap:4px;";
          var bar = document.createElement("div");
          var h = Math.max((t.count / maxCount) * 80, 4);
          bar.style.cssText = "width:100%; background:var(--plum,#A8556B); border-radius:4px 4px 0 0; opacity:0.85; height:" + h + "px;";
          var label = document.createElement("div");
          label.textContent = t.hour + "時";
          label.style.cssText = "font-size:10px; color:#8e8e93;";
          barWrap.appendChild(bar);
          barWrap.appendChild(label);
          chart.appendChild(barWrap);
        });
      }
    } catch (e) {
      toast("レポートの取得に失敗しました");
    }
  }

  async function loadData() {
    var results = await Promise.all([
      fetch("/api/bookings/all").then(function(r){ return r.json(); }),
      fetch("/api/history").then(function(r){ return r.json(); }),
      fetch("/api/customers").then(function(r){ return r.json(); }),
      fetch("/api/menus").then(function(r){ return r.json(); }),
      fetch("/api/closed-days").then(function(r){ return r.json(); }).catch(function(){ return {closed_days:[]}; })
    ]);
    bookings = results[0]; histories = results[1]; customers = results[2]; menus = results[3].menus || [];
    holidays = (results[4] && results[4].closed_days) ? results[4].closed_days : [];
    populateMenus(); populateCustomers(); updateStats(); renderBoard(); renderList(); renderHistory(); renderMenus(); renderCustomers(); renderHolidays(); renderReport();
  }

  var lastAppliedHash = "";
  var pendingHash = null;
  async function checkAndReloadData() {
    try {
      var res = await fetch("/api/bookings/all"); if (!res.ok) return;
      var data = await res.json();
      var currentHash = JSON.stringify(data);
      if (lastAppliedHash === "") { lastAppliedHash = currentHash; return; }
      if (currentHash === lastAppliedHash) { pendingHash = null; return; }

      if (document.querySelector(".modal-overlay.open")) {
        if (pendingHash !== currentHash) { pendingHash = currentHash; toast("新しいデータがあります（入力を終えると反映されます）"); }
        return;
      }
      pendingHash = null;
      lastAppliedHash = currentHash;
      toast("最新のデータに更新されました");
      loadData();
    } catch (e) {}
  }

  document.addEventListener("visibilitychange", function() {
    if (document.visibilityState === "visible") checkAndReloadData();
  });

  loadData();
  setInterval(checkAndReloadData, 15000);
</script>
</body>
</html>
"""

# ===============================
# API エンドポイント群
# ===============================

@app.get("/api/bookings/all")
async def api_get_all_bookings():
    return [dict(r) for r in db.get_all_bookings_with_details()]

@app.get("/api/history")
async def api_get_history(limit: int = 50):
    return [dict(r) for r in db.get_booking_history(limit=limit)]

@app.get("/api/customers")
async def api_get_customers():
    return [dict(r) for r in db.get_all_customers()]

@app.get("/api/customers/{user_id}")
async def api_get_customer(user_id: str):
    """LIFF予約フォームで、既存のお客様情報を自動入力するための単体取得API"""
    c = db.get_customer(user_id)
    if not c:
        return JSONResponse({"error": "not found"}, status_code=404)
    return dict(c)

@app.get("/api/customers/{user_id}/visits")
async def api_get_customer_visits(user_id: str):
    return [dict(r) for r in db.get_visit_history(user_id, limit=20)]

@app.put("/api/visits/{visit_id}")
async def api_update_visit_notes(visit_id: int, data: VisitNotesUpdateRequest):
    if db.update_visit_history_notes(visit_id, data.notes):
        return {"status": "ok"}
    return JSONResponse({"error": "更新に失敗しました"}, status_code=500)

@app.put("/api/customers/{user_id}")
async def api_update_customer(user_id: str, data: CustomerUpdateRequest):
    if not db.get_customer(user_id):
        return JSONResponse({"error": "顧客が見つかりません"}, status_code=404)
    db.update_customer(user_id, name=data.name, phone=data.phone)
    return {"status": "ok"}

@app.delete("/api/customers/{user_id}")
async def api_delete_customer(user_id: str):
    if db.has_active_bookings(user_id):
        return JSONResponse({"error": "有効な予約が残っているため削除できません"}, status_code=400)
    if db.delete_customer(user_id):
        return {"status": "ok"}
    return JSONResponse({"error": "削除に失敗しました"}, status_code=500)

@app.post("/api/customers/merge")
async def api_merge_customer(data: CustomerMergeRequest):
    if db.merge_customers(data.manual_user_id, data.line_user_id):
        # ★ ガッチャンコ成功後、そのお客様の「今後の予約」を取得してLINEを送信する
        upcoming_bookings = db.get_bookings_by_user(data.line_user_id, status='confirmed')
        if upcoming_bookings:
            # 直近の予約情報を取得
            next_booking = upcoming_bookings[0]
            b_date = next_booking['booking_date']
            b_time = next_booking['booking_time']
            b_id = next_booking['id']
            
            # 複数メニュー名を取得
            conn = db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT GROUP_CONCAT(m.name, ' + ') as menu_names
                FROM booking_menus bm
                JOIN menus m ON bm.menu_id = m.id
                WHERE bm.booking_id = ?
            ''', (b_id,))
            row = cursor.fetchone()
            conn.close()
            
            menu_name = row['menu_names'] if (row and row['menu_names']) else 'メニュー'

            # LINEメッセージ作成＆送信
            msg = f"""✅ LINEアカウントとの連携が完了しました！

📅 次回ご予約日時:
{b_date} {b_time}

✂️ メニュー: {menu_name}

ご来店を心よりお待ちしております。"""
            line_handler.send_text(data.line_user_id, msg)

        return {"status": "ok"}
    return JSONResponse({"error": "失敗しました"}, status_code=500)

@app.post("/api/customers/send-message")
async def api_send_direct_message(data: DirectMessageRequest):
    if data.user_id.startswith("manual_"):
        return JSONResponse({"error": "LINE未連携です"}, status_code=400)
    line_handler.send_text(data.user_id, data.message)
    return {"status": "ok"}

@app.get("/api/menus")
async def api_get_menus():
    return {"menus": [dict(r) for r in db.get_all_menus()]}

@app.post("/api/menus")
async def add_menu(data: MenuAddRequest):
    menu_id = db.add_menu(data.name, data.price, data.duration_minutes)
    return {"status": "ok", "menu_id": menu_id}

@app.delete("/api/menus/{menu_id}")
async def delete_menu(menu_id: int):
    db.delete_menu(menu_id)
    return {"status": "ok"}

@app.get("/api/closed-days")
async def api_get_closed_days():
    return {"closed_days": [dict(r) for r in db.get_closed_days()]}

@app.post("/api/closed-days")
async def api_add_closed_day(data: ClosedDayRequest):
    if db.add_closed_day(data.closed_date, data.note):
        return {"status": "ok"}
    return JSONResponse({"error": "登録失敗"}, status_code=500)

@app.delete("/api/closed-days/{closed_date}")
async def api_delete_closed_day(closed_date: str):
    if db.delete_closed_day(closed_date):
        return {"status": "ok"}
    return JSONResponse({"error": "削除失敗"}, status_code=500)

# ===============================
# LIFF（お客様向け予約画面）API
# ===============================

JP_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

def _generate_time_slots():
    """営業時間・予約間隔から時間枠のリストを作る（例: ["09:00","09:30",...]）"""
    start_h, start_m = map(int, BUSINESS_HOURS_START.split(":"))
    end_h, end_m = map(int, BUSINESS_HOURS_END.split(":"))
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    slots = []
    t = start_total
    while t < end_total:
        slots.append(f"{t // 60:02d}:{t % 60:02d}")
        t += SLOT_INTERVAL_MINUTES
    return slots

@app.get("/api/availability")
async def api_get_availability(start_date: str, days: int = 7, duration_minutes: int = 60):
    """指定期間の空き状況を返す（過去時間ブロック ＆ 所要時間考慮の完全版）"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "start_dateの形式が不正です"}, status_code=400)

    # メニューの所要時間（指定がない場合は60分計算）
    menu_duration = duration_minutes or 60
    end = start + timedelta(days=days - 1)

    closed_day_set = {row["closed_date"] for row in db.get_closed_days()}
    
    # 期間内の全予約データを詳細（所要時間付き）で取得
    existing_bookings = db.get_bookings_with_details_in_range(start.isoformat(), end.isoformat())

    all_slots = _generate_time_slots()
    end_h, end_m = map(int, BUSINESS_HOURS_END.split(":"))
    end_total = end_h * 60 + end_m

    from zoneinfo import ZoneInfo
    
    # 強制的にタイ時間（Asia/Bangkok = UTC+7）で現在時刻を取得
    local_now = datetime.now(ZoneInfo("Asia/Bangkok"))
    today_local = local_now.date()
    now_total_minutes = local_now.hour * 60 + local_now.minute

    dates = []
    availability = {}

    for i in range(days):
        d = start + timedelta(days=i)
        d_str = d.isoformat()
        dates.append({"date": d_str, "day": d.day, "weekday": JP_WEEKDAYS[d.weekday()]})

        is_closed_day = (d.weekday() in CLOSED_WEEKDAYS) or (d_str in closed_day_set)
        
        # 当日の確定予約の「開始〜終了時間（分）」リストを作成
        day_booked_spans = []
        for b in existing_bookings:
            if b["booking_date"] == d_str:
                bh, bm = map(int, b["booking_time"].split(":"))
                b_start = bh * 60 + bm
                # メニューの所要時間を取得（無ければ60分）
                b_duration = b["duration_minutes"] if ("duration_minutes" in b.keys() and b["duration_minutes"]) else 60
                b_end = b_start + b_duration
                day_booked_spans.append((b_start, b_end))

        day_availability = {}
        for slot in all_slots:
            h, m = map(int, slot.split(":"))
            slot_start = h * 60 + m
            slot_end = slot_start + menu_duration

            # 1. 休業日
            if is_closed_day:
                day_availability[slot] = "closed"
                continue

            # 2. 過去の日時（本日かつタイの現在時刻より前の時間枠）
            if d < today_local or (d == today_local and slot_start < now_total_minutes):
                day_availability[slot] = "too_late"
                continue

            # 3. 営業終了時間を超える施術
            if slot_end > end_total:
                day_availability[slot] = "closed"
                continue

            # 4. 既存予約との重複チェック（施術時間範囲が被っているか）
            is_overlap = False
            for b_start, b_end in day_booked_spans:
                # 枠の「開始〜終了」が既存予約の「開始〜終了」と重なっているか判定
                if max(slot_start, b_start) < min(slot_end, b_end):
                    is_overlap = True
                    break

            if is_overlap:
                day_availability[slot] = "booked"
            else:
                day_availability[slot] = "available"

        availability[d_str] = day_availability

    return {"time_slots": all_slots, "dates": dates, "availability": availability}

@app.post("/api/booking/create")
async def api_create_booking_from_liff(data: BookingCreateFromLiffRequest):
    """LIFF（booking-form.html）からの新規予約確定"""
    # 空き枠チェック（従来の60分単位判定のまま）
    if not db.is_slot_available(data.booking_date, data.booking_time):
        return JSONResponse({"error": "この日時は既にご予約が入っています"}, status_code=409)

    db.save_customer_profile(
        data.user_id, data.name, furigana=data.furigana,
        gender=data.gender, birthdate=data.birthdate, phone=data.phone
    )
    
    # 選択された複数メニューIDの配列を渡して登録
    booking_id = db.add_booking(data.user_id, data.booking_date, data.booking_time, data.menu_ids)
    if not booking_id:
        return JSONResponse({"error": "予約の作成に失敗しました"}, status_code=500)

    db.add_booking_history(
        booking_id, "created", data.user_id,
        after_date=data.booking_date, after_time=data.booking_time, note="LIFF予約"
    )

    # 通知用に選択メニュー名を結合して取得
    conn = db.get_connection()
    cursor = conn.cursor()
    placeholders = ','.join(['?'] * len(data.menu_ids))
    cursor.execute(f"SELECT GROUP_CONCAT(name, ' + ') as menu_names FROM menus WHERE id IN ({placeholders})", data.menu_ids)
    row = cursor.fetchone()
    menu_name = row["menu_names"] if (row and row["menu_names"]) else "不明"
    conn.close()

    line_handler.send_text(data.user_id, f"""✅ ご予約が確定しました

📅 {data.booking_date} {data.booking_time}
🎨 メニュー: {menu_name}

ご来店を心よりお待ちしております。""")

    line_handler.notify_owner(
        f"🆕 新規予約が入りました（LIFF）\n\n"
        f"お客様: {data.name}\n"
        f"予約日時: {data.booking_date} {data.booking_time}\n"
        f"メニュー: {menu_name}\n"
    )

    return {"status": "ok", "booking_id": booking_id}

@app.post("/api/booking/reschedule")
async def api_reschedule_booking(data: RescheduleRequest):
    """LIFF（calendar.html）からの予約変更"""
    booking = db.get_booking(data.booking_id)
    if not booking or booking["user_id"] != data.user_id:
        return JSONResponse({"error": "予約が見つかりません"}, status_code=404)

    if not db.is_slot_available(data.booking_date, data.booking_time, exclude_booking_id=data.booking_id):
        return JSONResponse({"error": "この日時は既にご予約が入っています"}, status_code=409)

    db.update_booking(data.booking_id, booking_date=data.booking_date, booking_time=data.booking_time)
    db.add_booking_history(
        data.booking_id, "modified", data.user_id,
        before_date=booking["booking_date"], before_time=booking["booking_time"],
        after_date=data.booking_date, after_time=data.booking_time, note="LIFFから変更"
    )

    menu = db.get_menu(booking["menu_id"])
    menu_name = menu["name"] if menu else "不明"

    line_handler.send_text(data.user_id, f"""📝 ご予約日時を変更しました

📅 {data.booking_date} {data.booking_time}
🎨 メニュー: {menu_name}
📍 予約ID: {data.booking_id}""")

    return {"status": "ok"}

# ===============================
# 管理画面向け 予約API
# ===============================

@app.post("/api/bookings")
async def api_create_booking(data: DashboardBookingCreate):
    if not db.is_slot_available(data.booking_date, data.booking_time):
        return JSONResponse({"error": "時間重複"}, status_code=409)
    user_id = data.existing_user_id or f"manual_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:8]}"
    if not data.existing_user_id:
        db.save_customer_profile(user_id, data.customer_name, phone=data.phone)
    booking_id = db.add_booking(user_id, data.booking_date, data.booking_time, data.menu_id, data.notes)
    if booking_id:
        db.add_booking_history(booking_id, "created", user_id, after_date=data.booking_date, after_time=data.booking_time, note="手動登録")
        return {"id": booking_id, "status": "ok"}
    return JSONResponse({"error": "failed"}, status_code=500)

@app.put("/api/bookings/{booking_id}")
async def api_update_booking_endpoint(booking_id: int, data: BookingUpdateRequest):
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "not found"}, status_code=404)
    
    # 変更前と変更後の日時を取得
    new_date = data.booking_date or booking["booking_date"]
    new_time = data.booking_time or booking["booking_time"]

    db.update_booking(booking_id, data.booking_date, data.booking_time, data.menu_id)
    db.add_booking_history(booking_id, "modified", booking["user_id"], before_date=booking["booking_date"], before_time=booking["booking_time"], after_date=new_date, after_time=new_time)
    
    # 手動登録の顧客（manual_...）でなければLINE通知を送信
    user_id = booking["user_id"]
    if user_id and not user_id.startswith("manual_"):
        line_handler.send_text(user_id, f"""📝 ご予約日時が変更されました

📅 変更後の日時: {new_date} {new_time}
📍 予約ID: {booking_id}

ご来店を心よりお待ちしております。""")

    return {"status": "ok"}

@app.post("/api/bookings/{booking_id}/complete")
async def api_complete_booking(booking_id: int, data: CompleteBookingRequest):
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.update_booking_status(booking_id, "completed")
    db.add_visit_history(booking["user_id"], booking_id, booking["booking_date"], data.notes)
    return {"status": "ok"}

@app.delete("/api/bookings/{booking_id}")
async def api_delete_booking(booking_id: int):
    booking = db.get_booking(booking_id)
    if not booking:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.cancel_booking(booking_id)
    db.add_booking_history(booking_id, "cancelled", booking["user_id"], before_date=booking["booking_date"], before_time=booking["booking_time"])
    return {"status": "ok"}

# ===============================
# オーナーコマンド & ヘルスチェック
# ===============================

def handle_owner_command(user_id: str, text: str):
    if text in ["/today", "今日", "本日"]:
        today = datetime.now().date()
        bookings_data = db.get_bookings_by_date(today)
        if not bookings_data:
            line_handler.send_text(user_id, "本日の予約はありません")
            return
        msg = "📅 本日の予約\n\n"
        for b in bookings_data:
            c = db.get_customer(b[2])
            m = db.get_menu(b[4])
            msg += f"⏰ {b[3]} - {c['name'] if c else '不明'} ({m['name'] if m else '不明'})\n"
        line_handler.send_text(user_id, msg)

@app.get("/api/reports/monthly", dependencies=[Depends(verify_admin)])
async def api_monthly_report(year: int, month: int):
    import calendar
    start_date = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    # 基本サマリー
    cursor.execute("""
        SELECT 
            COUNT(*) as total_bookings,
            SUM(CASE WHEN status != 'cancelled' THEN 1 ELSE 0 END) as active_bookings,
            SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled_bookings,
            SUM(CASE WHEN status = 'completed' THEN m.price ELSE 0 END) as revenue,
            AVG(CASE WHEN status = 'completed' THEN m.price END) as avg_price
        FROM bookings b
        LEFT JOIN menus m ON b.menu_id = m.id
        WHERE b.booking_date >= ? AND b.booking_date <= ?
    """, (start_date, end_date))
    row = cursor.fetchone()
    
    # ★★★ ここが修正点：fetchone() が None の場合の対策 ★★★
    if row is None:
        summary = {
            "total_bookings": 0, "active_bookings": 0,
            "cancelled_bookings": 0, "revenue": 0, "avg_price": 0,
            "cancellation_rate": 0.0
        }
    else:
        summary = dict(row)
        summary["total_bookings"] = summary.get("total_bookings") or 0
        summary["active_bookings"] = summary.get("active_bookings") or 0
        summary["cancelled_bookings"] = summary.get("cancelled_bookings") or 0
        summary["revenue"] = summary.get("revenue") or 0
        # ★★★ avg_price が None の場合の対策 ★★★
        avg_val = summary.get("avg_price")
        summary["avg_price"] = int(avg_val) if avg_val is not None else 0
        total = summary["total_bookings"]
        summary["cancellation_rate"] = round(
            (summary["cancelled_bookings"] / total * 100), 1
        ) if total else 0.0
    
    # メニュー別売上ランキング
    cursor.execute("""
        SELECT m.name, COUNT(*) as count, SUM(m.price) as revenue
        FROM bookings b
        JOIN menus m ON b.menu_id = m.id
        WHERE b.booking_date >= ? AND b.booking_date <= ? AND b.status = 'completed'
        GROUP BY m.id
        ORDER BY revenue DESC
    """, (start_date, end_date))
    menu_stats = [dict(r) for r in cursor.fetchall()]
    
    # 時間帯別予約分布
    cursor.execute("""
        SELECT substr(booking_time, 1, 2) as hour, COUNT(*) as count
        FROM bookings
        WHERE booking_date >= ? AND booking_date <= ? AND status IN ('confirmed', 'completed')
        GROUP BY hour
        ORDER BY hour
    """, (start_date, end_date))
    time_stats = [dict(r) for r in cursor.fetchall()]
    
    # 顧客ランキング（来店回数）
    cursor.execute("""
        SELECT c.name, COUNT(*) as visits, SUM(m.price) as total_spent
        FROM bookings b
        JOIN customers c ON b.user_id = c.user_id
        JOIN menus m ON b.menu_id = m.id
        WHERE b.booking_date >= ? AND b.booking_date <= ? AND b.status = 'completed'
        GROUP BY b.user_id
        ORDER BY visits DESC
        LIMIT 10
    """, (start_date, end_date))
    customer_stats = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    
    return {
        "summary": summary,
        "menu_stats": menu_stats,
        "time_stats": time_stats,
        "customer_stats": customer_stats
    }

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
