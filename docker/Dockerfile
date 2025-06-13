FROM python:3.11-slim

WORKDIR /app

# システムの依存関係をインストール
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Pythonパッケージをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# セキュリティのため非rootユーザーを作成
RUN useradd -m -u 1000 bot

# アプリケーションコードをコピー
COPY scripts/ ./scripts/
COPY tickets/ ./tickets/

# 出力ディレクトリを作成
RUN mkdir -p output logs

# 権限を設定
RUN chown -R bot:bot /app

# ユーザー切り替え
USER bot

# Playwrightブラウザをインストール（chromiumのみ）
RUN playwright install chromium

CMD ["python", "scripts/bot_v2.py"]