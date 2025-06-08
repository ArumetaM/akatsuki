# Auto‑Bet Horse Racing Bot (MVP)

> Deposit funds → purchase betting tickets → download voting results (CSV)  
> **Tech**: TypeScript + Playwright / Docker / WSL2 (local dev)  
> **Scope**: MVP without S3; purchases simulated from a local `tickets.csv`.

---

## 1. Why another repo?

* 自宅 PC の GPU を用いた予測モデルで算出した **購入指示 (tickets.csv)** を自動実行  
* 初期段階はローカルのみで完結させ、  
  * **ヘッドレスブラウザ操作** が安定する  
  * **Docker イメージ** が 1 コマンドで再現  
 する状態を作る  
* 安定後に **S3 → AWS Lambda (定期実行)** へ lift‑&‑shift

---

## 2. Architecture (MVP)

┌────────────┐ ┌─────────────┐
│tickets.csv │──────▶│Docker‑ized │
│(local) │ │Playwright Bot│
└────────────┘ └────┬────────┘
▼
Betting Website
▼
results-yyyyMMdd.csv

* **tickets.csv** … 1 行 1 購入指示  
  `<race_id>,<horse_no>,<bet_type>,<amount>`  
* **Playwright Bot** …  
  1. ログイン  
  2. 必要額を入金（Stub／手動承認で代替可能）  
  3. 指示通りに馬券を購入  
  4. 購入完了後、投票結果 CSV をダウンロード  
* 取得した CSV は `output/` に保存。後続フェーズで S3 へアップロード予定。

---

## 3. Repository Layout

.
├── docker/
│ └── Dockerfile # Node + Playwright + cron (later)
├── scripts/
│ └── bot.ts # Entry point
├── tickets/ # ← 手動配置 (MVP)
│ └── sample_tickets.csv
├── output/ # DL した CSV を格納
├── .env.example # 環境変数テンプレ
└── README.md

---

## 4. Quick Start (local WSL + Docker Compose)

```bash
# 1) まず Playwright の依存入りイメージをビルド
docker build -t auto-bet-bot -f docker/Dockerfile .

# 2) 購入指示を配置
cp path/to/your/tickets.csv tickets/

# 3) .env を作る（後述の ENV 参照）
cp .env.example .env
$EDITOR .env

# 4) 実行（ローカルブラウザ UI のデバッグ時は HEADLESS=false）
docker run --rm \
  --env-file .env \
  -v ${PWD}/tickets:/app/tickets \
  -v ${PWD}/output:/app/output \
  auto-bet-bot
ログはコンテナ標準出力に流れます。output/ に結果 CSV が落ちれば成功。

5. Environment Variables (.env)
Key	説明	例
BET_PORTAL_USERNAME	投票サイトのログイン ID	your_id
BET_PORTAL_PASSWORD	パスワード	********
HEADLESS	"true" / "false" でブラウザ表示	true
DEPOSIT_METHOD	"manual" or "auto"	manual
TIMEOUT_MS	ページ遷移の最大待機 ms	20000

将来的に AWS Secrets Manager / Parameter Store へ移行予定。

6. Implementation Guide
6.1 Dockerfile（抜粋）
Dockerfile
FROM mcr.microsoft.com/playwright:v1.44.0-jammy

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

# Run under a non‑root user for safety
RUN adduser --disabled-password bot && chown -R bot /app
USER bot

CMD ["node", "dist/bot.js"]
6.2 bot.ts 主なステップ
ts
import { chromium } from 'playwright';
import * as fs from 'node:fs/promises';
import * as path from 'node:path';

(async () => {
  const {
    BET_PORTAL_USERNAME,
    BET_PORTAL_PASSWORD,
    HEADLESS = 'true',
    TIMEOUT_MS = '20000',
  } = process.env;

  const browser = await chromium.launch({ headless: HEADLESS === 'true' });
  const context  = await browser.newContext({ acceptDownloads: true });
  const page     = await context.newPage();

  // 1) login
  await page.goto('https://example-bet.jp/login');
  await page.fill('#user', BET_PORTAL_USERNAME!);
  await page.fill('#pass', BET_PORTAL_PASSWORD!);
  await Promise.all([
    page.waitForNavigation({ timeout: +TIMEOUT_MS }),
    page.click('button[type=submit]'),
  ]);

  // 2) deposit (stub)
  //    depositFunds(page);

  // 3) iterate tickets
  const tickets = await fs.readFile('tickets/tickets.csv', 'utf-8');
  for (const t of tickets.trim().split('\n')) {
    // parse & place bets ...
  }

  // 4) DL results
  const [ download ] = await Promise.all([
    page.waitForEvent('download'),
    page.click('text=CSVダウンロード'),
  ]);
  const filePath = path.join('output', await download.suggestedFilename());
  await download.saveAs(filePath);

  await browser.close();
})();
リトライ・エラーハンドリング は別モジュール化予定

unit test → jest + Playwright test runner

7. CI / CD (optional)
GitHub Actions

docker build → docker push ghcr.io/<user>/auto-bet-bot:sha

将来: sam build で Lambda 用 ZIP を artifact 化

Playwright Test Report を PR コミットに自動添付

8. Roadmap
フェーズ	ゴール	技術トピック
MVP	ローカル Docker で自動購入 & CSV 取得	Playwright / dotenv
Phase 2	S3 連携 (tickets 取得 + results 保管)	AWS SDK v3 / S3 Gateway IAM
Phase 3	Lambda 化＋EventBridge cron (例: 毎 Raceday 08:55)	Lambda container images / AWS SAM
Phase 4	セキュリティ & 運用	SSM Parameter Store / CloudWatch Logs / Alarm
Phase 5	モデル & 資金管理自動フィードバック	DynamoDB / Step Functions / SageMaker

9. Contributing
devcontainer.json で VS Code Remote Containers に対応

npm run format (prettier) + npm run lint (eslint) が CI Gate

Issue / PR テンプレートに 再現手順 と 期待結果 を記載

10. License
Apache‑2.0
© 2025 Your Name

---

### 使い方のポイント

* **“最初は動く最小構成”** を徹底  
  - S3 を見据えていても **ローカルファイル** から始める  
  - 入金 API が無い場合は Stub⇢手動確認に切り替えやすい設計に  
* Playwright の selector は **公式レース投票サイト** を実際に操作してから `page.getByRole()` 系を優先  
* コンテナ化によって **WSL2 / macOS / CI** でも同一挙動  
* Lambda 移行時は  
  - **`aws lambda build` 時の `--no-cache`** で chromium‑ffmpeg layer サイズ削減  
  - `context.download` → `/tmp` 保存 → S3 putObject に変更

---

これをベースに **AI 生成コード** を行う場合は、上記ステップごとに「タスクカード」を切り出し、GitHub Copilot Chat / GPT‑4o などへ以下のようにプロンプトすると効率的です。

「docker/Dockerfile を上記 README の仕様で作って」
「playwright でログイン〜CSV ダウンロードのスクリプトを書いて。入力は tickets/tickets.csv 」
