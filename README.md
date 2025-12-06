# akatsuki - IPAT自動購入システム

JRA IPATへの自動ログイン・入金・馬券購入を行うシステムです。

## システム構成

本システムは4つのリポジトリで構成されています：

| リポジトリ | 役割 | Lambda関数 |
|-----------|------|-----------|
| [apollo](https://github.com/ArumetaM/apollo) | JRDBデータダウンロード・CSV変換 | jrdb-main-downloader |
| [saudade](https://github.com/ArumetaM/saudade) | ML推論・買い目生成 | jrdb-main-inference |
| **akatsuki** | IPAT自動購入 | jrdb-main-purchase |
| [melissa](https://github.com/ArumetaM/melissa) | AWSインフラ（Terraform） | - |

### データフロー

```
EventBridge (19:00 JST) → Step Functions
    ↓
apollo: JRDB → S3 (csv/)
    ↓
saudade: CSV → 推論 → 買い目JSON
    ↓
akatsuki: 買い目 → IPAT購入
```

---

## 本番運用（2025年11月〜）

### Lambda関数

- **関数名**: `jrdb-main-purchase`
- **トリガー**: Step Functions経由
- **ランタイム**: コンテナイメージ（Playwright）
- **タイムアウト**: 15分

### S3入出力

| 種別 | パス |
|------|------|
| 入力（買い目） | `s3://jrdb-main-financial-data/inference-results/{YYYY}/{MM}/{DD}/` |
| 出力（購入結果） | `s3://jrdb-main-financial-data/purchase-results/{YYYY}/{MM}/{DD}/` |

### Slack通知

- **購入開始時**: 対象日付、買い目数
- **購入完了時**: 合計金額、購入レース数
- **エラー発生時**: エラー内容、スタックトレース

---

## クイックスタート

### Lambda購入（本番）

```bash
# Step Functions経由で実行（通常はsaudade完了後に自動起動）
aws lambda invoke \
  --function-name jrdb-main-purchase \
  --payload '{"target_date": "20251116", "bets": [{"place_code": "06", "race_number": "01", "horse_number": "03"}]}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/response.json \
  --profile terraform-infra

cat /tmp/response.json
```

### ローカル実行（開発）

```bash
# Dockerイメージのビルド
docker build -t akatsuki-bot -f docker/Dockerfile .

# 環境変数設定
cp .env.example .env
vim .env  # IPATログイン情報を設定

# 実行
docker run --rm \
  --env-file .env \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  akatsuki-bot
```

---

## 入力形式（買い目）

### Step Functions入力

```json
{
  "target_date": "20251116",
  "bets": [
    {"place_code": "06", "race_number": "01", "horse_number": "03"},
    {"place_code": "06", "race_number": "01", "horse_number": "07"},
    {"place_code": "05", "race_number": "05", "horse_number": "12"}
  ]
}
```

### S3買い目ファイル

saudadeが出力する買い目ファイル形式：

```
{場コード},{レース番号},{馬番}
```

---

## 出力形式（購入結果）

### Lambda出力

```json
{
  "statusCode": 200,
  "body": {
    "success": true,
    "target_date": "20251116",
    "purchases_count": 5,
    "total_amount": 5000,
    "output_path": "s3://jrdb-main-financial-data/purchase-results/2025/11/16/result_20251116.json"
  }
}
```

### S3購入結果ファイル

```json
{
  "target_date": "20251116",
  "executed_at": "2025-11-16T10:30:00+09:00",
  "total_amount": 5000,
  "purchases": [
    {
      "place_code": "06",
      "place_name": "中山",
      "race_number": "01",
      "horse_number": "03",
      "amount": 1000,
      "status": "success"
    }
  ]
}
```

---

## ディレクトリ構成

```
akatsuki/
├── lambda/
│   ├── purchase_handler.py   # Lambda エントリーポイント
│   └── slack_service.py      # Slack通知
├── scripts/
│   ├── bot.py               # メインスクリプト
│   └── bot_simple.py        # シンプル版
├── docker/
│   └── Dockerfile           # Playwright環境
├── tickets/                 # 購入指示CSV（ローカル）
├── output/                  # 実行結果（ローカル）
└── requirements.txt
```

---

## 環境変数

### Secrets Manager（本番）

IPAT認証情報はAWS Secrets Managerで管理：

| シークレット名 | 内容 |
|---------------|------|
| `jrdb-main/ipat` | jra_user_id, jra_p_ars, jra_inet_id |
| `jrdb-main/slack` | bot_token, channel_id |

### ローカル実行用（.env）

| 変数名 | 説明 |
|--------|------|
| AWS_SECRET_NAME | Secrets Managerのシークレット名 |
| TIMEOUT_MS | ページ遷移待機時間 |
| S3_CONFIG_BUCKET | 設定ファイル用S3バケット |

---

## 詳細ドキュメント

- [Lambda購入詳細](docs/lambda-purchase.md)
- [IPAT自動化の注意点](docs/ipat-automation.md)
- [Slack通知設定](docs/slack-notification.md)
- [ローカル開発環境](docs/local-development.md)

---

## 購入履歴管理（冪等性・照会検証）

### S3購入履歴

購入処理は冪等性を確保するため、S3に購入履歴を記録しています。

| パス | 内容 |
|------|------|
| `s3://jrdb-main-financial-data/purchase-history/{YYYYMMDD}/tickets.json` | 日別の購入履歴 |

### ステータス管理

| ステータス | 意味 | 次回購入時の動作 |
|------------|------|------------------|
| **PURCHASED** | 照会確認済み | スキップ（重複購入しない） |
| **UNVERIFIED** | 画面成功だが照会未確認 | 再購入対象 |
| **FAILED** | 購入失敗 | 再購入対象 |

### 照会検証フロー

購入ボタン押下後、実際に購入されたかを照会メニューで確認します。

```
1. 購入ボタンクリック → 画面で成功確認
2. 5秒待機
3. 照会メニューへ遷移 → 投票内容照会で該当チケットを検索
4. 確認結果に応じて:
   - 確認成功 → S3にPURCHASEDとして記録
   - 確認失敗 → S3にUNVERIFIEDとして記録 + Slack通知
5. 次のチケット処理へ
```

### 購入履歴レコード形式

```json
{
  "race_course": "中山",
  "race_number": 8,
  "horse_number": 3,
  "bet_type": "単勝",
  "amount": 5000,
  "status": "PURCHASED",
  "purchased_at": "2025-12-06T12:30:00+00:00"
}
```

### Slack通知

| 通知種別 | 説明 |
|----------|------|
| 購入検証失敗 | 画面上は成功表示でも照会で確認できなかった場合に通知 |
| 入金失敗 | 銀行口座残高不足で入金できなかった場合に通知 |

---

## 更新履歴

### 2025-12-06
- 購入後の照会検証機能追加（UNVERIFIED対応）
- 入金失敗時のSlack通知追加

### 2025-11-29
- Slack通知にリトライロジック追加（tenacity）

### 2025-11-01
- AWS Lambda本番運用開始
- Step Functions連携実装
