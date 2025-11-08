#!/usr/bin/env python3
"""
IPAT自動投票Bot - Seleniumコードベースのシンプル実装
"""
import os
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
import pandas as pd
import boto3
import json
from botocore.exceptions import ClientError
import logging

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


async def get_all_secrets():
    """AWS Secrets Managerから認証情報を取得"""
    try:
        client = boto3.client('secretsmanager', region_name=os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-1'))
        secret_id = os.environ['AWS_SECRET_NAME']

        response = client.get_secret_value(SecretId=secret_id)
        secrets = json.loads(response['SecretString'])

        credentials = {
            'inet_id': secrets.get('jra_inet_id', ''),  # INET-ID（第1段階）- 使わない可能性あり
            'user_id': secrets['jra_user_id'],          # 加入者番号（第2段階）
            'password': secrets['jra_password'],        # 暗証番号（第2段階）
            'pars': secrets['jra_p_ars']                # P-ARS番号（第2段階）
        }

        # 認証情報の桁数を確認（実際の値は表示しない）
        logger.info("=== 認証情報の桁数確認 ===")
        logger.info(f"INET-ID: {len(credentials['inet_id'])}桁")
        logger.info(f"加入者番号 (User ID): {len(credentials['user_id'])}桁")
        logger.info(f"暗証番号 (Password): {len(credentials['password'])}桁")
        logger.info(f"P-ARS番号: {len(credentials['pars'])}桁")
        logger.info(f"AWS Secrets Managerから取得: はい")
        logger.info(f"Secret ID: {secret_id}")
        logger.info("========================")

        slack_info = {
            'token': secrets.get('slack_bot_user_oauth_token', ''),
            'bets_channel_id': os.environ.get('SLACK_channel_id_bets-live', ''),
            'alerts_channel_id': os.environ.get('SLACK_channel_id_alerts', '')
        }

        return credentials, slack_info

    except (ClientError, KeyError) as e:
        logger.error(f"Failed to retrieve secrets: {e}")
        raise


async def take_screenshot(page: Page, name: str):
    """スクリーンショットを保存"""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"output/screenshots/{name}_{timestamp}.png"
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=filename)
        logger.info(f"Screenshot saved: {filename}")
    except Exception as e:
        logger.warning(f"Failed to save screenshot: {e}")


async def deposit(page: Page, credentials: dict):
    """入金処理（Seleniumコードベース）"""
    try:
        deposit_amount = int(os.environ.get('DEPOSIT_AMOUNT', '10000'))
        logger.info(f"💸 Starting deposit process: {deposit_amount}円")

        # "入出金"ボタンを探してクリック
        buttons = await page.query_selector_all('button')
        found_deposit_button = False
        for button in buttons:
            text = await button.text_content()
            if text and "入出金" in text:
                logger.info("✓ Found '入出金' button")

                # 新しいウィンドウが開くのを待つ
                async with page.expect_popup() as popup_info:
                    await button.click()
                deposit_page = await popup_info.value
                found_deposit_button = True
                break

        if not found_deposit_button:
            logger.error("❌ '入出金' button not found")
            return False

        await deposit_page.wait_for_timeout(4000)
        logger.info(f"✓ Deposit window opened: {deposit_page.url}")

        # "入金指示"リンクをクリック
        links = await deposit_page.query_selector_all('a')
        found_deposit_link = False
        for link in links:
            text = await link.text_content()
            if text and "入金指示" in text:
                logger.info("✓ Found '入金指示' link")
                await link.click()
                found_deposit_link = True
                break

        if not found_deposit_link:
            logger.error("❌ '入金指示' link not found")
            await deposit_page.close()
            return False

        await deposit_page.wait_for_timeout(4000)

        # 金額を入力
        await deposit_page.fill('input[name="NYUKIN"]', str(deposit_amount))
        logger.info(f"✓ Deposit amount entered: {deposit_amount}円")

        # "次へ"をクリック
        links = await deposit_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "次へ" in text:
                logger.info("✓ Clicking '次へ'")
                await link.click()
                break

        await deposit_page.wait_for_timeout(4000)

        # パスワード（暗証番号）を入力
        await deposit_page.fill('input[name="PASS_WORD"]', credentials['password'])
        logger.info("✓ Password entered for deposit")

        # "実行"をクリック
        links = await deposit_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "実行" in text:
                logger.info("✓ Clicking '実行'")
                await link.click()
                break

        await deposit_page.wait_for_timeout(4000)

        # アラートを承認
        try:
            deposit_page.on('dialog', lambda dialog: dialog.accept())
            await deposit_page.wait_for_timeout(2000)
            logger.info("✓ Alert accepted")
        except Exception as e:
            logger.debug(f"No alert or already handled: {e}")

        await deposit_page.wait_for_timeout(4000)
        await take_screenshot(deposit_page, "deposit_complete")

        # 入金ウィンドウを閉じる
        await deposit_page.close()
        logger.info("✅ Deposit completed successfully")

        # メインページで残高が更新されるまで待つ
        await page.wait_for_timeout(5000)

        return True

    except Exception as e:
        logger.error(f"❌ Deposit failed: {e}")
        await take_screenshot(page, "deposit_error")
        return False


async def login_simple(page: Page, credentials: dict):
    """Seleniumコードベースのシンプルなログイン"""
    try:
        logger.info("🔐 Starting simple IPAT login...")

        # ログイン画面の表示（PC版 - 2段階ログイン）
        await page.goto(IPAT_URL)
        await page.wait_for_timeout(4000)

        # ========== 第1段階: INET-ID入力 ==========
        logger.info("🔐 Stage 1: INET-ID login")
        await page.fill('input[name="inetid"]', credentials['inet_id'])
        logger.info("✓ INET-ID entered")

        # 次の画面への遷移
        await page.click('.button')
        await page.wait_for_timeout(4000)
        logger.info("✓ Stage 1 button clicked")
        await take_screenshot(page, "after_stage1")

        # ========== 第2段階: 加入者番号、暗証番号、P-ARS番号入力 ==========
        logger.info("🔐 Stage 2: User credentials")

        # 加入者番号の入力
        await page.fill('input[name="i"]', credentials['user_id'])
        logger.info("✓ User ID entered")

        # 暗証番号の入力
        await page.fill('input[name="p"]', credentials['password'])
        logger.info("✓ Password entered")

        # P-ARS番号の入力
        await page.fill('input[name="r"]', credentials['pars'])
        logger.info("✓ P-ARS entered")

        await page.wait_for_timeout(2000)

        # 次の画面への遷移 - .buttonModernをクリック
        button_modern = await page.wait_for_selector('.buttonModern', timeout=5000)
        logger.info("✓ Found .buttonModern element")

        await button_modern.click(force=True)
        await page.wait_for_timeout(8000)
        logger.info(f"✓ Stage 2 button clicked, current URL: {page.url}")

        # エラーメッセージの確認
        page_text = await page.evaluate("document.body.innerText")
        if "エラー" in page_text or "入力してください" in page_text or "正しく" in page_text:
            logger.error(f"Error message detected: {page_text[:1000]}")
            # HTMLも保存
            html = await page.content()
            with open("output/error_page.html", "w", encoding="utf-8") as f:
                f.write(html)
            logger.error("HTML saved to output/error_page.html")

        await take_screenshot(page, "after_stage2")

        # お知らせなどの確認画面の判定(OKがあればOKをクリック)
        try:
            await page.wait_for_timeout(4000)
            buttons = await page.query_selector_all('button')
            for button in buttons:
                text = await button.text_content()
                if text and "OK" in text:
                    await button.click()
                    logger.info("✓ OK button clicked")
                    await page.wait_for_timeout(4000)
                    break
        except Exception as e:
            logger.debug(f"No OK button found (normal): {e}")

        # メインフレームの読み込みを待つ
        await page.wait_for_timeout(6000)

        # ログイン成功/失敗の判定
        page_text = await page.evaluate("document.body.innerText")

        # ログインフォームが再表示されている場合はログイン失敗
        if "加入者番号" in page_text and "暗証番号" in page_text and "P-ARS番号" in page_text:
            logger.error("❌ ログイン失敗: ログインフォームが再表示されています")
            logger.error("以下のいずれかの可能性があります:")
            logger.error("  1. アカウントがロックされている")
            logger.error("  2. 認証情報が間違っている")
            logger.error("  3. システムエラー")
            logger.error("")
            logger.error("JRA IPATサポートセンターに連絡してアカウント状況を確認してください")
            await take_screenshot(page, "login_failed")
            raise Exception("Login failed: Login form was displayed again after submission")

        logger.info("✓ ログインフォームは表示されていません - ログイン処理は正常に進んでいます")

        # フレームの確認と切り替え
        logger.info(f"Checking frames... total: {len(page.frames)}")
        main_frame = None
        for i, frame in enumerate(page.frames):
            try:
                frame_url = frame.url
                logger.info(f"Frame {i}: {frame_url}")
                # メインフレームを探す（通常、/cgi-bin/ を含むURLがメインコンテンツ）
                if "/cgi-bin/" in frame_url or "main" in frame_url.lower():
                    main_frame = frame
                    logger.info(f"Found main frame: {frame_url}")
                    break
            except Exception as e:
                logger.debug(f"Error checking frame {i}: {e}")

        # メインフレームが見つからなければメインページを使用
        if not main_frame:
            logger.info("No main frame found, using main page")
            main_frame = page
        else:
            # メインフレームに切り替わるまで待つ
            await page.wait_for_timeout(3000)

        # 残高確認（メインフレーム内で）
        # まずページ全体のHTMLを保存してデバッグ
        html_content = await page.content()
        with open("output/login_after_page.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info("✓ HTML saved for debugging: output/login_after_page.html")

        # ページの全テキストを確認
        body_text = await page.evaluate("document.body.innerText")
        logger.info(f"Page text (first 500 chars): {body_text[:500]}")

        max_retries = 5
        balance = None
        for i in range(max_retries):
            tds = await main_frame.query_selector_all('td')
            logger.info(f"Found {len(tds)} td elements in frame")

            # デバッグ: 最初の試行でtd要素のテキストをログ出力
            if i == 0:
                for idx, td in enumerate(tds[:15]):  # 最初の15個
                    text = await td.text_content()
                    logger.info(f"  TD[{idx}]: '{text.strip() if text else ''}'")

            # td要素で残高を探す
            for td in tds:
                text = await td.text_content()
                if text and "円" in text:
                    logger.info(f"✓ Balance found: {text.strip()}")
                    # 残高を数値として抽出
                    try:
                        balance = int(text.replace(",", "").replace("円", "").strip())
                        logger.info(f"💰 Current balance: {balance}円")
                    except:
                        pass
                    break

            # td要素で見つからない場合は、ページ全体のテキストから"円"を含む部分を探す
            if balance is None and "円" in body_text:
                logger.info("Trying to find balance in body text...")
                import re
                # 数字とカンマと円のパターンを探す
                matches = re.findall(r'(\d{1,3}(?:,\d{3})*)\s*円', body_text)
                if matches:
                    logger.info(f"Found {len(matches)} potential balance values: {matches}")
                    # 最初の値を残高として使用
                    try:
                        balance = int(matches[0].replace(",", ""))
                        logger.info(f"💰 Current balance (from text): {balance}円")
                    except:
                        pass

            if balance is not None:
                break
            logger.info(f"Waiting for balance... ({i+1}/{max_retries})")
            await page.wait_for_timeout(3000)

        await page.wait_for_timeout(2000)
        await take_screenshot(page, "login_complete")
        logger.info("✅ Login completed successfully")

        # 残高が0円の場合は入金処理を実行
        if balance is not None and balance == 0:
            logger.info("💸 Balance is 0, starting deposit process...")
            await deposit(page, credentials)

        return True

    except Exception as e:
        logger.error(f"❌ Login failed: {e}")
        await take_screenshot(page, "login_error")
        raise


async def navigate_to_vote_simple(page: Page):
    """投票画面へ移動（シンプル版）"""
    try:
        logger.info("📋 Navigating to vote page...")

        # ページが完全に読み込まれるまで待つ
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "before_vote_navigation")

        # ページのHTMLをデバッグ出力
        page_content = await page.content()
        logger.info(f"Page content length: {len(page_content)}")

        # すべてのボタンをデバッグ出力
        buttons = await page.query_selector_all('button')
        logger.info(f"Found {len(buttons)} buttons on page")
        for i, button in enumerate(buttons[:10]):  # 最初の10個を表示
            text = await button.text_content()
            logger.info(f"Button {i}: '{text.strip() if text else ''}'")

        # "通常"と"投票"を含むボタンを探す
        for button in buttons:
            text = await button.text_content()
            if text and "通常" in text and "投票" in text:
                await button.click()
                logger.info(f"✓ Clicked vote button: {text.strip()}")
                await page.wait_for_timeout(4000)
                await take_screenshot(page, "vote_page")
                return True

        # フレームをチェック
        frames = page.frames
        logger.info(f"Checking {len(frames)} frames")
        for i, frame in enumerate(frames):
            try:
                frame_buttons = await frame.query_selector_all('button')
                logger.info(f"Frame {i} has {len(frame_buttons)} buttons")
                for button in frame_buttons:
                    text = await button.text_content()
                    if text and "通常" in text and "投票" in text:
                        await button.click()
                        logger.info(f"✓ Clicked vote button in frame {i}: {text.strip()}")
                        await page.wait_for_timeout(4000)
                        await take_screenshot(page, "vote_page")
                        return True
            except Exception as e:
                logger.debug(f"Frame {i} error: {e}")

        logger.error("❌ Vote button not found")
        await take_screenshot(page, "vote_button_not_found")
        return False

    except Exception as e:
        logger.error(f"Failed to navigate to vote: {e}")
        return False


async def select_race_simple(page: Page, racecourse: str, race_number: int):
    """競馬場とレースを選択（シンプル版）"""
    try:
        logger.info(f"🏇 Selecting {racecourse} R{race_number}...")

        # 曜日を取得
        dt_now = datetime.now().weekday()
        weekday_list = ["月", "火", "水", "木", "金", "土", "日"]
        field_name = f"{racecourse}（{weekday_list[dt_now]}）"

        # 競馬場の選択
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and field_name in text:
                await button.click()
                logger.info(f"✓ Selected racecourse: {field_name}")
                break

        await page.wait_for_timeout(2000)

        # レースの選択
        race_text = f"{race_number}R"
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text:
                # "10R"の場合は text[0:3] == "10R"
                # "9R"の場合は text[0:2] == "9R"
                if (len(race_text) == 2 and text[0:2] == race_text) or \
                   (len(race_text) == 3 and text[0:3] == race_text):
                    await button.click()
                    logger.info(f"✓ Selected race: {race_text}")
                    break

        await page.wait_for_timeout(4000)
        await take_screenshot(page, f"race_selected_{racecourse}_{race_number}")
        return True

    except Exception as e:
        logger.error(f"Failed to select race: {e}")
        return False


async def select_horse_and_bet_simple(page: Page, horse_number: int, horse_name: str, bet_amount: int):
    """馬を選択して投票（シンプル版）"""
    try:
        logger.info(f"🎯 Selecting horse #{horse_number} {horse_name}, bet {bet_amount} yen...")

        await page.wait_for_timeout(4000)

        # スクロール（大きい番号の場合）
        if horse_number >= 9:
            logger.info("Scrolling for larger horse numbers...")
            await page.evaluate("window.scrollTo(0, 300);")
            await page.wait_for_timeout(2000)
            if horse_number >= 13:
                await page.evaluate("window.scrollTo(0, 300);")
                await page.wait_for_timeout(2000)

        # 馬番から買う馬券を選択
        labels = await page.query_selector_all('label')
        # Seleniumコードでは cnt=0から始めて、number+8でクリック
        if len(labels) > horse_number + 8:
            await labels[horse_number + 8].click()
            logger.info(f"✓ Horse #{horse_number} selected")
        else:
            raise Exception(f"Not enough labels found: {len(labels)} < {horse_number + 8}")

        await page.wait_for_timeout(2000)

        # セットのクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "セット":
                await button.click()
                logger.info("✓ 'Set' button clicked")
                break

        await page.wait_for_timeout(2000)

        # 入力終了のクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "入力終了":
                await button.click()
                logger.info("✓ 'Input End' button clicked")
                break

        await page.wait_for_timeout(4000)
        await take_screenshot(page, "before_amount_input")

        # 購入直前の投票票数の入力
        inputs = await page.query_selector_all('input')
        bet_units = bet_amount // 100

        await inputs[9].fill(str(bet_units))
        await page.wait_for_timeout(1000)
        await inputs[10].fill(str(bet_units))
        await page.wait_for_timeout(1000)
        await inputs[11].fill(str(bet_amount))
        logger.info(f"✓ Bet amount entered: {bet_amount} yen")

        await page.wait_for_timeout(4000)
        await take_screenshot(page, "before_purchase")

        # 購入ボタン
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "購入する":
                await button.click()
                logger.info("✓ 'Purchase' button clicked")
                break

        await page.wait_for_timeout(4000)

        # OKボタン
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "OK":
                await button.click()
                logger.info(f"✅ Purchase successful: {horse_name} - {bet_amount} yen")
                await take_screenshot(page, "purchase_success")
                return True

        logger.warning("OK button not found, but purchase may have succeeded")
        return True

    except Exception as e:
        logger.error(f"Failed to place bet: {e}")
        await take_screenshot(page, "bet_error")
        return False


async def main():
    """メイン処理"""
    try:
        logger.info("🚀 STARTING AKATSUKI BOT - SIMPLE VERSION")

        # 認証情報取得
        credentials, slack_info = await get_all_secrets()

        # tickets.csv読み込み
        tickets_path = Path('tickets/tickets.csv')
        if not tickets_path.exists():
            logger.error("tickets.csv not found")
            return

        tickets_df = pd.read_csv(tickets_path)
        logger.info(f"📄 Found {len(tickets_df)} tickets to process")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )

            # セッション情報の復元を試みる
            session_path = "output/session.json"
            session_exists = Path(session_path).exists()

            if session_exists:
                logger.info("🔄 Restoring session from saved state...")
                try:
                    context = await browser.new_context(
                        storage_state=session_path,
                        viewport={'width': 1280, 'height': 720}
                    )
                    logger.info("✓ Session restored successfully")
                except Exception as e:
                    logger.warning(f"Failed to restore session: {e}")
                    logger.info("Will proceed with fresh login...")
                    context = await browser.new_context(
                        viewport={'width': 1280, 'height': 720}
                    )
                    session_exists = False
            else:
                logger.info("📝 No saved session found, will login normally")
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 720}
                )

            page = await context.new_page()

            # セッションが無い場合のみログイン
            if not session_exists:
                await login_simple(page, credentials)

                # ログイン成功後、セッション情報を保存
                logger.info("💾 Saving session state...")
                Path(session_path).parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=session_path)
                logger.info(f"✓ Session saved to {session_path}")
            else:
                # セッションを使う場合でも、ログイン状態を確認
                await page.goto(IPAT_URL)
                await page.wait_for_timeout(3000)
                page_text = await page.evaluate("document.body.innerText")

                # ログインフォームが表示されている場合はセッション期限切れ
                if "INET-ID" in page_text or "加入者番号" in page_text:
                    logger.warning("⚠️ Session expired, logging in again...")
                    await login_simple(page, credentials)
                    await context.storage_state(path=session_path)
                    logger.info("✓ Session refreshed")
                else:
                    logger.info("✓ Session is still valid")

            # 各チケットを処理
            for idx, ticket in tickets_df.iterrows():
                try:
                    racecourse = ticket['race_course']
                    race_number = int(ticket['race_number'])
                    horse_number = int(ticket['horse_number'])
                    horse_name = ticket['horse_name']
                    bet_amount = int(ticket['amount'])

                    logger.info(f"\n{'='*60}")
                    logger.info(f"🎫 Ticket {idx+1}/{len(tickets_df)}")
                    logger.info(f"   {racecourse} R{race_number} - #{horse_number} {horse_name} - ¥{bet_amount}")
                    logger.info(f"{'='*60}")

                    # 投票画面へ移動
                    if not await navigate_to_vote_simple(page):
                        logger.error("Failed to navigate to vote page")
                        continue

                    # レース選択
                    if not await select_race_simple(page, racecourse, race_number):
                        logger.error("Failed to select race")
                        continue

                    # 馬選択と投票
                    if await select_horse_and_bet_simple(page, horse_number, horse_name, bet_amount):
                        logger.info(f"✅ Ticket {idx+1} completed successfully")
                    else:
                        logger.error(f"❌ Ticket {idx+1} failed")

                    # 次のチケットのため少し待機
                    await page.wait_for_timeout(5000)

                except Exception as e:
                    logger.error(f"Error processing ticket {idx+1}: {e}")
                    continue

            logger.info("\n🏁 All tickets processed")
            await browser.close()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
