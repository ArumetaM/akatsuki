# akatsuki Lambda用 Dockerfile
# Playwright + Chromium を AWS Lambda コンテナイメージに同梱
#
# 使用方法:
#   docker build -t akatsuki-purchase:latest -f lambda/Dockerfile .
#   docker push xxxx.dkr.ecr.ap-northeast-1.amazonaws.com/akatsuki-purchase:latest
#
# アプローチ: Microsoft公式Playwrightイメージ + Lambda Runtime Interface Client
# PlaywrightのGLIBC要件を満たすDebian/Ubuntuベースを使用

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Lambda Runtime Interface Client の依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Lambda作業ディレクトリ
WORKDIR /var/task

# Lambda環境変数
ENV HOME=/tmp
ENV PYTHONUNBUFFERED=1
# Microsoft公式Playwrightイメージのブラウザパス（/ms-playwright固定）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Python依存関係のインストール
COPY lambda/requirements.txt ./
RUN pip install --no-cache-dir \
    boto3 \
    pandas \
    aiohttp \
    python-dotenv \
    "numpy<2.0" \
    playwright==1.44.0 \
    awslambdaric \
    requests

# Playwrightブラウザはベースイメージに含まれている
# 追加インストール不要

# アプリケーションコードをコピー
COPY scripts/ ./scripts/
COPY lambda/purchase_handler.py ./
COPY lambda/slack_service.py ./

# Lambda実行ユーザー(sbx_user1051)が読み取れるようパーミッションを修正
# 755 for directories, 644 for files
RUN chmod -R 755 . && \
    find . -type f -name "*.py" -exec chmod 644 {} \;

# 出力ディレクトリを作成（/tmp配下とカレントディレクトリ両方）
# bot_simple.py がカレント配下の output/ に書き込もうとするため
RUN mkdir -p /tmp/output /tmp/logs /tmp/screenshots /var/task/output && \
    chmod 777 /var/task/output

# Lambda Runtime Interface Client のエントリーポイント
# AWS Lambda環境では自動的にRICが使用される
ENTRYPOINT [ "python", "-m", "awslambdaric" ]
CMD ["purchase_handler.handler"]
