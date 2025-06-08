# akatsuki - JRA自動投票Bot (MVP)

> 入金 → 馬券購入 → 投票結果CSV取得を自動化  
> **Tech**: Python + Playwright / Docker / WSL2 (local dev)  
> **Scope**: MVPはS3連携なし、ローカルの`tickets.csv`から投票実行

---

## 1. プロジェクト概要

* 自宅PCのGPU予測モデルで算出した**購入指示 (tickets.csv)** を自動実行
* 初期段階はローカル完結で開発し、安定動作を確認
* 将来的に**S3 → AWS Lambda (定期実行)** へ移行予定
* boto3との親和性を考慮しPythonで実装

---

## 2. アーキテクチャ (MVP)

```
┌────────────┐     ┌─────────────────┐
│tickets.csv │────▶│ Docker Container │
│  (local)   │     │ Python+Playwright│
└────────────┘     └────────┬────────┘
                            │
                            ▼
                    https://www.ipat.jra.go.jp/
                            │
                            ▼
                    output/results-yyyyMMdd.csv
```

* **tickets.csv**: 購入指示ファイル（フォーマット開発中）
* **Playwright Bot**:
  1. IPAT.JRA.GO.JPへログイン
  2. 自動入金処理
  3. 指示通りに馬券購入
  4. 投票結果CSVダウンロード
* 取得したCSVは`output/`に保存（将来S3へアップロード）

---

## 3. ディレクトリ構成

```
.
├── docker/
│   └── Dockerfile          # Python + Playwright環境
├── scripts/
│   └── bot.py             # メインスクリプト
├── tickets/               # 購入指示CSV配置
│   └── sample_tickets.csv
├── output/                # ダウンロードしたCSV保存先
├── requirements.txt       # Python依存関係
├── .env.example          # 環境変数テンプレート
└── README.md
```

---

## 4. Quick Start (ローカル開発)

```bash
# 1) Dockerイメージのビルド
docker build -t akatsuki-bot -f docker/Dockerfile .

# 2) 購入指示CSVを配置
cp path/to/your/tickets.csv tickets/

# 3) 環境変数を設定
cp .env.example .env
vim .env  # IPATのログイン情報を設定

# 4) 実行
docker run --rm \
  --env-file .env \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  akatsuki-bot
```

---

## 5. 環境変数 (.env)

| Key | 説明 | 例 |
|-----|------|-----|
| AWS_ACCESS_KEY_ID | AWSアクセスキー | AKIAXXXXXXXXXXXXXXXX |
| AWS_SECRET_ACCESS_KEY | AWSシークレットキー | ******** |
| AWS_DEFAULT_REGION | AWSリージョン | ap-northeast-1 |
| SECRETS_MANAGER_SECRET_ID | Secrets ManagerのシークレットID | akatsuki/ipat/credentials |
| AUTO_DEPOSIT_AMOUNT | 自動入金額（円） | 10000 |
| TIMEOUT_MS | ページ遷移待機時間 | 20000 |

※IPAT認証情報（ログインID、パスワード、PAY-NAVI暗証番号）はAWS Secrets Managerに保存

---

## 6. 実装詳細

### 6.1 Dockerfile

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwrightブラウザをインストール
RUN playwright install chromium

COPY . .

# セキュリティのため非rootユーザーで実行
RUN useradd -m -u 1000 bot && chown -R bot:bot /app
USER bot

CMD ["python", "scripts/bot.py"]
```

### 6.2 bot.py メインフロー

```python
import os
import asyncio
import json
from datetime import datetime
from playwright.async_api import async_playwright
import pandas as pd
import boto3

async def get_secrets():
    """AWS Secrets Managerから認証情報を取得"""
    client = boto3.client('secretsmanager')
    secret_id = os.environ['SECRETS_MANAGER_SECRET_ID']
    
    response = client.get_secret_value(SecretId=secret_id)
    secrets = json.loads(response['SecretString'])
    
    return {
        'username': secrets['ipat_username'],
        'password': secrets['ipat_password'],
        'paynavi_pass': secrets['ipat_paynavi_password']
    }

async def main():
    # Secrets Managerから認証情報取得
    secrets = await get_secrets()
    deposit_amount = int(os.environ.get('AUTO_DEPOSIT_AMOUNT', '10000'))
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        
        # 1) IPATログイン
        await login_ipat(page, secrets['username'], secrets['password'])
        
        # 2) 自動入金
        await auto_deposit(page, secrets['paynavi_pass'], deposit_amount)
        
        # 3) tickets.csv読み込み・投票実行
        tickets_df = pd.read_csv('tickets/tickets.csv')
        for _, ticket in tickets_df.iterrows():
            await place_bet(page, ticket)
        
        # 4) 投票結果ダウンロード
        download_path = await download_results(page)
        print(f"Results saved to: {download_path}")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 7. CI/CD (将来実装)

* GitHub Actions でDockerイメージビルド・プッシュ
* Playwright Test Reportの自動生成
* AWS SAMでLambdaデプロイパイプライン構築

---

## 8. ロードマップ

| フェーズ | ゴール | 技術要素 |
|---------|--------|----------|
| **MVP** | ローカルで自動投票・CSV取得 | Python, Playwright, Docker |
| Phase 2 | S3連携（tickets取得・results保存） | boto3, IAM |
| Phase 3 | Lambda化 + EventBridge定期実行 | Lambda Container, SAM |
| Phase 4 | セキュリティ・監視強化 | Secrets Manager, CloudWatch |
| Phase 5 | 予測モデル連携・資金管理自動化 | DynamoDB, Step Functions |

---

## 9. Contributing

* `.devcontainer/devcontainer.json` でVS Code Remote Containers対応
* `black` + `flake8` でコード品質管理
* Issue/PRテンプレートで再現手順・期待結果を明記

---

## 10. License

Apache-2.0  
© 2025 Yusuke

---

### 開発メモ

* IPATのセレクタは実際のサイトを確認しながら調整が必要
* 入金処理はPAY-NAVI連携で実装
* Lambda移行時は`/tmp`へのダウンロード→S3アップロードに変更
* エラーハンドリング・リトライ処理を別モジュール化予定