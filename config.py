"""
店舗設定
営業時間・定休日・予約間隔をここで一括管理する。
変更したいときはこのファイルだけ編集すればOK。
"""

# 営業時間
BUSINESS_HOURS_START = "09:00"
BUSINESS_HOURS_END = "18:00"

# 予約の時間間隔（分）
SLOT_INTERVAL_MINUTES = 60

# 定休日（0=月, 1=火, 2=水, 3=木, 4=金, 5=土, 6=日）
CLOSED_WEEKDAYS = [1, 2]  # 火曜・水曜定休
