# 💇‍♀️ LINE美容室予約BOT

LINE Messaging API を使用した、完全自動化された美容室予約システムです。

## ✨ 機能

### 顧客向け（LINE）
- ✅ LINE で日時選択による予約
- ✅ メニュー選択
- ✅ 予約の変更・キャンセル
- ✅ 来店履歴確認
- ✅ 予約1週間前の自動リマインダー

### オーナー向け（Web管理画面）
- ✅ メニュー管理（追加・削除）
- ✅ LINE登録顧客から選択して予約追加
- ✅ 本日の予約確認
- ✅ 顧客情報管理
- ✅ リアルタイムダッシュボード

## 🛠️ 技術スタック

- **バックエンド**: Python FastAPI
- **データベース**: SQLite
- **スケジューラー**: APScheduler
- **LINE API**: LINE Messaging API
- **ホスティング**: Railway

## 📋 ファイル構成

```
beauty-booking-bot/
├── main.py                 # FastAPI メインアプリケーション
├── database.py             # SQLite データベース操作
├── line_handler.py         # LINE メッセージ処理
├── reminder.py             # 自動リマインダー機能
├── requirements.txt        # Python 依存ライブラリ
├── .env                    # 環境変数（秘密）
└── README.md              # このファイル
```

## 🚀 クイックスタート

### 1. 環境変数を設定

```bash
cp .env.example .env
```

`.env` に以下を設定：

```
LINE_CHANNEL_ACCESS_TOKEN=xxxxxxxxxxxxxx
LINE_CHANNEL_SECRET=xxxxxxxxxxxxxx
OWNER_USER_ID=Uxxxxxxxxxxxxxxx
PORT=8000
```

### 2. 依存ライブラリをインストール

```bash
pip install -r requirements.txt
```

### 3. ローカルで実行（開発用）

```bash
python main.py
```

ブラウザで http://localhost:8000/ を開く

### 4. Railway にデプロイ

```bash
git add .
git commit -m "Deploy to Railway"
git push origin main
```

## 📱 LINE 顧客の使い方

### 初回：LINE で登録

1. BOT を友達追加
2. 何かメッセージを送信（何でもいい）
3. ドロップダウンに表示されるようになります

### 予約する

1. LINE で「予約」と送信
2. 日付を選択
3. 時間を選択
4. メニューを選択
5. 確定
6. 完了！🎉

### 予約を変更・キャンセル

1. 「マイページ」を送信
2. 次回予約が表示される
3. 変更またはキャンセルボタンをクリック

## 🖥️ Web管理画面の使い方

### 管理画面にアクセス

```
https://your-railway-url.railway.app/
```

### 予約を追加

1. 「予約を追加（LINE登録顧客）」セクション
2. ドロップダウンから顧客を選択
3. 日付・時間・メニューを選択
4. 「予約を追加」をクリック
5. 顧客に自動で確認メッセージが送信されます

### メニュー管理

1. 「メニュー管理」セクション
2. メニュー名・価格・施術時間を入力
3. 「メニューを追加」をクリック
4. 削除は各メニューの「削除」ボタン

## ⚙️ オーナーコマンド（LINE）

オーナーアカウント（OWNER_USER_ID）から以下のコマンドが使えます：

```
/today または 「今日」「本日」
→ 本日の予約一覧を表示

/tomorrow または 「明日」
→ 明日の予約一覧を表示

/help
→ コマンド一覧を表示
```

## 🔧 カスタマイズ

### 営業時間を変更

`line_handler.py` の `show_time_picker()` 関数：

```python
# 現在: 10:00-19:00
for hour in range(10, 19):

# 例：11:00-20:00 に変更する場合
for hour in range(11, 20):
```

### リマインダー時間を変更

`reminder.py` の `check_and_send_reminders()` 関数：

```python
# 現在：7日以内に送信
if 0 <= days_until_booking <= 7:

# 例：1日前だけに送信する場合
if days_until_booking == 1:
```

## 📊 データベース設計

### customers テーブル
```
id, user_id, name, phone, created_at
```

### menus テーブル
```
id, name, price, duration_minutes, created_at
```

### bookings テーブル
```
id, booking_date, user_id, booking_time, menu_id, notes, status, reminder_sent, created_at
```

### visit_history テーブル
```
id, user_id, booking_id, visited_date, memo, created_at
```

## 🐛 トラブルシューティング

### LINE Webhook が接続できない

- Railway の Webhook URL が正しいか確認
- Webhook URL の形式: `https://your-url.railway.app/callback`
- LINE Developers Console で「検証」ボタンを押す

### メッセージが送信されない

- Channel Access Token が正しいか確認
- 環境変数が設定されているか確認
- Railway のログを確認

### リマインダーが送信されない

- Railway が24時間起動しているか確認
- 予約日時が正しく保存されているか確認
- APScheduler のログを確認

## 📞 サポート

問題が発生した場合：

1. Railway のログを確認
2. LINE Developers Console で Webhook 検証
3. SQLite DB のデータを確認

## 📄 ライセンス

このプロジェクトはオープンソースです。

## 🎉 完成！

このシステムで、完全に自動化された LINE 予約システムが実現します。

質問や機能追加のご相談はお気軽に！
