# Lambda購入詳細

## 概要

akatsukiのLambda関数（`jrdb-main-purchase`）は、saudadeが生成した買い目をIPATで自動購入します。

---

## 処理フロー

```
1. Step Functionsから買い目リストを受信
   ↓
2. S3から入金額設定を取得
   ↓
3. IPAT認証情報をSecrets Managerから取得
   ↓
4. Playwrightでブラウザ起動
   ↓
5. IPATにログイン（2段階認証）
   ↓
6. 自動入金処理
   ↓
7. 各買い目を順次購入
   ↓
8. 購入結果をS3に保存
   ↓
9. Slack通知
```

---

## Lambda設定

| 項目 | 値 |
|------|-----|
| 関数名 | jrdb-main-purchase |
| ランタイム | コンテナイメージ |
| メモリ | 2048MB |
| タイムアウト | 900秒（15分） |
| 一時ストレージ | 512MB |

---

## 入力イベント

### Step Functions経由

```json
{
  "target_date": "20251116",
  "bets": [
    {"place_code": "06", "race_number": "01", "horse_number": "03"},
    {"place_code": "06", "race_number": "01", "horse_number": "07"}
  ],
  "dry_run": false
}
```

| フィールド | 説明 | デフォルト |
|-----------|------|-----------|
| target_date | 購入対象日付（YYYYMMDD） | 必須 |
| bets | 買い目リスト | 必須 |
| dry_run | ドライランモード（購入実行しない） | false |

---

## 出力レスポンス

### 成功時

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

### エラー時

```json
{
  "statusCode": 500,
  "body": {
    "success": false,
    "error": "Login failed: Invalid credentials",
    "target_date": "20251116"
  }
}
```

---

## 購入金額計算

### 均等配分

```python
def calculate_bet_amount(total_budget: int, bets_count: int) -> int:
    """1買い目あたりの金額を計算"""
    if bets_count == 0:
        return 0

    # 100円単位に丸める
    amount_per_bet = (total_budget // bets_count // 100) * 100

    # 最低100円
    return max(amount_per_bet, 100)
```

### 入金額設定

S3から入金額設定を取得：

```
s3://akatsuki-config/deposit_config.json
```

```json
{
  "default_deposit": 10000,
  "max_deposit_per_day": 50000
}
```

---

## デプロイ手順

### 1. Dockerイメージのビルド

```bash
docker build -t akatsuki-lambda -f docker/Dockerfile.lambda .
```

### 2. ECRへプッシュ

```bash
aws ecr get-login-password --region ap-northeast-1 --profile terraform-infra | \
docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.ap-northeast-1.amazonaws.com

docker tag akatsuki-lambda:latest \
  <ACCOUNT_ID>.dkr.ecr.ap-northeast-1.amazonaws.com/jrdb-main-purchase:latest

docker push <ACCOUNT_ID>.dkr.ecr.ap-northeast-1.amazonaws.com/jrdb-main-purchase:latest
```

### 3. Lambda更新

```bash
aws lambda update-function-code \
  --function-name jrdb-main-purchase \
  --image-uri <ACCOUNT_ID>.dkr.ecr.ap-northeast-1.amazonaws.com/jrdb-main-purchase:latest \
  --profile terraform-infra
```

---

## ドライランモード

テスト実行時は`dry_run: true`を指定：

```bash
aws lambda invoke \
  --function-name jrdb-main-purchase \
  --payload '{"target_date": "20251116", "bets": [...], "dry_run": true}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/response.json \
  --profile terraform-infra
```

ドライランモードでは：
- IPATへのログインは実行
- 購入処理はスキップ
- 購入結果は「simulated」として記録

---

## トラブルシューティング

### ログインエラー

**症状**: Login failed

**対処法**:
- Secrets Managerの認証情報を確認
- IPATアカウントがロックされていないか確認
- 2段階認証の手順を確認

### タイムアウト

**症状**: Task timed out after 900.00 seconds

**対処法**:
- 買い目数が多すぎる場合は分割
- ネットワーク遅延を確認

### Playwright起動エラー

**症状**: Browser failed to launch

**対処法**:
- Lambdaのメモリ設定を確認
- Dockerイメージのビルドを再実行
