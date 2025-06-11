#!/usr/bin/env python3
"""
IPAT自動投票Bot v2 - Seleniumコードを基にした実装
"""
import os
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, TimeoutError
import pandas as pd
import boto3
from botocore.exceptions import ClientError

# カスタムユーティリティ
from utils import (
    retry_async,
    take_screenshot,
    wait_and_click,
    wait_and_fill,
    safe_navigate,
    setup_file_logging
)
from slack_notifier import SlackNotifier

# 環境変数読み込み
load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ファイルログも設定
setup_file_logging('akatsuki_bot_v2')

# 定数
IPAT_URL = "https://www.ipat.jra.go.jp/"
TIMEOUT_MS = int(os.environ.get('TIMEOUT_MS', '20000'))
HEADLESS_MODE = os.environ.get('HEADLESS_MODE', 'true').lower() == 'true'


async def get_all_secrets():
    """AWS Secrets Managerから認証情報とSlack情報を取得"""
    try:
        client = boto3.client('secretsmanager', region_name=os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-1'))
        secret_id = os.environ['AWS_SECRET_NAME']
        
        response = client.get_secret_value(SecretId=secret_id)
        secrets = json.loads(response['SecretString'])
        
        # IPAT認証情報
        credentials = {
            'inet_id': secrets['jra_inet_id'],      # INET-ID（第1段階）
            'user_id': secrets['jra_user_id'],       # 加入者番号（第2段階）
            'password': secrets['jra_p_ars'],        # 暗証番号（第2段階）
            'pars': secrets.get('jra_pars', '0519')  # P-ARS番号（第2段階）- Seleniumコードから
        }
        
        # Slack情報（2つのチャンネル）
        slack_info = {
            'token': secrets['slack_bot_user_oauth_token'],
            'bets_channel_id': os.environ.get('SLACK_channel_id_bets-live', ''),  # 投票通知用
            'alerts_channel_id': os.environ.get('SLACK_channel_id_alerts', '')    # エラー通知用
        }
        
        return credentials, slack_info
        
    except ClientError as e:
        logger.error(f"Failed to retrieve secrets: {e}")
        raise
    except KeyError as e:
        logger.error(f"Missing required secret key: {e}")
        raise


async def login_ipat_v2(page: Page, credentials: dict):
    """IPAT 2段階ログイン（Seleniumコードを基に実装）"""
    try:
        logger.info("Starting IPAT login process...")
        
        # IPATサイトへアクセス
        if not await safe_navigate(page, IPAT_URL, TIMEOUT_MS):
            raise Exception("Failed to navigate to IPAT")
        
        await page.wait_for_timeout(4000)  # Seleniumと同じ待機時間
        
        # === 第1段階: INET-ID入力 ===
        logger.info("Stage 1: Entering INET-ID...")
        if not await wait_and_fill(page, 'input[name="inetid"]', credentials['inet_id']):
            raise Exception("Failed to fill INET-ID")
        
        # 次へボタンクリック（class="button"）
        if not await wait_and_click(page, '.button'):
            raise Exception("Failed to click next button")
        
        await page.wait_for_timeout(4000)
        
        # === 第2段階: 3つの認証情報入力 ===
        logger.info("Stage 2: Entering authentication details...")
        
        # 加入者番号
        if not await wait_and_fill(page, 'input[name="i"]', credentials['user_id']):
            raise Exception("Failed to fill user ID")
        
        # 暗証番号
        if not await wait_and_fill(page, 'input[name="p"]', credentials['password']):
            raise Exception("Failed to fill password")
        
        # P-ARS番号
        if credentials.get('pars'):
            if not await wait_and_fill(page, 'input[name="r"]', credentials['pars']):
                raise Exception("Failed to fill P-ARS number")
        
        # ログインボタンクリック（class="buttonModern"）
        if not await wait_and_click(page, '.buttonModern'):
            raise Exception("Failed to click login button")
        
        # === お知らせ確認画面の処理 ===
        await page.wait_for_timeout(4000)
        try:
            ok_buttons = await page.query_selector_all('button')
            for button in ok_buttons:
                text = await button.text_content()
                if text and "OK" in text:
                    logger.info("Found OK button in notification, clicking...")
                    await button.click()
                    break
        except Exception as e:
            logger.debug(f"No OK button found or error: {e}")
        
        logger.info("Successfully logged in to IPAT")
        
    except TimeoutError:
        logger.error("Login timeout - check credentials or network connection")
        await take_screenshot(page, "login_timeout_v2")
        raise
    except Exception as e:
        logger.error(f"Login failed: {e}")
        await take_screenshot(page, "login_error_v2")
        raise


async def get_balance(page: Page) -> int:
    """残高を取得"""
    try:
        logger.info("Getting account balance...")
        await page.wait_for_timeout(4000)
        
        td_elements = await page.query_selector_all('td')
        for td in td_elements:
            text = await td.text_content()
            if text and "円" in text:
                balance = int(text.replace(",", "").replace("円", ""))
                logger.info(f"Current balance: {balance} yen")
                return balance
        
        logger.warning("Could not find balance")
        return 0
        
    except Exception as e:
        logger.error(f"Failed to get balance: {e}")
        return 0


async def navigate_to_vote(page: Page):
    """投票画面へ移動"""
    try:
        logger.info("Navigating to vote page...")
        
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and "通常" in text and "投票" in text:
                await button.click()
                logger.info("Clicked normal vote button")
                await page.wait_for_timeout(4000)
                return True
        
        logger.error("Could not find vote button")
        return False
        
    except Exception as e:
        logger.error(f"Failed to navigate to vote: {e}")
        return False


async def select_race(page: Page, racecourse: str, race_number: int):
    """競馬場とレースを選択"""
    try:
        logger.info(f"Selecting race: {racecourse} R{race_number}")
        
        # 競馬場選択
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and racecourse in text:
                await button.click()
                logger.info(f"Selected racecourse: {racecourse}")
                break
        
        # レース番号選択
        race_text = f"{race_number}R"
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text:
                if len(race_text) == 2 and text[:2] == race_text:
                    await button.click()
                    logger.info(f"Selected race: {race_text}")
                    break
                elif len(race_text) == 3 and text[:3] == race_text:
                    await button.click()
                    logger.info(f"Selected race: {race_text}")
                    break
        
        await page.wait_for_timeout(4000)
        return True
        
    except Exception as e:
        logger.error(f"Failed to select race: {e}")
        return False


async def select_horse_and_bet(page: Page, horse_number: int, horse_name: str, bet_amount: int, 
                              racecourse: str, race_number: int, slack: Optional[SlackNotifier] = None):
    """馬を選択して投票"""
    try:
        logger.info(f"Selecting horse #{horse_number} {horse_name} with bet {bet_amount}")
        
        await page.wait_for_timeout(4000)
        
        # 大きい番号の場合はスクロール
        if horse_number >= 9:
            await page.evaluate("window.scrollTo(0, 300)")
            await page.wait_for_timeout(2000)
            if horse_number >= 13:
                await page.evaluate("window.scrollTo(0, 300)")
                await page.wait_for_timeout(2000)
        
        # 馬番号選択（labelタグ、インデックスで選択）
        labels = await page.query_selector_all('label')
        if len(labels) > horse_number + 8:
            await labels[horse_number + 8].click()
            logger.info(f"Selected horse number {horse_number}")
        
        await page.wait_for_timeout(2000)
        
        # セットボタン
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text == "セット":
                await button.click()
                break
        
        await page.wait_for_timeout(2000)
        
        # 入力終了ボタン
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text == "入力終了":
                await button.click()
                break
        
        await page.wait_for_timeout(4000)
        
        # 金額入力
        inputs = await page.query_selector_all('input')
        if len(inputs) > 11:
            # 投票票数
            await inputs[9].fill(str(bet_amount // 100))
            await page.wait_for_timeout(1000)
            # 賭け票数
            await inputs[10].fill(str(bet_amount // 100))
            await page.wait_for_timeout(1000)
            # 合計金額
            await inputs[11].fill(str(bet_amount))
        
        await page.wait_for_timeout(4000)
        
        # 購入直前のSlack通知
        if slack:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="開始")
        
        # 購入ボタン
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text == "購入する":
                await button.click()
                break
        
        await page.wait_for_timeout(4000)
        
        # OK確認
        success = False
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text == "OK":
                await button.click()
                logger.info(f"Successfully placed bet for {horse_name}")
                success = True
                break
        
        # 購入完了のSlack通知
        if slack and success:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="完了")
        elif slack and not success:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="失敗")
        
        return success
        
    except Exception as e:
        logger.error(f"Failed to place bet: {e}")
        await take_screenshot(page, f"bet_error_{horse_name}")
        if slack:
            await slack.send_error_notification(f"投票エラー: {horse_name}", str(e))
        return False


async def auto_deposit_v2(page: Page, amount: int, password: str, slack: Optional[SlackNotifier] = None):
    """銀行連携による自動入金（別ウィンドウ処理対応）"""
    try:
        logger.info(f"Starting auto deposit: {amount} yen")
        
        # 入金前の残高を取得
        balance_before = await get_balance(page)
        
        # 入出金ボタンを探してクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and "入出金" in text:
                await button.click()
                break
        
        await page.wait_for_timeout(4000)
        
        # 新しいウィンドウ/タブを待つ
        async with page.context.expect_page() as new_page_info:
            # 新しいページが開くのを待つ
            pass
        
        new_page = await new_page_info.value
        await new_page.wait_for_load_state()
        
        # 入金指示リンクをクリック
        links = await new_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "入金指示" in text:
                await link.click()
                break
        
        await new_page.wait_for_timeout(4000)
        
        # 入金額入力
        await new_page.fill('input[name="NYUKIN"]', str(amount))
        
        # 次へボタン
        links = await new_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "次へ" in text:
                await link.click()
                break
        
        await new_page.wait_for_timeout(4000)
        
        # パスワード入力（暗証番号を使用）
        await new_page.fill('input[name="PASS_WORD"]', password)
        
        # 実行ボタン
        links = await new_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "実行" in text:
                await link.click()
                break
        
        await new_page.wait_for_timeout(4000)
        
        # アラートの処理
        new_page.on('dialog', lambda dialog: dialog.accept())
        
        logger.info(f"Successfully deposited {amount} yen")
        await new_page.close()
        
        # 入金後の残高を取得
        await page.wait_for_timeout(4000)
        balance_after = await get_balance(page)
        
        # Slack通知
        if slack:
            await slack.send_deposit_notification(amount, balance_before, balance_after)
        
        return True
        
    except Exception as e:
        logger.error(f"Deposit failed: {e}")
        await take_screenshot(page, "deposit_error")
        if slack:
            await slack.send_error_notification("入金エラー", str(e))
        return False


async def place_bet_from_csv(page: Page, ticket: pd.Series, slack: Optional[SlackNotifier] = None):
    """CSVからの投票処理"""
    try:
        # CSVのフォーマットに合わせて調整
        racecourse = ticket.get('race_course', ticket.get('競馬場', ''))
        race_number = int(ticket.get('race_number', ticket.get('Race', 0)))
        horse_number = int(ticket.get('horse_number', ticket.get('Number', 0)))
        horse_name = ticket.get('horse_name', ticket.get('馬名', ''))
        bet_amount = int(ticket.get('amount', 100))
        
        # 投票画面へ移動
        if not await navigate_to_vote(page):
            raise Exception("Failed to navigate to vote page")
        
        # レース選択
        if not await select_race(page, racecourse, race_number):
            raise Exception("Failed to select race")
        
        # 馬選択と投票（Slack通知付き）
        if not await select_horse_and_bet(page, horse_number, horse_name, bet_amount, 
                                        racecourse, race_number, slack):
            raise Exception("Failed to place bet")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to process ticket: {e}")
        return False


async def main():
    """メイン処理"""
    slack_bets = None
    slack_alerts = None
    try:
        # Secrets Managerから認証情報とSlack情報を取得
        logger.info("Retrieving credentials from AWS Secrets Manager...")
        credentials, slack_info = await get_all_secrets()
        
        # SlackNotifierの初期化（2つのチャンネル）
        if slack_info['token']:
            if slack_info['bets_channel_id']:
                slack_bets = SlackNotifier(slack_info['token'], slack_info['bets_channel_id'])
                logger.info("Slack bets notifier initialized")
            
            if slack_info['alerts_channel_id']:
                slack_alerts = SlackNotifier(slack_info['token'], slack_info['alerts_channel_id'])
                logger.info("Slack alerts notifier initialized")
        else:
            logger.warning("Slack token not found, notifications disabled")
        
        # TODO: S3から入金額設定を取得（現在は仮実装）
        deposit_amount = 10000
        
        # 統計情報の初期化
        total_bets = 0
        total_amount = 0
        successful_bets = 0
        
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
            
            # 1) IPATログイン（2段階認証対応）
            await retry_async(login_ipat_v2, page, credentials)
            
            # 2) 残高確認
            balance = await get_balance(page)
            
            # 3) 必要に応じて入金
            if balance < deposit_amount:
                logger.info(f"Balance {balance} is less than required {deposit_amount}, depositing...")
                await retry_async(auto_deposit_v2, page, deposit_amount - balance, 
                                credentials['password'], slack_bets)
            
            # 4) tickets.csv読み込み・投票実行
            tickets_path = Path('tickets/tickets.csv')
            if tickets_path.exists():
                logger.info("Reading tickets.csv...")
                tickets_df = pd.read_csv(tickets_path, encoding='cp932')  # Shift-JIS対応
                logger.info(f"Found {len(tickets_df)} tickets to process")
                
                for idx, ticket in tickets_df.iterrows():
                    try:
                        success = await place_bet_from_csv(page, ticket, slack_bets)
                        if success:
                            successful_bets += 1
                            bet_amount = int(ticket.get('amount', 100))
                            total_amount += bet_amount
                        total_bets += 1
                        
                        # レート制限対策で待機
                        await asyncio.sleep(5)
                    except Exception as e:
                        logger.error(f"Failed to process ticket {idx+1}: {e}")
                        await take_screenshot(page, f"ticket_error_{idx+1}")
                        if slack_alerts:
                            await slack_alerts.send_error_notification(
                                f"チケット処理エラー (#{idx+1})", str(e)
                            )
                        continue
            else:
                logger.warning("No tickets.csv found, skipping betting phase")
            
            # 5) 最終残高確認
            final_balance = await get_balance(page)
            logger.info(f"Final balance: {final_balance} yen")
            
            # 6) サマリー通知
            if slack_bets and total_bets > 0:
                await slack_bets.send_summary_notification(
                    successful_bets, total_amount, final_balance
                )
            
            await browser.close()
            
    except Exception as e:
        logger.error(f"Fatal error in main process: {e}")
        if slack_alerts:
            await slack_alerts.send_error_notification("致命的エラー", str(e))
        raise


if __name__ == "__main__":
    asyncio.run(main())