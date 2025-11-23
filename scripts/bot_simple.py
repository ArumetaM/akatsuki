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
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum

# 定数のインポート
from constants import Timeouts, UIIndices, URLs, Config

# ユーティリティのインポート
from page_navigator import PageNavigator

# 環境変数読み込み
load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 定数（後方互換性のため残す）
IPAT_URL = URLs.IPAT_BASE
IPAT_HOME_URL = URLs.IPAT_HOME


# ========================================
# データ構造（冪等性対応）
# ========================================

class TicketStatus(Enum):
    """チケットの状態"""
    ALREADY_PURCHASED = "already_purchased"      # 重複で購入しない
    NOT_PURCHASED = "not_purchased"              # 未購入（購入対象）
    SKIPPED_DRY_RUN = "skipped_dry_run"         # DRY_RUNでスキップ
    PURCHASE_SUCCESS = "purchase_success"        # 購入成功
    PURCHASE_FAILED = "purchase_failed"          # 購入失敗


@dataclass
class ExistingBet:
    """既存の投票データ（投票内容照会から取得）"""
    receipt_number: str      # 受付番号 (e.g., "0001")
    racecourse: str          # 競馬場 (e.g., "東京")
    race_number: int         # レース番号 (e.g., 8)
    bet_type: str            # 券種 (e.g., "単勝", "複勝", "馬連")
    horse_number: int        # 馬番 (e.g., 13)
    amount: int              # 金額 (e.g., 5000)

    def __str__(self):
        return f"{self.racecourse} {self.race_number}R - {self.bet_type} {self.horse_number}番 {self.amount:,}円 (receipt: {self.receipt_number})"


@dataclass
class Ticket:
    """tickets.csvから読み込んだ投票指示"""
    racecourse: str          # race_course column
    race_number: int         # race_number column
    bet_type: str            # bet_type column (default: "単勝")
    horse_number: int        # horse_number column
    horse_name: str          # horse_name column
    amount: int              # amount column

    def matches(self, existing_bet: ExistingBet) -> bool:
        """既存の投票と一致するかチェック"""
        return (
            self.racecourse == existing_bet.racecourse and
            self.race_number == existing_bet.race_number and
            self.bet_type == existing_bet.bet_type and
            self.horse_number == existing_bet.horse_number and
            self.amount == existing_bet.amount
        )

    def __str__(self):
        return f"{self.racecourse} {self.race_number}R - {self.horse_number}番 {self.horse_name} {self.amount:,}円"


@dataclass
class ReconciliationResult:
    """突合結果"""
    ticket: Ticket
    status: TicketStatus
    existing_bet: Optional[ExistingBet] = None
    error_message: Optional[str] = None


# ========================================
# ヘルパー関数
# ========================================

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


async def navigate_to_bet_history_page(page: Page, navigator: PageNavigator, date_type: str) -> bool:
    """投票履歴ページへ遷移"""
    try:
        # メインメニューに戻る
        await page.goto(IPAT_HOME_URL)
        await page.wait_for_timeout(Timeouts.NAVIGATION)

        # 「投票履歴」ボタンをクリック
        await page.wait_for_timeout(Timeouts.MEDIUM)

        # ページのテキストを取得してデバッグ
        body_text = await page.evaluate("document.body.innerText")
        logger.info(f"Page text (first 500 chars): {body_text[:500]}")

        # PageNavigatorを使用してボタンをクリック
        履歴_found = "投票履歴" in body_text and await navigator.find_and_click_by_text(
            "投票履歴",
            element_types=['button', 'a', 'div[role="button"]']
        )

        if not 履歴_found:
            logger.warning("⚠️ Could not find 投票履歴 button, will try alternative approach")
            await take_screenshot(page, "投票履歴_not_found")
            return False

        await page.wait_for_timeout(Timeouts.NAVIGATION)

        # 「投票内容照会（当日分/前日分）」を選択
        if date_type == "same_day":
            logger.info("Selecting 当日分...")
            await navigator.find_and_click_by_text(
                "当日",
                element_types=['button', 'a', 'div[role="button"]', 'label']
            )
        else:
            logger.info("Selecting 前日分...")
            await navigator.find_and_click_by_text(
                "前日",
                element_types=['button', 'a', 'div[role="button"]', 'label']
            )

        await page.wait_for_timeout(Timeouts.NAVIGATION)
        return True
    except Exception as e:
        logger.error(f"❌ Failed to navigate to bet history: {e}")
        await take_screenshot(page, "bet_history_nav_error")
        return False


async def get_bet_receipt_links(page: Page) -> int:
    """投票履歴ページから受付番号リンク数を取得"""
    try:
        # まずページのHTMLを保存してデバッグ
        try:
            html_content = await page.content()
            with open("output/bet_history_page.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info("✓ HTML saved: output/bet_history_page.html")
        except Exception as e:
            logger.warning(f"Failed to save HTML: {e}")

        # 受付番号リンクを取得
        receipt_links = await page.query_selector_all('.bet-refer-list a[ng-click*="showBetReferDetail"]')
        total_receipts = len(receipt_links)
        logger.info(f"Found {total_receipts} receipt links")

        return total_receipts
    except Exception as e:
        logger.error(f"❌ Failed to get receipt links: {e}")
        return 0


async def open_receipt_detail_view(page: Page, idx: int, total_receipts: int) -> Optional[str]:
    """
    受付番号詳細ビューを開く

    Args:
        page: Playwright page
        idx: 受付番号のインデックス
        total_receipts: 全受付番号数

    Returns:
        受付番号文字列（失敗時はNone）
    """
    # 毎回リンクを再取得（DOM変更による陳腐化を防ぐ）
    receipt_links = await page.query_selector_all('.bet-refer-list a[ng-click*="showBetReferDetail"]')
    if idx >= len(receipt_links):
        logger.warning(f"⚠️ Receipt {idx} no longer available, skipping")
        return None

    link = receipt_links[idx]
    receipt_num = await link.text_content()
    receipt_num = receipt_num.strip()
    logger.info(f"📄 Checking receipt {idx+1}/{total_receipts}: {receipt_num}")

    # 詳細ビューを開く
    await link.click()
    await page.wait_for_timeout(Timeouts.MEDIUM)

    # 詳細ビューが完全に表示されるまで待つ
    try:
        await page.wait_for_selector('.bet-refer-result', state='visible', timeout=Timeouts.SELECTOR_WAIT)
    except:
        logger.warning("   ⚠️ Detail view not fully loaded")

    return receipt_num


async def extract_horse_number(page: Page, html_content: str) -> Optional[int]:
    """
    馬番を複数の方法で抽出

    Args:
        page: Playwright page
        html_content: ページのHTMLコンテンツ

    Returns:
        馬番（失敗時はNone）
    """
    import re
    horse_number = None

    # Method 1: CSS selector (推奨)
    try:
        horse_elem = await page.query_selector('.horse-combi .set-heading')
        if horse_elem:
            horse_text = await horse_elem.text_content()
            horse_number = int(horse_text.strip())
            logger.debug(f"   Horse number from CSS: {horse_number}")
            return horse_number
    except Exception as e:
        logger.debug(f"   CSS selector failed: {e}")

    # Method 2: Regex fallback on HTML content
    horse_match = re.search(r'class="set-heading[^"]*"[^>]*>\s*(\d+)\s*</span>', html_content)
    if horse_match:
        horse_number = int(horse_match.group(1))
        logger.debug(f"   Horse number from regex: {horse_number}")
        return horse_number

    # Method 3: Print version fallback on HTML content
    horse_match = re.search(r'ng-switch-when="\d+"[^>]*>\s*(\d+)\s*</span>', html_content)
    if horse_match:
        horse_number = int(horse_match.group(1))
        logger.debug(f"   Horse number from print version: {horse_number}")
        return horse_number

    # Method 4: Simple pattern in text - look for 馬番 in isolation
    horse_match = re.search(r'ng-bind="vm\.header\.horse\d+">(\d+)</span>', html_content)
    if horse_match:
        horse_number = int(horse_match.group(1))
        logger.debug(f"   Horse number from ng-bind pattern: {horse_number}")
        return horse_number

    return None


async def extract_bet_info_from_page(page: Page, idx: int) -> dict:
    """
    ページから馬券情報を抽出

    Args:
        page: Playwright page
        idx: 受付番号のインデックス

    Returns:
        馬券情報の辞書
    """
    import re

    # 詳細ビューのHTMLを解析
    html_content = await page.content()
    page_text = await page.text_content('body')

    # 最初のレコードのために詳細ビューのHTMLを保存（デバッグ用）
    if idx == 0:
        with open('output/bet_detail_first.html', 'w', encoding='utf-8') as f:
            f.write(html_content)
        await take_screenshot(page, "bet_detail_first")
        logger.info("✓ Saved first bet detail HTML for debugging")

    # テキストから情報を抽出
    # 1. レース場
    racecourse_match = re.search(r'(東京|京都|阪神|中山|小倉|福島|新潟|札幌|函館|中京)', page_text)
    racecourse = racecourse_match.group(1) if racecourse_match else None

    # 2. レース番号
    race_num_match = re.search(r'(\d+)R', page_text)
    race_number = int(race_num_match.group(1)) if race_num_match else None

    # 3. 式別
    bet_type_match = re.search(r'(単勝|複勝|馬連|馬単|ワイド|三連複|三連単)', page_text)
    bet_type = bet_type_match.group(1) if bet_type_match else None

    # 4. 金額
    amount_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*円', page_text)
    amount = int(amount_match.group(1).replace(',', '')) if amount_match else None

    # 5. 馬番
    horse_number = await extract_horse_number(page, html_content)

    return {
        'racecourse': racecourse,
        'race_number': race_number,
        'bet_type': bet_type,
        'horse_number': horse_number,
        'amount': amount
    }


async def close_receipt_detail_view(page: Page):
    """
    詳細ビューを閉じて一覧に戻る

    Args:
        page: Playwright page
    """
    back_button = await page.query_selector('button[ng-click="vm.closeBetReferDetail()"]')
    if back_button:
        await back_button.click()
        await page.wait_for_timeout(Timeouts.SHORT)
    else:
        logger.warning("⚠️ Could not find back button, trying close button")
        close_button = await page.query_selector('button[ng-click="vm.close()"]')
        if close_button:
            await close_button.click()
            await page.wait_for_timeout(Timeouts.SHORT)


async def parse_bet_receipt_detail(page: Page, idx: int, total_receipts: int) -> Optional[ExistingBet]:
    """
    1件の受付番号詳細を解析してExistingBetを返す

    Args:
        page: Playwright page
        idx: 受付番号のインデックス
        total_receipts: 全受付番号数

    Returns:
        ExistingBet（失敗時はNone）
    """
    try:
        # 1. 詳細ビューを開く
        receipt_num = await open_receipt_detail_view(page, idx, total_receipts)
        if receipt_num is None:
            return None

        # 2. 馬券情報を抽出
        bet_info = await extract_bet_info_from_page(page, idx)

        # 3. すべてのフィールドが取得できたか確認
        if all([bet_info['racecourse'], bet_info['race_number'], bet_info['bet_type'],
                bet_info['horse_number'], bet_info['amount']]):
            existing_bet = ExistingBet(
                receipt_number=receipt_num,
                racecourse=bet_info['racecourse'],
                race_number=bet_info['race_number'],
                bet_type=bet_info['bet_type'],
                horse_number=bet_info['horse_number'],
                amount=bet_info['amount']
            )
            logger.info(f"   ✓ Parsed: {bet_info['racecourse']} {bet_info['race_number']}R {bet_info['bet_type']} {bet_info['horse_number']}番 {bet_info['amount']}円")

            # 4. 詳細ビューを閉じる
            await close_receipt_detail_view(page)
            return existing_bet
        else:
            logger.warning(f"   ⚠️ Could not parse all fields")
            logger.warning(f"      racecourse={bet_info['racecourse']}, race={bet_info['race_number']}, type={bet_info['bet_type']}, horse={bet_info['horse_number']}, amount={bet_info['amount']}")
            await close_receipt_detail_view(page)
            return None

    except Exception as e:
        logger.warning(f"Failed to parse receipt {idx+1}: {e}")
        # エラー時も一覧に戻るボタンを試す
        try:
            await close_receipt_detail_view(page)
        except:
            pass
        return None


async def fetch_existing_bets(page: Page, date_type: str = "same_day") -> List[ExistingBet]:
    """
    投票内容照会から既存の投票を取得

    Args:
        page: Playwright page object
        date_type: "same_day" (当日分) or "previous_day" (前日分)

    Returns:
        List of ExistingBet objects
    """
    try:
        logger.info("📋 Fetching existing bets from 投票内容照会...")

        # PageNavigatorのインスタンス化
        navigator = PageNavigator(page, logger)

        # 1. 投票履歴ページへ遷移
        if not await navigate_to_bet_history_page(page, navigator, date_type):
            return []

        # 2. 受付番号リンク数を取得
        total_receipts = await get_bet_receipt_links(page)
        if total_receipts == 0:
            logger.warning("⚠️ No receipt links found - no bets today")
            return []

        # 3. 各受付番号を解析
        existing_bets = []
        for idx in range(total_receipts):
            bet = await parse_bet_receipt_detail(page, idx, total_receipts)
            if bet:
                existing_bets.append(bet)

        logger.info(f"✅ Found {len(existing_bets)} existing bets from {total_receipts} receipts")

        # 4. メインページに戻る
        await page.goto(IPAT_HOME_URL)
        await page.wait_for_timeout(Timeouts.MEDIUM)

        return existing_bets

    except Exception as e:
        logger.error(f"❌ Failed to fetch existing bets: {e}")
        await take_screenshot(page, "fetch_existing_bets_error")
        return []


def reconcile_tickets(
    tickets: List[Ticket],
    existing_bets: List[ExistingBet]
) -> List[ReconciliationResult]:
    """
    tickets.csvと既存投票を突合

    Args:
        tickets: tickets.csvから読み込んだチケットリスト
        existing_bets: 投票履歴から取得した既存投票リスト

    Returns:
        ReconciliationResultのリスト
    """
    results = []

    logger.info("=" * 60)
    logger.info("TICKET RECONCILIATION")
    logger.info("=" * 60)

    for ticket in tickets:
        # Check if ticket already exists in placed bets
        matching_bet = None
        for existing_bet in existing_bets:
            if ticket.matches(existing_bet):
                matching_bet = existing_bet
                break

        if matching_bet:
            result = ReconciliationResult(
                ticket=ticket,
                status=TicketStatus.ALREADY_PURCHASED,
                existing_bet=matching_bet
            )
            logger.info(f"✓ SKIP: {ticket}")
            logger.info(f"        (already purchased - receipt: {matching_bet.receipt_number})")
        else:
            result = ReconciliationResult(
                ticket=ticket,
                status=TicketStatus.NOT_PURCHASED
            )
            logger.info(f"→ TODO: {ticket} (not yet purchased)")

        results.append(result)

    # Summary
    already_purchased = sum(1 for r in results if r.status == TicketStatus.ALREADY_PURCHASED)
    to_purchase = sum(1 for r in results if r.status == TicketStatus.NOT_PURCHASED)

    logger.info("=" * 60)
    logger.info(f"SUMMARY: {already_purchased} already purchased, {to_purchase} to purchase")
    logger.info("=" * 60)

    return results


async def get_current_balance(page: Page) -> int:
    """現在の購入限度額（残高）を取得"""
    try:
        # まず画面に表示されているテキストから探す
        body_text = await page.evaluate("document.body.innerText")

        # "購入限度額" の後の数字を探す（複数パターン対応）
        import re
        patterns = [
            r'購入限度額[^\d]*(\d+(?:,\d+)*)\s*円',  # トップページ
            r'購入限度額\s*(\d+(?:,\d+)*)\s*円',      # 投票ページ（スペース付き）
            r'(\d+(?:,\d+)*)\s*円[^\d]*購入限度額',  # 逆順パターン
        ]

        for pattern in patterns:
            match = re.search(pattern, body_text)
            if match:
                balance_str = match.group(1).replace(',', '')
                balance = int(balance_str)
                logger.info(f"💰 Current balance: {balance:,}円")
                return balance

        # スクリーンショットを取得して確認
        logger.warning("⚠️ Could not find balance on page, taking screenshot for debugging")
        await take_screenshot(page, "balance_not_found")

        # 見つからない場合でも0を返す（エラーにはしない）
        logger.info("💰 Current balance: unknown (assuming sufficient)")
        return 999999  # 不明な場合は十分な金額と仮定

    except Exception as e:
        logger.error(f"Failed to get balance: {e}")
        return 999999  # エラー時も十分な金額と仮定


async def open_deposit_window(page: Page) -> Optional[Page]:
    """
    入出金ポップアップウィンドウを開く

    Returns:
        入金ページ（失敗時はNone）
    """
    try:
        # "入出金"ボタンを探してクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and "入出金" in text:
                logger.info("✓ Found '入出金' button")

                # 新しいウィンドウが開くのを待つ
                async with page.expect_popup() as popup_info:
                    await button.click()
                deposit_page = await popup_info.value

                await deposit_page.wait_for_timeout(Timeouts.LONG)
                logger.info(f"✓ Deposit window opened: {deposit_page.url}")
                return deposit_page

        logger.error("❌ '入出金' button not found")
        return None

    except Exception as e:
        logger.error(f"❌ Failed to open deposit window: {e}")
        return None


async def navigate_to_deposit_form(deposit_page: Page) -> bool:
    """
    入金指示フォームへ遷移

    Returns:
        成功したらTrue
    """
    try:
        # "入金指示"リンクをクリック
        links = await deposit_page.query_selector_all('a')
        for link in links:
            text = await link.text_content()
            if text and "入金指示" in text:
                logger.info("✓ Found '入金指示' link")
                await link.click()
                await deposit_page.wait_for_timeout(Timeouts.LONG)
                return True

        logger.error("❌ '入金指示' link not found")
        return False

    except Exception as e:
        logger.error(f"❌ Failed to navigate to deposit form: {e}")
        return False


async def complete_and_submit_deposit(deposit_page: Page, credentials: dict, deposit_amount: int) -> bool:
    """
    入金フォームの入力と送信を完了

    Returns:
        成功したらTrue
    """
    try:
        # 金額を入力
        await deposit_page.fill('input[name="NYUKIN"]', str(deposit_amount))
        logger.info(f"✓ Deposit amount entered: {deposit_amount}円")

        # "次へ"をクリック（ボタンまたはリンク）
        clickables = await deposit_page.query_selector_all('a, button, input[type="button"], input[type="submit"]')
        next_clicked = False
        for element in clickables:
            text = await element.text_content() if element else ""
            value = await element.get_attribute('value') if element else ""
            if (text and "次へ" in text) or (value and "次へ" in value):
                logger.info("✓ Clicking '次へ' button")
                await element.click()
                next_clicked = True
                break

        if not next_clicked:
            logger.error("❌ '次へ' button not found!")
            return False

        await deposit_page.wait_for_timeout(Timeouts.LONG)

        # パスワード（暗証番号）を入力
        await deposit_page.fill('input[name="PASS_WORD"]', credentials['password'])
        logger.info("✓ Password entered for deposit")

        # デバッグ: 実行前のHTMLを保存
        try:
            html_before = await deposit_page.content()
            with open("output/deposit_page_before_execution.html", "w", encoding="utf-8") as f:
                f.write(html_before)
            logger.info("✓ HTML saved: output/deposit_page_before_execution.html")
        except Exception as e:
            logger.warning(f"Failed to save HTML: {e}")

        # "実行"をクリック（ボタンまたはリンク）- JavaScriptクリックで確実に
        clickables = await deposit_page.query_selector_all('a, button, input[type="button"], input[type="submit"]')
        execution_element = None
        for element in clickables:
            text = await element.text_content() if element else ""
            value = await element.get_attribute('value') if element else ""
            if (text and "実行" in text) or (value and "実行" in value):
                logger.info(f"✓ Found '実行' button/link: text='{text}', value='{value}'")
                execution_element = element
                break

        if not execution_element:
            logger.error("❌ '実行' button not found!")
            return False

        # 実行ボタンの詳細をログ出力
        tag_name = await execution_element.evaluate("el => el.tagName")
        onclick = await execution_element.get_attribute("onclick")
        logger.info(f"✓ Element type: {tag_name}, onclick: {onclick}")

        # confirmダイアログを自動承認するハンドラーを設定
        deposit_page.on('dialog', lambda dialog: dialog.accept())
        logger.info("✓ Dialog handler set to auto-accept")

        # deposit_pageのコンテキストでsubmitForm関数を直接実行（診断情報付き）
        logger.info("✓ Executing submitForm with diagnostics")
        try:
            # submitFormの各ステップを詳細に追跡
            result = await deposit_page.evaluate("""
                () => {
                    const form = document.forms.nyukinForm;
                    const execButton = document.querySelector('a[onclick*="EXEC"]');

                    if (!form) {
                        return {success: false, message: 'Form not found'};
                    }
                    if (!execButton) {
                        return {success: false, message: 'Exec button not found'};
                    }
                    if (typeof submitForm !== 'function') {
                        return {success: false, message: 'submitForm function not found'};
                    }
                    if (typeof checkInput !== 'function') {
                        return {success: false, message: 'checkInput function not found'};
                    }

                    // checkInput の結果を取得
                    form.COMMAND.value = 'EXEC';
                    const errFlg = checkInput(form);

                    return {
                        success: true,
                        checkInputResult: errFlg,
                        commandValue: form.COMMAND.value,
                        hasConfirm: true,
                        willSubmit: errFlg === 0
                    };
                }
            """)
            logger.info(f"✓ Diagnostic result: {result}")

            if not result.get('success', False):
                logger.error(f"❌ Diagnostic failed: {result.get('message')}")
                return False

            # checkInput がエラーを返している場合
            if result.get('checkInputResult', 0) != 0:
                logger.error(f"❌ checkInput returned error code: {result.get('checkInputResult')}")
                logger.error("This means the form validation failed. Possible reasons:")
                logger.error("- 銀行口座が登録されていない")
                logger.error("- 入金額が不正")
                logger.error("- その他のバリデーションエラー")
                await take_screenshot(deposit_page, "checkInput_failed")
                return False

            logger.info(f"✓ checkInput passed (errFlg=0), proceeding with submission")

            # checkInputが成功した場合のみ、実際にsubmitを実行
            submit_result = await deposit_page.evaluate("""
                () => {
                    const form = document.forms.nyukinForm;
                    const execButton = document.querySelector('a[onclick*="EXEC"]');

                    // flagをリセット（グローバル変数）
                    if (typeof flag !== 'undefined') {
                        window.flag = false;
                    }

                    // submitForm を呼び出し
                    submitForm(execButton, form, 'EXEC');

                    return {success: true, message: 'submitForm called'};
                }
            """)
            logger.info(f"✓ Submit result: {submit_result}")

            # フォーム送信後のナビゲーションを待つ
            logger.info("⏳ Waiting for navigation after form submission...")
            try:
                await deposit_page.wait_for_load_state('networkidle', timeout=Timeouts.NETWORKIDLE)
                logger.info("✓ Navigation completed")
            except Exception as nav_error:
                logger.warning(f"⚠️ Navigation timeout (might be expected): {nav_error}")

        except Exception as e:
            logger.error(f"❌ Execution failed: {e}")
            return False

        await deposit_page.wait_for_timeout(Timeouts.LONG)

        # アラートを承認
        try:
            deposit_page.on('dialog', lambda dialog: dialog.accept())
            await deposit_page.wait_for_timeout(Timeouts.MEDIUM)
            logger.info("✓ Alert accepted")
        except Exception as e:
            logger.debug(f"No alert or already handled: {e}")

        await deposit_page.wait_for_timeout(Timeouts.LONG)
        await take_screenshot(deposit_page, "deposit_complete")

        return True

    except Exception as e:
        logger.error(f"❌ Failed to complete and submit deposit: {e}")
        return False


async def verify_deposit_balance(page: Page, deposit_amount: int) -> bool:
    """
    入金が残高に反映されたか確認

    Args:
        page: メインページ
        deposit_amount: 入金額

    Returns:
        残高が入金額以上になったらTrue、タイムアウトや失敗時はFalse
    """
    try:
        # メインページで残高が更新されるまで待つ（最大3回、各30秒 = 最大90秒）
        # Note: Balance may not update if funds are reserved in cart
        logger.info("⏳ Checking if deposit has reflected in balance...")

        balance = 0
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            logger.info(f"💰 Attempt {attempt}/{max_retries}: Checking balance...")

            # デバッグ: HTMLを保存
            try:
                html_content = await page.content()
                with open(f"output/main_page_after_deposit_attempt{attempt}.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
                logger.info(f"✓ HTML saved: output/main_page_after_deposit_attempt{attempt}.html")
            except Exception as e:
                logger.warning(f"Failed to save HTML: {e}")

            # 残高を確認
            balance = await get_current_balance(page)

            if balance >= deposit_amount:
                logger.info(f"✅ Deposit confirmed! Balance: {balance:,}円 (Expected: {deposit_amount:,}円)")
                return True
            else:
                logger.warning(f"⚠️ Balance not yet updated: {balance:,}円 / {deposit_amount:,}円")
                if attempt < max_retries:
                    logger.info(f"🔄 Waiting 30 seconds before next check... ({attempt}/{max_retries})")
                    # 次のチェックまで30秒待機
                    await page.wait_for_timeout(Timeouts.BALANCE_CHECK)

        # 最終確認
        if balance < deposit_amount:
            logger.error(f"❌ Balance verification timed out after {max_retries} attempts")
            logger.error(f"   Expected: {deposit_amount:,}円, Got: {balance:,}円")
            logger.error("❌ 入金が反映されませんでした。銀行口座の残高不足の可能性があります。")
            logger.error("❌ 投票処理を中止します。")
            await take_screenshot(page, "deposit_verification_timeout")
            return False

        logger.info(f"✅ Deposit completed and verified: {balance:,}円")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to verify deposit balance: {e}")
        await take_screenshot(page, "deposit_verification_error")
        return False


async def deposit(page: Page, credentials: dict, amount: int = 20000):
    """
    入金処理（Seleniumコードベース）

    Args:
        page: メインページ
        credentials: 認証情報
        amount: 入金額

    Returns:
        成功したらTrue
    """
    try:
        deposit_amount = amount
        logger.info(f"💸 Starting deposit process: {deposit_amount}円")

        # 1. 入金ウィンドウを開く
        deposit_page = await open_deposit_window(page)
        if not deposit_page:
            return False

        # 2. 入金指示フォームへ遷移
        if not await navigate_to_deposit_form(deposit_page):
            await deposit_page.close()
            return False

        # 3. 入金フォームを入力して送信
        if not await complete_and_submit_deposit(deposit_page, credentials, deposit_amount):
            await deposit_page.close()
            return False

        # 4. 入金ウィンドウを閉じる
        await deposit_page.close()

        # 5. 残高反映を確認
        return await verify_deposit_balance(page, deposit_amount)

    except Exception as e:
        logger.error(f"❌ Deposit failed: {e}")
        await take_screenshot(page, "deposit_error")
        return False


async def perform_stage1_login(page: Page, credentials: dict):
    """
    第1段階ログイン: INET-ID入力

    Returns:
        成功したらTrue
    """
    try:
        logger.info("🔐 Stage 1: INET-ID login")
        await page.fill('input[name="inetid"]', credentials['inet_id'])
        logger.info("✓ INET-ID entered")

        # 次の画面への遷移
        await page.click('.button')
        await page.wait_for_timeout(Timeouts.LONG)
        logger.info("✓ Stage 1 button clicked")
        await take_screenshot(page, "after_stage1")
        return True

    except Exception as e:
        logger.error(f"❌ Stage 1 login failed: {e}")
        return False


async def perform_stage2_login(page: Page, credentials: dict):
    """
    第2段階ログイン: 加入者番号、暗証番号、P-ARS番号入力

    Returns:
        成功したらTrue
    """
    try:
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

        await page.wait_for_timeout(Timeouts.MEDIUM)

        # 次の画面への遷移 - .buttonModernをクリック
        button_modern = await page.wait_for_selector('.buttonModern', timeout=Timeouts.SELECTOR_WAIT)
        logger.info("✓ Found .buttonModern element")

        await button_modern.click(force=True)
        await page.wait_for_timeout(Timeouts.LOGIN)
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
            return False

        await take_screenshot(page, "after_stage2")
        return True

    except Exception as e:
        logger.error(f"❌ Stage 2 login failed: {e}")
        return False


async def handle_ok_dialog(page: Page):
    """
    OKダイアログが表示された場合の処理

    Returns:
        なし（OKボタンがない場合も正常）
    """
    try:
        await page.wait_for_timeout(Timeouts.LONG)
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and "OK" in text:
                await button.click()
                logger.info("✓ OK button clicked")
                await page.wait_for_timeout(Timeouts.LONG)
                break
    except Exception as e:
        logger.debug(f"No OK button found (normal): {e}")


async def verify_login_success(page: Page):
    """
    ログイン成功の確認と残高取得

    Returns:
        成功したらTrue

    Raises:
        Exception: ログイン失敗時
    """
    # メインフレームの読み込みを待つ
    await page.wait_for_timeout(Timeouts.VERY_LONG)

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
        await page.wait_for_timeout(Timeouts.NAVIGATION)

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
        await page.wait_for_timeout(Timeouts.NAVIGATION)

    await page.wait_for_timeout(Timeouts.MEDIUM)
    await take_screenshot(page, "login_complete")
    logger.info("✅ Login completed successfully")
    return True


async def login_simple(page: Page, credentials: dict):
    """
    シンプルなIPATログイン処理

    Args:
        page: Playwright page
        credentials: 認証情報

    Returns:
        成功したらTrue
    """
    try:
        logger.info("🔐 Starting simple IPAT login...")

        # ログイン画面へ移動
        await page.goto(IPAT_URL)
        await page.wait_for_timeout(Timeouts.LONG)

        # 1. 第1段階ログイン (INET-ID)
        if not await perform_stage1_login(page, credentials):
            raise Exception("Stage 1 login failed")

        # 2. 第2段階ログイン (ユーザー認証情報)
        if not await perform_stage2_login(page, credentials):
            raise Exception("Stage 2 login failed")

        # 3. OKダイアログの処理
        await handle_ok_dialog(page)

        # 4. ログイン成功確認と残高取得
        return await verify_login_success(page)

    except Exception as e:
        logger.error(f"❌ Login failed: {e}")
        await take_screenshot(page, "login_error")
        raise


async def check_already_on_vote_page(page: Page) -> bool:
    """
    既に投票ページにいるかチェック

    Returns:
        既に投票ページにいればTrue
    """
    # 競馬場タブが表示されていて、モーダルがない場合は既に投票ページにいる
    racecourse_tabs = await page.query_selector_all('[class*="jyoTab"], [class*="field"]')
    modals = await page.query_selector_all('.modal, [class*="dialog"]')
    visible_modals = []
    for modal in modals:
        if await modal.is_visible():
            visible_modals.append(modal)

    if len(racecourse_tabs) >= 3 and len(visible_modals) == 0:
        logger.info("✓ Already on clean vote page, skipping navigation")
        await take_screenshot(page, "vote_page")
        return True

    return False


async def close_visible_modals(page: Page):
    """
    表示されているモーダルを閉じる
    """
    modals = await page.query_selector_all('.modal, [class*="dialog"]')
    visible_modals = []
    for modal in modals:
        if await modal.is_visible():
            visible_modals.append(modal)

    if len(visible_modals) > 0:
        logger.info(f"Found {len(visible_modals)} visible modals, trying to close...")
        # OK/閉じるボタンを探してクリック
        all_buttons = await page.query_selector_all('button, input[type="button"]')
        for btn in all_buttons:
            try:
                if await btn.is_visible():
                    text = await btn.text_content()
                    if text and ("OK" in text or "閉じる" in text):
                        await btn.click()
                        logger.info(f"✓ Clicked close button: {text.strip()}")
                        await page.wait_for_timeout(Timeouts.SHORT)
                        break
            except:
                pass


async def click_vote_menu_link(page: Page):
    """
    投票メニューリンクをクリック（トップメニューから投票選択画面へ）
    """
    all_links = await page.query_selector_all('a, button, div[ng-click]')
    for link in all_links:
        try:
            text = await link.text_content()
            if text and "投票メニュー" in text:
                logger.info("✓ Clicking '投票メニュー' link to reset vote page")
                await link.click()
                await page.wait_for_timeout(Timeouts.MEDIUM)
                break
        except:
            pass


async def find_and_click_vote_button_in_main_page(page: Page) -> bool:
    """
    メインページで通常投票ボタンを探してクリック

    Returns:
        ボタンが見つかってクリックできたらTrue
    """
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
            # JavaScriptクリックを使用（要素が他の要素に隠れていてもOK）
            try:
                await button.evaluate("el => el.click()")
                logger.info(f"✓ Clicked vote button (JS click): {text.strip()}")
            except Exception as e:
                logger.warning(f"JS click failed, trying normal click: {e}")
                await button.click()
                logger.info(f"✓ Clicked vote button: {text.strip()}")
            await page.wait_for_timeout(Timeouts.LONG)

            # 投票ボタンクリック後にモーダルが出る場合があるので再度チェック
            try:
                post_click_modals = await page.query_selector_all('.modal, [class*="dialog"], [role="dialog"]')
                for modal in post_click_modals:
                    if await modal.is_visible():
                        # "このまま進む" や "OK" ボタンを探してクリック
                        modal_buttons = await modal.query_selector_all('button, input[type="button"]')
                        for mbtn in modal_buttons:
                            try:
                                mtext = await mbtn.text_content()
                                if mtext and ("このまま進む" in mtext or "OK" in mtext or "進む" in mtext):
                                    await mbtn.click()
                                    logger.info(f"✓ Closed post-vote modal: {mtext.strip()}")
                                    await page.wait_for_timeout(Timeouts.MEDIUM)
                                    break
                            except:
                                pass
                        break
            except Exception as e:
                logger.debug(f"No post-vote modals: {e}")

            await take_screenshot(page, "vote_page")
            return True

    return False


async def find_and_click_vote_button_in_frames(page: Page) -> bool:
    """
    フレーム内で通常投票ボタンを探してクリック

    Returns:
        ボタンが見つかってクリックできたらTrue
    """
    frames = page.frames
    logger.info(f"Checking {len(frames)} frames")
    for i, frame in enumerate(frames):
        try:
            frame_buttons = await frame.query_selector_all('button')
            logger.info(f"Frame {i} has {len(frame_buttons)} buttons")
            for button in frame_buttons:
                text = await button.text_content()
                if text and "通常" in text and "投票" in text:
                    # JavaScriptクリックを使用
                    try:
                        await button.evaluate("el => el.click()")
                        logger.info(f"✓ Clicked vote button in frame {i} (JS click): {text.strip()}")
                    except Exception as e:
                        logger.warning(f"JS click failed in frame {i}, trying normal click: {e}")
                        await button.click()
                        logger.info(f"✓ Clicked vote button in frame {i}: {text.strip()}")
                    await page.wait_for_timeout(Timeouts.LONG)
                    await take_screenshot(page, "vote_page")
                    return True
        except Exception as e:
            logger.debug(f"Frame {i} error: {e}")

    return False


async def navigate_to_vote_simple(page: Page):
    """
    投票画面へ移動（シンプル版）

    Returns:
        成功したらTrue
    """
    try:
        logger.info("📋 Navigating to vote page...")

        # ページが完全に読み込まれるまで待つ
        await page.wait_for_timeout(Timeouts.MEDIUM)
        await take_screenshot(page, "before_vote_navigation")

        # ページのHTMLをデバッグ出力
        page_content = await page.content()
        logger.info(f"Page content length: {len(page_content)}")

        # 1. 既に投票ページにいるかチェック
        if await check_already_on_vote_page(page):
            return True

        # 2. モーダルを閉じる
        await close_visible_modals(page)

        # 3. 投票メニューリンクをクリック
        await click_vote_menu_link(page)

        await page.wait_for_timeout(Timeouts.MEDIUM)

        # 4. メインページで通常投票ボタンを探してクリック
        if await find_and_click_vote_button_in_main_page(page):
            return True

        # 5. フレーム内で通常投票ボタンを探してクリック
        if await find_and_click_vote_button_in_frames(page):
            return True

        logger.error("❌ Vote button not found")
        await take_screenshot(page, "vote_button_not_found")
        return False

    except Exception as e:
        logger.error(f"Failed to navigate to vote: {e}")
        return False


async def find_and_click_racecourse_button(page: Page, racecourse: str) -> bool:
    """
    競馬場ボタンを検索してクリック

    Args:
        page: Playwright page
        racecourse: 競馬場名（例: "東京", "福島"）

    Returns:
        クリックに成功したらTrue
    """
    # buttons, links, and clickable divs を全て検索
    all_clickables = await page.query_selector_all('button, a, div[ng-click], span[ng-click]')
    logger.info(f"Found {len(all_clickables)} clickable elements")

    for i, element in enumerate(all_clickables):
        text = await element.text_content()
        if text:
            text = text.strip()
            # デバッグ: 最初の50個の要素をログ出力
            if i < 50:
                logger.info(f"  Element[{i}]: '{text[:50]}'")
            # "福島（土）", "福島（金）" など、競馬場名で始まる要素を検索
            if text.startswith(racecourse + "（"):
                # JavaScriptクリックで確実にクリック（要素が隠れていてもOK）
                try:
                    await element.evaluate("el => el.click()")
                    logger.info(f"✓ Selected racecourse (JS click): {text}")
                except Exception as e:
                    logger.warning(f"JS click failed, trying normal click: {e}")
                    await element.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)
                    await element.click()
                    logger.info(f"✓ Selected racecourse: {text}")
                return True

    logger.error(f"Racecourse button not found for: {racecourse}")
    await take_screenshot(page, f"racecourse_not_found_{racecourse}")
    return False


async def find_and_click_race_button(page: Page, racecourse: str, race_number: int) -> tuple[bool, Optional[any]]:
    """
    レースボタンを検索してクリック

    Args:
        page: Playwright page
        racecourse: 競馬場名
        race_number: レース番号

    Returns:
        (成功したか, クリックしたレースボタン要素)
    """
    race_text = f"{race_number}R"
    all_race_elements = await page.query_selector_all('button, a, div[ng-click], span[ng-click]')
    logger.info(f"Found {len(all_race_elements)} elements for race selection")

    race_button = None
    for i, element in enumerate(all_race_elements):
        text = await element.text_content()
        if text:
            text = text.strip()
            # デバッグ用に最初の20個のレース要素をログ出力
            if i < 20 and ('R' in text or '(' in text):
                logger.info(f"  Race element[{i}]: '{text[:100]}'")

            # "10R (時刻)"のようなフォーマットに対応
            if text.startswith(race_text):
                race_button = element
                logger.info(f"✓ Found race button at index {i}: '{text[:50]}'")
                break

    if not race_button:
        logger.error(f"Race button {race_text} not found")
        await take_screenshot(page, f"race_button_not_found_{racecourse}_{race_number}")
        return False, None

    # JavaScriptクリックで確実にクリック
    try:
        await race_button.evaluate("el => el.click()")
        logger.info(f"✓ Clicked race button (JS click): {race_text}")
    except Exception as e:
        logger.warning(f"JS click failed on race button, trying normal click: {e}")
        await race_button.click()
        logger.info(f"✓ Clicked race button: {race_text}")

    return True, race_button


async def wait_for_race_button_activation(page: Page, race_button):
    """
    レースボタンがアクティブ化（"on"クラス追加）されるまで待機

    Args:
        page: Playwright page
        race_button: レースボタン要素
    """
    logger.info("Waiting for Angular to update DOM...")
    try:
        # レースボタンが "on" クラスを持つまで待つ（最大10秒）
        for i in range(20):  # 20回 x 500ms = 10秒
            btn_class = await race_button.get_attribute('class')
            if btn_class and 'on' in btn_class:
                logger.info(f"✓ Race button activated (on class detected) after {i * 0.5}s")
                break
            await page.wait_for_timeout(500)
        else:
            logger.warning("Race button didn't get 'on' class within 10 seconds")
    except Exception as e:
        logger.warning(f"Error waiting for 'on' class: {e}")


async def scroll_to_horse_selection_area(page: Page, racecourse: str, race_number: int):
    """
    馬番選択エリアまでスクロール

    Args:
        page: Playwright page
        racecourse: 競馬場名
        race_number: レース番号
    """
    logger.info("Scrolling to horse selection area...")
    await page.evaluate("window.scrollTo(0, 400);")
    await page.wait_for_timeout(Timeouts.MEDIUM)
    await take_screenshot(page, f"horse_selection_{racecourse}_{race_number}")


async def select_race_simple(page: Page, racecourse: str, race_number: int):
    """
    競馬場とレースを選択（シンプル版）

    Args:
        page: Playwright page
        racecourse: 競馬場名
        race_number: レース番号

    Returns:
        成功したらTrue
    """
    try:
        logger.info(f"🏇 Selecting {racecourse} R{race_number}...")

        # 1. 競馬場ボタンを検索してクリック
        if not await find_and_click_racecourse_button(page, racecourse):
            return False

        # 2. Angularがレース一覧を読み込むまで待つ
        logger.info("Waiting for race list to load...")
        await page.wait_for_timeout(Timeouts.NAVIGATION)
        await take_screenshot(page, f"after_racecourse_selection_{racecourse}")

        # 3. レースボタンを検索してクリック
        success, race_button = await find_and_click_race_button(page, racecourse, race_number)
        if not success:
            return False

        # 4. レースボタンのアクティブ化待機
        await wait_for_race_button_activation(page, race_button)

        await page.wait_for_timeout(Timeouts.MEDIUM)
        await take_screenshot(page, f"race_selected_{racecourse}_{race_number}")

        # 5. 馬番選択エリアまでスクロール
        await scroll_to_horse_selection_area(page, racecourse, race_number)

        return True

    except Exception as e:
        logger.error(f"Failed to select race: {e}")
        return False


async def select_horse_on_page(page: Page, horse_number: int) -> bool:
    """ページ上で馬を選択"""
    try:
        # スクロール（大きい番号の場合）
        if horse_number >= 9:
            logger.info("Scrolling for larger horse numbers...")
            await page.evaluate("window.scrollTo(0, 300);")
            await page.wait_for_timeout(Timeouts.MEDIUM)
            if horse_number >= 13:
                await page.evaluate("window.scrollTo(0, 300);")
                await page.wait_for_timeout(Timeouts.MEDIUM)

        # 馬番から買う馬券を選択
        # デバッグ: HTMLとlabelの情報を保存
        try:
            html_content = await page.content()
            with open("output/horse_selection_page.html", "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info("✓ HTML saved for debugging: output/horse_selection_page.html")
        except Exception as e:
            logger.warning(f"Failed to save HTML: {e}")

        labels = await page.query_selector_all('label')
        logger.info(f"Found {len(labels)} labels on page")

        # 最初の30個のlabelのテキストを出力
        for i in range(min(30, len(labels))):
            text = await labels[i].text_content()
            logger.info(f"  Label[{i}]: {text.strip() if text else '(empty)'}")

        # 固定オフセットではなく、より柔軟な方法を試す
        # まず単勝エリアのlabelを探す
        found = False
        for i, label in enumerate(labels):
            text = await label.text_content()
            # 馬番が含まれるlabelを探す（例: "1", "2", "14"など）
            if text and text.strip() == str(horse_number):
                logger.info(f"Found label for horse #{horse_number} at index {i}")
                await label.click()
                logger.info(f"✓ Horse #{horse_number} selected")
                found = True
                break

        if not found:
            # フォールバック: 旧方式
            if len(labels) > horse_number + 8:
                await labels[horse_number + UIIndices.HORSE_LABEL_OFFSET].click()
                logger.info(f"✓ Horse #{horse_number} selected (fallback method)")
            else:
                raise Exception(f"Not enough labels found: {len(labels)} < {horse_number + 8}")

        await page.wait_for_timeout(Timeouts.MEDIUM)
        return True
    except Exception as e:
        logger.error(f"❌ Failed to select horse: {e}")
        return False


async def complete_bet_input_form(page: Page, bet_amount: int) -> bool:
    """馬券入力フォームを完成させる"""
    try:
        # セットのクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "セット":
                await button.click()
                logger.info("✓ 'Set' button clicked")
                break

        await page.wait_for_timeout(Timeouts.MEDIUM)

        # 入力終了のクリック
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "入力終了":
                await button.click()
                logger.info("✓ 'Input End' button clicked")
                break

        await page.wait_for_timeout(Timeouts.LONG)
        await take_screenshot(page, "before_amount_input")

        # 購入直前の投票票数の入力
        inputs = await page.query_selector_all('input')
        bet_units = bet_amount // 100

        await inputs[UIIndices.BET_UNITS_INPUT_1].fill(str(bet_units))
        await page.wait_for_timeout(Timeouts.SHORT)
        await inputs[UIIndices.BET_UNITS_INPUT_2].fill(str(bet_units))
        await page.wait_for_timeout(Timeouts.SHORT)
        await inputs[UIIndices.BET_AMOUNT_INPUT].fill(str(bet_amount))
        logger.info(f"✓ Bet amount entered: {bet_amount} yen")

        await page.wait_for_timeout(Timeouts.LONG)
        await take_screenshot(page, "before_purchase")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to complete bet input form: {e}")
        return False


async def add_bet_to_cart(page: Page, horse_name: str, bet_amount: int) -> bool:
    """馬券をカートに追加（セット処理）"""
    try:
        # 購入ボタン（実際にはカートに追加）
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "購入する":
                await button.click()
                logger.info("✓ 'Purchase' button clicked")
                break

        await page.wait_for_timeout(Timeouts.LONG)

        # ダイアログのメッセージを確認
        page_text = await page.text_content('body')

        # エラーメッセージのチェック
        error_keywords = ['できません', 'エラー', '失敗', '不足', '無効', '締切']
        success_keywords = ['受付', '完了', '購入しました', 'セットしました']

        # どのキーワードがマッチしたかを記録
        matched_errors = [kw for kw in error_keywords if kw in page_text]
        matched_success = [kw for kw in success_keywords if kw in page_text]

        has_error = len(matched_errors) > 0
        has_success = len(matched_success) > 0

        logger.info(f"🔍 Matched error keywords: {matched_errors}")
        logger.info(f"✅ Matched success keywords: {matched_success}")

        # 成功キーワードが見つかった場合は成功を優先（エラーキーワードは他のレースの状態表示にも含まれるため）
        if has_success:
            logger.info(f"✅ Purchase set successfully (success keywords found): {matched_success}")
            # 成功の場合でもエラーキーワードが含まれていたら警告
            if has_error:
                logger.warning(f"⚠️ Error keywords also found (likely from other races): {matched_errors}")
        elif has_error:
            # 成功キーワードがなく、エラーキーワードのみの場合はエラー
            logger.error(f"❌ Purchase failed! Error message detected: {matched_errors}")
            logger.error(f"Page content: {page_text[:1000]}")  # 最初の1000文字を出力
            await take_screenshot(page, "purchase_failed")
            # エラーダイアログのOKをクリック
            buttons = await page.query_selector_all('button')
            for button in buttons:
                text = await button.text_content()
                if text and text.strip() == "OK":
                    await button.click()
                    break
            return False

        # OKボタンをクリック（「セットしました」ダイアログを閉じる）
        ok_clicked = False
        buttons = await page.query_selector_all('button')
        for button in buttons:
            text = await button.text_content()
            if text and text.strip() == "OK":
                await button.click()
                logger.info("✓ 'Set confirmation' dialog closed")
                ok_clicked = True
                break

        if not ok_clicked:
            logger.error("❌ Set confirmation failed: OK button not found")
            await take_screenshot(page, "set_no_ok_button")
            return False

        if not has_success:
            logger.warning("⚠️ Set status unclear - success message not found")
            await take_screenshot(page, "set_unclear")
            return False

        # ここまでで「セット」(カートに追加)が完了
        logger.info(f"✅ Bet added to cart: {horse_name} - {bet_amount} yen")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to add bet to cart: {e}")
        return False


async def confirm_and_purchase_bet(page: Page) -> bool:
    """投票内容を確認して購入を実行"""
    try:
        # 実際の「購入」処理を実行
        await page.wait_for_timeout(Timeouts.MEDIUM)
        await take_screenshot(page, "after_set")

        # 購入予定リストから「投票内容確認」ボタンを探してクリック
        logger.info("🛒 Looking for 'Confirm Vote Content' button...")

        # より柔軟な検索：テキストに「投票」「内容」「確認」が全て含まれる要素を探す
        # （改行やスペースに対応するため）
        confirm_clicked = False

        # まず、通常の方法で試す
        confirm_buttons = await page.query_selector_all('button, a, div')
        logger.info(f"Found {len(confirm_buttons)} potential button elements")

        for btn in confirm_buttons:
            try:
                text = await btn.text_content()
                if text:
                    # 改行・スペースを削除して検索
                    normalized_text = text.replace('\n', '').replace(' ', '').replace('\t', '')
                    if "投票内容確認" in normalized_text or ("投票" in normalized_text and "内容" in normalized_text and "確認" in normalized_text):
                        logger.info(f"✓ Found button with vote confirmation text: '{text[:100]}'")
                        try:
                            # JavaScriptクリックを使用
                            await btn.evaluate("el => el.click()")
                            logger.info(f"✓ Confirm button clicked successfully")
                            confirm_clicked = True
                            break
                        except Exception as click_error:
                            logger.warning(f"⚠️ Click failed, trying next match: {click_error}")
            except Exception as e:
                pass

        if not confirm_clicked:
            logger.error("❌ Confirm vote content button not found")
            await take_screenshot(page, "confirm_button_not_found")
            return False

        # 確認画面が表示されるまで待つ
        await page.wait_for_timeout(Timeouts.NAVIGATION)
        await take_screenshot(page, "purchase_confirmation_screen")

        # すでに購入完了しているかチェック（受付番号が表示されている場合）
        page_text = await page.text_content('body') or ''
        if '受付番号' in page_text and '購入しました' not in page_text:
            # 受付番号が表示されていれば、自動的に購入が完了している
            logger.info("✅ Purchase already completed (受付番号 detected on screen)")
            return True

        # 確認画面で「購入する」ボタンを探してクリック
        logger.info("💳 Looking for final purchase button on confirmation screen...")

        final_buttons = await page.query_selector_all('button, a, div[ng-click]')
        final_purchase_clicked = False

        for btn in final_buttons:
            try:
                text = await btn.text_content()
                if text:
                    normalized_text = text.replace('\n', '').replace(' ', '').replace('\t', '').strip()
                    # "購入する" を検索（改行・スペース対応）
                    if "購入する" in normalized_text:
                        # ボタンが表示されているか確認
                        if await btn.is_visible():
                            # JavaScriptクリックを使用
                            await btn.evaluate("el => el.click()")
                            logger.info(f"✓ Final purchase button clicked: {normalized_text}")
                            final_purchase_clicked = True
                            # 購入完了画面への遷移を待つ
                            await page.wait_for_timeout(Timeouts.NAVIGATION)
                            await take_screenshot(page, "after_final_purchase_click")
                            break
            except:
                pass

        if not final_purchase_clicked:
            logger.error("❌ Final purchase button not found on confirmation screen")
            await take_screenshot(page, "final_purchase_button_not_found")
            return False

        return True
    except Exception as e:
        logger.error(f"❌ Failed to confirm and purchase bet: {e}")
        return False


async def verify_purchase_completion(page: Page, horse_name: str, bet_amount: int) -> bool:
    """購入完了を確認"""
    try:
        # 購入確認ダイアログの処理
        await page.wait_for_timeout(Timeouts.NAVIGATION)
        await take_screenshot(page, "final_purchase_confirmation")

        # 購入完了のメッセージを確認
        page_text_final = await page.text_content('body')

        if '購入しました' in page_text_final or '受付' in page_text_final:
            logger.info(f"✅ Purchase completed successfully: {horse_name} - {bet_amount} yen")
            await take_screenshot(page, "purchase_complete_success")

            # 完了ダイアログのOKをクリック
            final_buttons = await page.query_selector_all('button')
            for btn in final_buttons:
                text = await btn.text_content()
                if text and text.strip() == "OK":
                    await btn.click()
                    logger.info("✓ Purchase completion dialog closed")
                    break

            return True
        else:
            logger.error("❌ Purchase completion message not found")
            logger.error(f"Page text: {page_text_final[:500]}")
            await take_screenshot(page, "purchase_completion_failed")
            return False
    except Exception as e:
        logger.error(f"❌ Failed to verify purchase completion: {e}")
        return False


async def select_horse_and_bet_simple(page: Page, horse_number: int, horse_name: str, bet_amount: int):
    """馬を選択して投票（シンプル版）"""
    try:
        logger.info(f"🎯 Selecting horse #{horse_number} {horse_name}, bet {bet_amount} yen...")

        # 購入前に残高をチェック（念のため）
        balance = await get_current_balance(page)
        if balance < bet_amount:
            logger.error(f"❌ Insufficient balance! Required: {bet_amount:,}円, Available: {balance:,}円")
            await take_screenshot(page, f"insufficient_balance_{horse_number}")
            return False

        await page.wait_for_timeout(Timeouts.LONG)

        # 1. 馬を選択
        if not await select_horse_on_page(page, horse_number):
            return False

        # 2. 馬券入力フォームを完成
        if not await complete_bet_input_form(page, bet_amount):
            return False

        # 3. 馬券をカートに追加
        if not await add_bet_to_cart(page, horse_name, bet_amount):
            return False

        # 4. 投票内容を確認して購入
        if not await confirm_and_purchase_bet(page):
            return False

        # 5. 購入完了を確認
        if not await verify_purchase_completion(page, horse_name, bet_amount):
            return False

        return True

    except Exception as e:
        logger.error(f"Failed to place bet: {e}")
        await take_screenshot(page, "bet_error")
        return False


async def load_configuration():
    """
    認証情報とチケットファイルを読み込む

    Returns:
        Tuple[dict, dict, Path]: (credentials, slack_info, tickets_path)
    """
    # 認証情報取得
    credentials, slack_info = await get_all_secrets()

    # tickets.csv読み込み（日付指定または最新）
    tickets_date = os.environ.get('TICKETS_DATE', None)

    if tickets_date:
        # 日付指定がある場合、そのファイルを読む
        tickets_path = Path(f'tickets/tickets_{tickets_date}.csv')
    else:
        # 日付指定がない場合、tickets_YYYYMMDD.csvの最新ファイルを探す
        tickets_dir = Path('tickets')
        dated_files = sorted(tickets_dir.glob('tickets_????????.csv'), reverse=True)
        if dated_files:
            tickets_path = dated_files[0]  # 最新のファイル
            logger.info(f"📅 Using latest tickets file: {tickets_path.name}")
        else:
            # 日付なしのtickets.csvにフォールバック
            tickets_path = Path('tickets/tickets.csv')

    if not tickets_path.exists():
        logger.error(f"❌ Tickets file not found: {tickets_path}")
        raise FileNotFoundError(f"Tickets file not found: {tickets_path}")

    return credentials, slack_info, tickets_path


async def initialize_browser_and_session(p, credentials):
    """
    ブラウザとセッションを初期化

    Returns:
        Tuple[Browser, BrowserContext, Page]: (browser, context, page)
    """
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
        await page.wait_for_timeout(Timeouts.NAVIGATION)
        page_text = await page.evaluate("document.body.innerText")

        # ログインフォームが表示されている場合はセッション期限切れ
        if "INET-ID" in page_text or "加入者番号" in page_text:
            logger.warning("⚠️ Session expired, logging in again...")
            await login_simple(page, credentials)
            await context.storage_state(path=session_path)
            logger.info("✓ Session refreshed")
        else:
            logger.info("✓ Session is still valid")

    return browser, context, page


async def load_and_reconcile_tickets(page: Page, tickets_path: Path):
    """
    チケットCSVを読み込み、既存投票と突合

    Returns:
        Tuple[List[Ticket], List[ReconciliationResult], List[Ticket]]:
        (tickets, reconciliation_results, to_purchase)
    """
    # CSVを読み込む
    tickets_df = pd.read_csv(tickets_path)
    logger.info(f"📄 Found {len(tickets_df)} tickets to process from {tickets_path.name}")

    # tickets.csvをTicketオブジェクトに変換
    tickets = []
    for _, row in tickets_df.iterrows():
        ticket = Ticket(
            racecourse=row['race_course'],
            race_number=int(row['race_number']),
            bet_type=row.get('bet_type', '単勝'),  # デフォルト: 単勝
            horse_number=int(row['horse_number']),
            horse_name=row['horse_name'],
            amount=int(row['amount'])
        )
        tickets.append(ticket)

    logger.info(f"📄 Loaded {len(tickets)} tickets from CSV")

    # 既存の投票を取得（冪等性チェック）
    existing_bets = await fetch_existing_bets(page, date_type="same_day")

    # 突合処理
    reconciliation_results = reconcile_tickets(tickets, existing_bets)

    # 未購入のチケットのみを抽出
    to_purchase = [
        r.ticket for r in reconciliation_results
        if r.status == TicketStatus.NOT_PURCHASED
    ]

    # サマリーレポート
    already_purchased_count = sum(
        1 for r in reconciliation_results
        if r.status == TicketStatus.ALREADY_PURCHASED
    )

    logger.info("\n" + "=" * 60)
    logger.info("RECONCILIATION SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total tickets: {len(tickets)}")
    logger.info(f"Already purchased: {already_purchased_count}")
    logger.info(f"To purchase: {len(to_purchase)}")
    logger.info("=" * 60)

    return tickets, reconciliation_results, to_purchase


async def handle_dry_run_mode(page: Page, to_purchase: List[Ticket], reconciliation_results: List):
    """
    DRY_RUNモードの処理

    Returns:
        bool: DRY_RUNモードならTrue（処理を終了すべき）
    """
    DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
    if not DRY_RUN:
        return False

    logger.warning("\n" + "=" * 60)
    logger.warning("🔸 DRY_RUN MODE: Simulating bet placement")
    logger.warning("=" * 60)
    logger.warning("The following bets would be placed:")
    for idx, ticket in enumerate(to_purchase):
        logger.warning(f"  {idx+1}. {ticket}")

    # 総費用を計算
    total_cost = sum(t.amount for t in to_purchase)
    logger.warning(f"\nTotal amount that would be spent: {total_cost:,}円")

    # 残高確認（参考情報）
    current_balance = await get_current_balance(page)
    logger.warning(f"Current balance: {current_balance:,}円")

    if current_balance < total_cost:
        shortage = total_cost - current_balance
        logger.warning(f"Would need to deposit: {shortage:,}円")
    else:
        logger.warning(f"Balance is sufficient (no deposit needed)")

    logger.warning("=" * 60)
    logger.warning("🔸 DRY_RUN: Skipping actual bet placement")
    logger.warning("=" * 60)

    # DRY_RUNステータスに更新
    for result in reconciliation_results:
        if result.status == TicketStatus.NOT_PURCHASED:
            result.status = TicketStatus.SKIPPED_DRY_RUN

    return True


async def ensure_sufficient_balance(page: Page, credentials: dict, to_purchase: List[Ticket]) -> bool:
    """
    残高を確認し、不足していれば入金

    Returns:
        bool: 成功したらTrue
    """
    # 未購入チケットの総費用を計算
    total_cost = sum(t.amount for t in to_purchase)
    logger.info(f"\n💰 Total cost for unpurchased tickets: {total_cost:,}円")

    # 現在の残高を確認
    current_balance = await get_current_balance(page)
    logger.info(f"💰 Current balance: {current_balance:,}円")

    # 不足分を計算
    if current_balance < total_cost:
        shortage = total_cost - current_balance
        logger.info(f"⚠️ Insufficient balance! Shortage: {shortage:,}円")
        logger.info(f"💸 Depositing shortage amount: {shortage:,}円")

        if await deposit(page, credentials, shortage):
            logger.info(f"✅ Deposit completed: {shortage:,}円")
            return True
        else:
            logger.error("❌ Deposit failed - aborting ticket processing")
            return False
    else:
        logger.info(f"✅ Balance is sufficient ({current_balance:,}円 >= {total_cost:,}円), skipping deposit")
        return True


async def process_tickets(page: Page, to_purchase: List[Ticket]):
    """
    未購入チケットを処理

    Args:
        page: Playwright page
        to_purchase: 購入すべきチケットのリスト
    """
    for ticket_idx, ticket in enumerate(to_purchase):
        try:
            logger.info(f"\n{'='*60}")
            logger.info(f"🎫 Purchasing {ticket_idx+1}/{len(to_purchase)}: {ticket}")
            logger.info(f"{'='*60}")

            # 各チケット処理の前にトップページに戻る（2つ目以降）
            if ticket_idx > 0:
                logger.info("🔄 Returning to top page...")
                await page.goto(IPAT_HOME_URL)
                await page.wait_for_timeout(Timeouts.NAVIGATION)
                logger.info("✓ Returned to top page")

            # 投票画面へ移動
            if not await navigate_to_vote_simple(page):
                logger.error("Failed to navigate to vote page")
                continue

            # レース選択
            if not await select_race_simple(page, ticket.racecourse, ticket.race_number):
                logger.error("Failed to select race")
                continue

            # 馬選択と投票
            if await select_horse_and_bet_simple(page, ticket.horse_number, ticket.horse_name, ticket.amount):
                logger.info(f"✅ Ticket {ticket_idx+1} completed successfully")
            else:
                logger.error(f"❌ Ticket {ticket_idx+1} failed")

            # 次のチケットのため少し待機
            await page.wait_for_timeout(5000)

        except Exception as e:
            logger.error(f"Error processing ticket {ticket_idx+1}: {e}")
            continue

    logger.info("\n🏁 All unpurchased tickets processed")


async def main():
    """メイン処理"""
    try:
        logger.info("🚀 STARTING AKATSUKI BOT - SIMPLE VERSION")

        # 1. 設定を読み込む
        credentials, slack_info, tickets_path = await load_configuration()

        # 2. ブラウザとセッションを初期化
        async with async_playwright() as p:
            browser, context, page = await initialize_browser_and_session(p, credentials)

            # DRY_RUNモード通知
            DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
            if DRY_RUN:
                logger.warning("=" * 60)
                logger.warning("🔸 DRY_RUN MODE ENABLED")
                logger.warning("=" * 60)

            # 3. チケット読み込みと突合
            tickets, reconciliation_results, to_purchase = await load_and_reconcile_tickets(page, tickets_path)

            # 全てのチケットが既に購入済みの場合
            if len(to_purchase) == 0:
                logger.info("✅ All tickets already purchased! Nothing to do.")
                await browser.close()
                return

            # 4. DRY_RUNモードの処理
            if await handle_dry_run_mode(page, to_purchase, reconciliation_results):
                await browser.close()
                return

            # ===== 通常モード: 実際に購入 =====

            # 5. 残高確認と入金
            if not await ensure_sufficient_balance(page, credentials, to_purchase):
                await browser.close()
                return

            # 6. チケット処理
            await process_tickets(page, to_purchase)

            await browser.close()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
