# ローカル開発環境

## 概要

akatsukiのローカル開発環境（Docker Playwright）のセットアップと使用方法です。

---

## 前提条件

- Docker / Docker Compose
- AWS CLI（S3アクセス用）
- IPATアカウント

---

## Docker環境のセットアップ

### 1. イメージのビルド

```bash
docker build -t akatsuki-bot -f docker/Dockerfile .
```

### 2. 環境変数設定

```bash
cp .env.example .env
vim .env
```

`.env`の設定:

```
# IPAT認証（ローカル開発用）
JRA_INET_ID=1234567890
JRA_USER_ID=1234
JRA_P_ARS=5678

# AWS設定
AWS_REGION=ap-northeast-1
AWS_SECRET_NAME=jrdb-main/ipat

# 動作設定
TIMEOUT_MS=30000
DRY_RUN=true
```

---

## 基本的な使い方

### インタラクティブ実行

```bash
docker run --rm -it \
  --env-file .env \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  akatsuki-bot bash
```

### 購入実行（ドライラン）

```bash
docker run --rm \
  --env-file .env \
  -e DRY_RUN=true \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  akatsuki-bot
```

---

## ディレクトリ構成

### tickets/（入力）

購入指示CSVを配置:

```
tickets/
└── 20251116.csv
```

CSVフォーマット:

```csv
場コード,レース番号,馬番
06,01,03
06,01,07
05,05,12
```

### output/（出力）

購入結果が出力される:

```
output/
└── result_20251116.json
```

---

## デバッグ方法

### ヘッドレスモード無効化

ローカルでブラウザを表示してデバッグ:

```python
# scripts/bot.py
browser = await playwright.chromium.launch(
    headless=False,  # ブラウザを表示
    slow_mo=1000     # 操作を遅くする（ms）
)
```

### スクリーンショット取得

```python
await page.screenshot(path="/app/output/debug.png")
```

---

## テスト実行

### ユニットテスト

```bash
docker run --rm \
  --env-file .env \
  akatsuki-bot \
  python -m pytest tests/
```

### 統合テスト（ドライラン）

```bash
docker run --rm \
  --env-file .env \
  -e DRY_RUN=true \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  akatsuki-bot \
  python scripts/bot.py --date 20251116
```

---

## Lambda環境との違い

| 項目 | ローカル | Lambda |
|------|---------|--------|
| 認証情報 | .envファイル | Secrets Manager |
| 入力 | tickets/ディレクトリ | Step Functions |
| 出力 | output/ディレクトリ | S3 |
| ブラウザ | headless/表示選択可 | headless必須 |
| タイムアウト | 無制限 | 15分 |

---

## トラブルシューティング

### Playwright起動エラー

**症状**:
```
Browser failed to launch
```

**対処法**:
- Dockerイメージを再ビルド
- メモリ割り当てを増加

### 認証エラー

**症状**:
```
Login failed: Invalid credentials
```

**対処法**:
- `.env`の認証情報を確認
- IPATにブラウザから手動ログインできるか確認

### タイムアウト

**症状**:
```
Timeout exceeded
```

**対処法**:
- `TIMEOUT_MS`を増加
- ネットワーク接続を確認

---

## 開発ワークフロー

### 1. コード変更

ホスト側でコードを編集（VSCode等）

### 2. ローカルテスト

```bash
# ドライランでテスト
docker run --rm \
  --env-file .env \
  -e DRY_RUN=true \
  -v ${PWD}:/app \
  akatsuki-bot \
  python scripts/bot.py --date 20251116
```

### 3. Lambdaデプロイ

```bash
# Lambdaイメージのビルド
docker build -t akatsuki-lambda -f docker/Dockerfile.lambda .

# ECRへプッシュ
# (docs/lambda-purchase.md参照)
```
