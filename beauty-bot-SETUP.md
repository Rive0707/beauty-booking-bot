# 💇‍♀️ LINE美容室予約BOT セットアップガイド

## 📋 概要

このボットは以下の機能を提供します：

✅ **顧客向け**
- LINEで日時選択予約
- 予約の変更・キャンセル
- 来店履歴・メモ確認
- 1週間前の自動リマインド

✅ **オーナー向け**
- Web管理ダッシュボード
- メニュー管理（追加・削除）
- 本日・明日の予約確認
- リアルタイム顧客管理

---

## 🚀 セットアップステップ

### ステップ 1: LINE Official Account 作成（5分）

1. **LINE Business Center にアクセス**
   - https://business.line.biz/
   - LINEアカウントでログイン

2. **新しいアカウントを作成**
   - アカウント名: 「美容室名 予約」など
   - 業種: 「美容・理容」

3. **Messaging APIを有効化**
   - LINE Developers Console にアクセス
   - アカウント → Messaging API → 有効化

4. **必要な情報を取得**
   ```
   📝 メモしておく：
   - Channel Access Token
   - Channel Secret
   - あなたのLINE User ID
   ```

**USER IDの取得方法：**
- LINE Developers Console の「テスト用ユーザーID」を確認
- または、別のアカウントから操作して日志から取得

---

### ステップ 2: Replit にアップロード（3分）

1. **Replit にアクセス**
   - https://replit.com/
   - Google/GitHub でログイン

2. **新規プロジェクト作成**
   - 「Create」 → 「Python」を選択
   - プロジェクト名: `beauty-booking-bot`

3. **ファイルをアップロード**
   ```
   Replit左パネルの「Upload」からファイルをアップロード：
   
   - main.py
   - database.py
   - line_handler.py
   - reminder.py
   - requirements.txt
   - .env (後で作成)
   ```

---

### ステップ 3: 環境変数設定（2分）

1. **Replit で `.env` ファイル作成**
   ```
   左パネル → 「+ New file」
   ファイル名: `.env`
   ```

2. **内容をコピー**
   ```
   LINE_CHANNEL_ACCESS_TOKEN=xxxxxxxxxxxxxxx
   LINE_CHANNEL_SECRET=xxxxxxxxxxxxxxx
   OWNER_USER_ID=Uxxxxxxxxxxxxxxx
   PORT=8000
   ```

   取得した値に置き換え

3. **Replit の Secrets 機能を使う（推奨）**
   - 左上の鍵アイコン → 「New Secret」
   - Key: `LINE_CHANNEL_ACCESS_TOKEN` → Value: トークン貼り付け
   - 同様に `LINE_CHANNEL_SECRET`, `OWNER_USER_ID` も設定

---

### ステップ 4: 依存ライブラリをインストール（1分）

Replit のターミナルで実行：

```bash
pip install -r requirements.txt
```

または「Run」ボタンを押すと自動でインストール

---

### ステップ 5: アプリを起動（1分）

Replit の「Run」ボタンを押す

ログに以下が表示されたら成功：

```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

### ステップ 6: LINE Webhook URL を設定（3分）

1. **Replit の URL をコピー**
   - Replit画面右上に表示される URL
   - 例: `https://beauty-booking-bot.xxxxxxx.replit.dev`

2. **LINE Developers Console で設定**
   - Messaging API 設定
   - Webhook URL: `https://beauty-booking-bot.xxxxxxx.replit.dev/callback`
   - ✅ Webhook を有効化

3. **Verify Token をテスト**
   - Developers Console で「検証」ボタン
   - 「成功」と表示されたら OK

---

### ステップ 7: LINE BOT をテスト（2分）

1. **LINE 公式アカウントを友達追加**
   - QR コード をスキャン（LINE Business Center 内）

2. **テストメッセージ送信**
   - 「予約」と送信
   - カレンダーが表示されたら成功！

---

## 🎯 使い方

### 📱 顧客側

```
【予約フロー】
「予約」ボタン
  ↓
📅 日付選択（カレンダーピッカー）
  ↓
⏰ 時間選択（10:00〜19:00）
  ↓
🎨 メニュー選択
  ↓
✅ 確認 → 予約完了！
```

### 🖥️ オーナー側

**Web 管理画面：**
- `https://beauty-booking-bot.xxxxxxx.replit.dev/`
- 🎨 メニュー追加・削除
- 📅 本日の予約確認
- 📋 顧客管理

**LINE コマンド（オーナー用）：**
```
/today    → 本日の予約一覧
/tomorrow → 明日の予約一覧
/help     → ヘルプ表示
```

---

## 🔧 カスタマイズ例

### 営業時間を変更

`line_handler.py` の `show_time_picker()` 関数：

```python
# 現在: 10:00-19:00
for hour in range(10, 19):

# 例：11:00-20:00 にする場合
for hour in range(11, 20):
```

### 1週間前のリマインド → 1日前に変更

`reminder.py` の `check_and_send_reminders()` 関数：

```python
# 現在：7日以内
if 0 <= days_until_booking <= 7:

# 例：1日前だけ
if days_until_booking == 1:
```

### 施術時間が動的に選択肢に反映されるようにする

`line_handler.py` の `show_time_picker()` で、選択メニュー毎に施術時間を考慮して次の利用可能時間を計算可能にする拡張

---

## 📊 ファイル構成

```
beauty-booking-bot/
├── main.py
│   └── FastAPI メインアプリ
│       - LINE Webhook 受信
│       - Web 管理画面
│       - メニュー API
│
├── database.py
│   └── SQLite 操作
│       - 顧客管理
│       - 予約管理
│       - メニュー管理
│       - 来店履歴
│
├── line_handler.py
│   └── LINE メッセージ処理
│       - 予約フロー
│       - 変更・キャンセル
│       - マイページ表示
│
├── reminder.py
│   └── 自動リマインド
│       - 1週間前に通知
│       - 変更・キャンセル誘導
│
├── requirements.txt
│   └── ライブラリ依存関係
│
└── .env
    └── 環境変数（秘密）
```

---

## 🐛 よくあるエラーと対処法

### ❌ Webhook接続エラー

**症状**: LINE Developers Console で「接続 失敗」

**対処**:
1. Replit が起動してるか確認
2. Webhook URL が正しいか確認（末尾 `/callback` まで）
3. Channel Secret が正しいか確認

### ❌ メッセージが来ない

**症状**: メッセージを送っても返信がない

**対処**:
1. LINE 公式アカウントを友達追加したか確認
2. Replit のログにエラーがないか確認
3. Webhook が有効になっているか確認

### ❌ リマインドが送信されない

**症状**: 7日前なのにリマインドが来ない

**対処**:
1. Replit が24時間起動してるか確認（Replit Free は自動停止する可能性）
2. 予約日時が正しく保存されているか DB で確認
3. `reminder_sent` フラグが正しく更新されているか確認

### ❌ データベースエラー

**症状**: 予約保存時にエラー

**対処**:
1. `beauty_booking.db` ファイルが Replit に作成されているか確認
2. SQL クエリのエラーをログで確認
3. 必要に応じて DB を初期化：
   ```bash
   rm beauty_booking.db
   python main.py  # 再起動で DB 再作成
   ```

---

## 💡 機能拡張アイデア

すべて後から追加可能です：

### Phase 2（簡単）
- ✅ 複数スタッフ対応
- ✅ 事前決済（Stripe/PayPal連携）
- ✅ 予約枠の自動設定（30分刻みなど）
- ✅ キャンセル待ちリスト

### Phase 3（中程度）
- ✅ SNS自動投稿（Instagram → LINE流入）
- ✅ 顧客セグメント（新規/リピート）
- ✅ 売上レポート生成
- ✅ Google Calendar 連携

### Phase 4（高度）
- ✅ AI による顧客分析
- ✅ 推奨メニュー提示
- ✅ 自動リマインド最適化
- ✅ 多言語対応

---

## 📞 サポート

### ドキュメント参考
- [LINE Messaging API ドキュメント](https://developers.line.biz/ja/docs/messaging-api/)
- [FastAPI チュートリアル](https://fastapi.tiangolo.com/ja/)
- [APScheduler ドキュメント](https://apscheduler.readthedocs.io/)

### デバッグ
- Replit のログを確認（右パネル）
- LINE Developers Console でテスト送信
- SQLite DB をブラウザで確認: [sqlitebrowser.org](https://sqlitebrowser.org/)

---

## 🎉 セットアップ完了！

ここまでで **完全に動作する LINE 予約BOT** が完成です。

顧客も オーナーも LINE だけで全て完結できます。😊

**次のステップ:**
1. 実際に何人かにテストしてもらう
2. フィードバックを集める
3. 必要な機能を追加する

頑張ってください！
