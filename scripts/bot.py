#!/usr/bin/env python3
import os
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError
import pandas as pd
import boto3
from botocore.exceptions import ClientError

# 環境変数読み込み
load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 定数
IPAT_URL = "https://www.ipat.jra.go.jp/"
TIMEOUT_MS = int(os.environ.get('TIMEOUT_MS', '20000'))
HEADLESS_MODE = os.environ.get('HEADLESS_MODE', 'true').lower() == 'true'


async def get_secrets():
    """AWS Secrets Managerから認証情報を取得"""
    try:
        client = boto3.client('secretsmanager', region_name=os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-1'))
        secret_id = os.environ['AWS_SECRET_NAME']
        
        response = client.get_secret_value(SecretId=secret_id)
        secrets = json.loads(response['SecretString'])
        
        return {
            'username': secrets['ipat_username'],
            'password': secrets['ipat_password'],
            'paynavi_pass': secrets['ipat_paynavi_password']
        }
    except ClientError as e:
        logger.error(f"Failed to retrieve secrets: {e}")
        raise
    except KeyError as e:
        logger.error(f"Missing required secret key: {e}")
        raise


async def get_deposit_amount_from_s3():
    """S3から入金額設定を取得"""
    try:
        s3_client = boto3.client('s3', region_name=os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-1'))
        # TODO: S3バケット名とキーを環境変数から取得
        bucket_name = os.environ.get('S3_CONFIG_BUCKET', 'akatsuki-config')
        key = os.environ.get('S3_DEPOSIT_CONFIG_KEY', 'deposit_config.json')
        
        response = s3_client.get_object(Bucket=bucket_name, Key=key)
        config = json.loads(response['Body'].read().decode('utf-8'))
        
        return config.get('deposit_amount', 10000)
    except ClientError as e:
        logger.warning(f"Failed to retrieve deposit config from S3: {e}")
        logger.info("Using default deposit amount: 10000")
        return 10000
    except Exception as e:
        logger.error(f"Unexpected error retrieving deposit config: {e}")
        return 10000


async def login_ipat(page: Page, username: str, password: str):
    """IPATにログイン"""
    try:
        logger.info("Navigating to IPAT login page...")
        await page.goto(IPAT_URL, wait_until='networkidle', timeout=TIMEOUT_MS)
        
        # ログインフォームの入力
        logger.info("Filling login form...")
        await page.fill('input[name="userNo"]', username)
        await page.fill('input[name="password"]', password)
        
        # ログインボタンクリック
        await page.click('input[type="submit"][value="ログイン"]')
        
        # ログイン成功を確認
        await page.wait_for_selector('text=マイページ', timeout=TIMEOUT_MS)
        logger.info("Successfully logged in to IPAT")
        
    except TimeoutError:
        logger.error("Login timeout - check credentials or network connection")
        raise
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise


async def auto_deposit(page: Page, paynavi_pass: str, amount: int):
    """PAY-NAVIを使用した自動入金"""
    try:
        logger.info(f"Starting auto deposit: {amount} yen")
        
        # 入金ページへ移動
        await page.click('text=入金')
        await page.wait_for_load_state('networkidle')
        
        # PAY-NAVI選択
        await page.click('text=PAY-NAVI')
        
        # 金額入力
        await page.fill('input[name="depositAmount"]', str(amount))
        
        # 暗証番号入力
        await page.fill('input[name="payNaviPassword"]', paynavi_pass)
        
        # 確認画面へ
        await page.click('input[type="submit"][value="確認"]')
        await page.wait_for_load_state('networkidle')
        
        # 入金実行
        await page.click('input[type="submit"][value="入金する"]')
        
        # 完了確認
        await page.wait_for_selector('text=入金が完了しました', timeout=TIMEOUT_MS)
        logger.info(f"Successfully deposited {amount} yen")
        
    except Exception as e:
        logger.error(f"Deposit failed: {e}")
        raise


async def place_bet(page: Page, ticket: pd.Series):
    """馬券購入処理"""
    try:
        logger.info(f"Placing bet: {ticket['race_date']} {ticket['race_name']}")
        
        # 購入ページへ移動
        await page.click('text=投票')
        await page.wait_for_load_state('networkidle')
        
        # レース選択
        await page.fill('input[name="raceDate"]', ticket['race_date'])
        await page.select_option('select[name="raceCourse"]', ticket['race_course'])
        await page.select_option('select[name="raceNumber"]', str(ticket['race_number']))
        
        # 式別選択
        await page.select_option('select[name="betType"]', ticket['bet_type'])
        
        # 馬番号入力
        horse_numbers = ticket['horse_numbers'].split(',')
        for i, horse_num in enumerate(horse_numbers):
            await page.fill(f'input[name="horse{i+1}"]', horse_num.strip())
        
        # 金額入力
        await page.fill('input[name="amount"]', str(ticket['amount']))
        
        # 購入確認
        await page.click('input[type="submit"][value="投票内容確認"]')
        await page.wait_for_load_state('networkidle')
        
        # 購入実行
        await page.click('input[type="submit"][value="投票する"]')
        
        # 完了確認
        await page.wait_for_selector('text=投票が完了しました', timeout=TIMEOUT_MS)
        logger.info(f"Successfully placed bet for {ticket['race_name']}")
        
    except Exception as e:
        logger.error(f"Failed to place bet: {e}")
        raise


async def download_results(page: Page):
    """投票結果CSVダウンロード"""
    try:
        logger.info("Downloading betting results...")
        
        # 投票履歴ページへ
        await page.click('text=投票履歴')
        await page.wait_for_load_state('networkidle')
        
        # 本日の投票履歴を選択
        today = datetime.now().strftime('%Y%m%d')
        await page.fill('input[name="fromDate"]', today)
        await page.fill('input[name="toDate"]', today)
        await page.click('input[type="submit"][value="検索"]')
        
        # CSVダウンロード
        async with page.expect_download() as download_info:
            await page.click('text=CSVダウンロード')
        download = await download_info.value
        
        # ファイル保存
        output_path = f"output/results-{today}.csv"
        await download.save_as(output_path)
        logger.info(f"Results saved to: {output_path}")
        
        return output_path
        
    except Exception as e:
        logger.error(f"Failed to download results: {e}")
        raise


async def main():
    """メイン処理"""
    try:
        # Secrets Managerから認証情報取得
        logger.info("Retrieving credentials from AWS Secrets Manager...")
        secrets = await get_secrets()
        
        # S3から入金額設定を取得
        logger.info("Retrieving deposit amount from S3...")
        deposit_amount = await get_deposit_amount_from_s3()
        
        # Playwrightブラウザ起動
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS_MODE,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = await browser.new_context(
                accept_downloads=True,
                viewport={'width': 1280, 'height': 720}
            )
            page = await context.new_page()
            
            # 1) IPATログイン
            await login_ipat(page, secrets['username'], secrets['password'])
            
            # 2) 自動入金
            await auto_deposit(page, secrets['paynavi_pass'], deposit_amount)
            
            # 3) tickets.csv読み込み・投票実行
            tickets_path = Path('tickets/tickets.csv')
            if tickets_path.exists():
                logger.info("Reading tickets.csv...")
                tickets_df = pd.read_csv(tickets_path)
                logger.info(f"Found {len(tickets_df)} tickets to process")
                
                for idx, ticket in tickets_df.iterrows():
                    try:
                        await place_bet(page, ticket)
                        # レート制限対策で少し待機
                        await asyncio.sleep(2)
                    except Exception as e:
                        logger.error(f"Failed to process ticket {idx+1}: {e}")
                        continue
            else:
                logger.warning("No tickets.csv found, skipping betting phase")
            
            # 4) 投票結果ダウンロード
            download_path = await download_results(page)
            logger.info(f"All processing completed. Results: {download_path}")
            
            await browser.close()
            
    except Exception as e:
        logger.error(f"Fatal error in main process: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())