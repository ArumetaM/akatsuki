"""
AWS Lambda Handler for akatsuki purchase automation

saudade推論結果からIPAT馬券購入を自動実行するLambda関数

入力イベント:
- inference_result: 推論Lambdaからの結果（S3パス含む）
- target_date: 購入対象日（YYYYMMDD形式、'auto'で当日）
- dry_run: テストモード（true/false）

処理フロー:
1. S3から推論結果CSVダウンロード
2. S3から購入金額設定CSVダウンロード
3. CSV→akatsuki形式に変換（PlaceName→race_course等）
4. Playwrightでブラウザ起動
5. IPATログイン（Secrets Manager認証）
6. チケット処理（購入実行）
7. 結果記録（S3/Slack）
"""

import os
import sys
import json
import logging
import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from slack_service import SlackService

# Slack通知サービス（遅延初期化）
_slack_service = None


def get_slack_service() -> SlackService:
    """SlackServiceのシングルトン取得"""
    global _slack_service
    if _slack_service is None:
        _slack_service = SlackService()
    return _slack_service


# Lambda環境用の設定
# HOME=/tmp と PLAYWRIGHT_BROWSERS_PATH=/ms-playwright は Dockerfile で設定済み
# os.environ設定による上書きを避けるためコード側での設定は行わない

# Lambda環境では/tmpのみ書き込み可能
# bot_simple.pyがカレントディレクトリに書き込むため、/tmpに移動
os.chdir('/tmp')
os.makedirs('/tmp/output', exist_ok=True)
os.makedirs('/tmp/output/screenshots', exist_ok=True)

# scriptsディレクトリをパスに追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
sys.path.insert(0, '/var/task/scripts')

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class PurchaseConfig:
    """購入設定"""
    target_date: str
    dry_run: bool
    source_bucket: str
    output_bucket: str
    bet_amount: int


class S3Client:
    """S3操作クライアント"""

    def __init__(self, region: str = 'ap-northeast-1'):
        self.s3 = boto3.client('s3', region_name=region)
        self.region = region

    def download_inference_results(self, bucket: str, target_date: str) -> Optional[pd.DataFrame]:
        """
        推論結果CSVをS3からダウンロード

        Args:
            bucket: S3バケット名
            target_date: 対象日（YYYYMMDD）

        Returns:
            推論結果のDataFrame（見つからない場合はNone）
        """
        # S3パス: inference-results/YYYY/MM/DD/phase58_bets_YYYYMMDD.csv
        year = target_date[:4]
        month = target_date[4:6]
        day = target_date[6:8]
        key = f"inference-results/{year}/{month}/{day}/phase58_bets_{target_date}.csv"

        logger.info(f"Downloading inference results from s3://{bucket}/{key}")

        try:
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False) as f:
                self.s3.download_fileobj(bucket, key, f)
                temp_path = f.name

            df = pd.read_csv(temp_path)
            os.unlink(temp_path)

            logger.info(f"Downloaded inference results: {len(df)} rows")
            return df

        except ClientError as e:
            if e.response['Error']['Code'] == '404' or e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"Inference results not found: {key}")
                return None
            raise

    def get_bet_amount_schedule(self, bucket: str, target_date: str) -> int:
        """
        購入金額設定CSVから対象日の金額を取得

        Args:
            bucket: S3バケット名
            target_date: 対象日（YYYYMMDD）

        Returns:
            購入金額（見つからない場合はデフォルト5000円）
        """
        key = "config/bet_amount_schedule.csv"
        default_amount = 5000

        try:
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False) as f:
                self.s3.download_fileobj(bucket, key, f)
                temp_path = f.name

            df = pd.read_csv(temp_path)
            os.unlink(temp_path)

            # 対象日の金額を検索
            target_int = int(target_date)
            for _, row in df.iterrows():
                start = int(row['start_date'])
                end = int(row['end_date'])
                if start <= target_int <= end:
                    amount = int(row['amount'])
                    logger.info(f"Bet amount for {target_date}: {amount}円")
                    return amount

            logger.warning(f"No matching period for {target_date}, using default: {default_amount}円")
            return default_amount

        except ClientError as e:
            if e.response['Error']['Code'] == '404' or e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"Bet amount schedule not found, using default: {default_amount}円")
                return default_amount
            raise
        except Exception as e:
            logger.warning(f"Error reading bet amount schedule: {e}, using default: {default_amount}円")
            return default_amount

    def upload_results(self, bucket: str, target_date: str, results: Dict[str, Any]) -> str:
        """
        購入結果をS3にアップロード

        Args:
            bucket: S3バケット名
            target_date: 対象日（YYYYMMDD）
            results: 購入結果の辞書

        Returns:
            アップロードしたS3キー
        """
        year = target_date[:4]
        month = target_date[4:6]
        day = target_date[6:8]
        timestamp = datetime.now().strftime("%H%M%S")
        key = f"purchase-results/{year}/{month}/{day}/purchase_result_{target_date}_{timestamp}.json"

        self.s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(results, ensure_ascii=False, indent=2),
            ContentType='application/json'
        )

        logger.info(f"Uploaded results to s3://{bucket}/{key}")
        return key


def convert_inference_to_tickets(df: pd.DataFrame, bet_amount: int) -> List[Dict[str, Any]]:
    """
    推論結果をakatsuki形式のチケットに変換

    saudade出力:
    - PlaceName: 競馬場名（東京、中山等）
    - RaceNumber: レース番号
    - HorseNumber: 馬番
    - HorseName: 馬名

    akatsuki入力:
    - race_course: 競馬場
    - race_number: レース番号
    - bet_type: 券種（固定: 単勝）
    - horse_number: 馬番
    - horse_name: 馬名
    - amount: 金額
    """
    tickets = []

    for _, row in df.iterrows():
        ticket = {
            'race_course': row['PlaceName'],
            'race_number': int(row['RaceNumber']),
            'bet_type': '単勝',
            'horse_number': int(row['HorseNumber']),
            'horse_name': row.get('HorseName', ''),
            'amount': bet_amount
        }
        tickets.append(ticket)

    logger.info(f"Converted {len(tickets)} tickets with amount {bet_amount}円 each")
    return tickets


def save_tickets_csv(tickets: List[Dict[str, Any]], output_path: str) -> None:
    """チケットをCSVファイルとして保存"""
    df = pd.DataFrame(tickets)
    df.to_csv(output_path, index=False)
    logger.info(f"Saved {len(tickets)} tickets to {output_path}")


async def run_purchase_bot(tickets_path: str, dry_run: bool = True) -> Dict[str, Any]:
    """
    購入Botを実行

    Args:
        tickets_path: チケットCSVのパス
        dry_run: DRY_RUNモード

    Returns:
        購入結果の辞書
    """
    from playwright.async_api import async_playwright

    # 環境変数設定
    os.environ['DRY_RUN'] = 'true' if dry_run else 'false'
    os.environ['TICKETS_PATH'] = tickets_path

    # bot_simple.pyの関数をインポート
    try:
        from bot_simple import (
            get_all_secrets,
            login_simple,
            load_and_reconcile_tickets,
            handle_dry_run_mode,
            ensure_sufficient_balance,
            process_tickets,
        )
        from constants import Timeouts
    except ImportError as e:
        logger.error(f"Failed to import bot_simple modules: {e}")
        raise

    results = {
        'status': 'unknown',
        'tickets_total': 0,
        'tickets_purchased': 0,
        'tickets_skipped': 0,
        'tickets_failed': 0,
        'dry_run': dry_run,
        'error': None
    }

    try:
        # 認証情報取得
        credentials, slack_info = await get_all_secrets()

        # Playwright起動（Lambda用設定）
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--single-process',
                    '--no-zygote',
                ]
            )

            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                locale='ja-JP',
                timezone_id='Asia/Tokyo'
            )

            page = await context.new_page()

            # ログイン
            logger.info("Logging in to IPAT...")
            if not await login_simple(page, credentials):
                raise Exception("IPAT login failed")

            # チケット読み込みと突合（Pathオブジェクトとして渡す）
            from pathlib import Path
            tickets, reconciliation_results, to_purchase = await load_and_reconcile_tickets(
                page, Path(tickets_path)
            )

            results['tickets_total'] = len(tickets)
            results['tickets_skipped'] = len(tickets) - len(to_purchase)

            if len(to_purchase) == 0:
                logger.info("All tickets already purchased!")
                results['status'] = 'success'
                results['message'] = 'All tickets already purchased'
                await browser.close()
                return results

            # DRY_RUNモード
            if dry_run:
                await handle_dry_run_mode(page, to_purchase, reconciliation_results)
                results['status'] = 'dry_run'
                results['tickets_to_purchase'] = len(to_purchase)
                total_cost = sum(t.amount for t in to_purchase)
                results['total_cost'] = total_cost
                await browser.close()
                return results

            # 残高確認と入金
            if not await ensure_sufficient_balance(page, credentials, to_purchase):
                raise Exception("Insufficient balance and deposit failed")

            # 購入実行
            await process_tickets(page, to_purchase)

            results['status'] = 'success'
            results['tickets_purchased'] = len(to_purchase)

            await browser.close()

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Purchase bot error: {e}")
        logger.error(f"Full traceback:\n{error_trace}")
        results['status'] = 'error'
        results['error'] = str(e)

    return results


def get_target_date(event: Dict[str, Any]) -> str:
    """
    イベントからtarget_dateを取得

    Args:
        event: Lambdaイベント

    Returns:
        YYYYMMDD形式の日付文字列
    """
    target_date = event.get('target_date', 'auto')

    if target_date == 'auto':
        # 当日の日付を使用
        return datetime.now().strftime('%Y%m%d')

    return target_date


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler

    Args:
        event: Lambdaイベント
            - target_date: 対象日（YYYYMMDD、'auto'で当日）
            - dry_run: DRY_RUNモード（デフォルト: true）
            - inference_result: 推論結果（Step Functionsからの入力）
        context: Lambdaコンテキスト

    Returns:
        実行結果の辞書
    """
    logger.info(f"Lambda handler started: {json.dumps(event)}")

    # Slack通知サービス取得
    slack = get_slack_service()
    target_date = 'unknown'

    try:
        # 設定取得
        target_date = get_target_date(event)
        dry_run = event.get('dry_run', True)

        # Step Functionsからの入力があれば使用
        inference_result = event.get('inference_result', {})
        if isinstance(inference_result, dict) and 'Payload' in inference_result:
            inference_result = inference_result['Payload']

        # バケット名
        source_bucket = event.get('output_bucket', 'jrdb-main-financial-data')
        output_bucket = event.get('output_bucket', 'jrdb-main-financial-data')

        logger.info(f"Target date: {target_date}, DRY_RUN: {dry_run}")

        # S3クライアント
        s3_client = S3Client()

        # 推論結果をダウンロード
        inference_df = s3_client.download_inference_results(source_bucket, target_date)

        if inference_df is None or len(inference_df) == 0:
            # 購入候補なし通知
            slack.send_no_bets(target_date)
            return {
                'statusCode': 200,
                'body': {
                    'status': 'no_bets',
                    'message': f'No inference results for {target_date}',
                    'target_date': target_date
                }
            }

        # 購入金額取得
        bet_amount = s3_client.get_bet_amount_schedule(output_bucket, target_date)

        # チケット形式に変換
        tickets = convert_inference_to_tickets(inference_df, bet_amount)

        # 開始通知
        slack.send_purchase_start(target_date, len(tickets), dry_run)

        # 一時ファイルにCSV保存
        tickets_path = '/tmp/tickets.csv'
        save_tickets_csv(tickets, tickets_path)

        # 購入Bot実行
        results = asyncio.get_event_loop().run_until_complete(
            run_purchase_bot(tickets_path, dry_run)
        )

        # 結果をS3にアップロード
        results['target_date'] = target_date
        results['bet_amount'] = bet_amount
        results['timestamp'] = datetime.now().isoformat()

        result_key = s3_client.upload_results(output_bucket, target_date, results)
        results['result_s3_key'] = result_key

        # 完了通知
        purchased = results.get('tickets_purchased', 0) or results.get('tickets_to_purchase', 0)
        skipped = results.get('tickets_skipped', 0)
        total_cost = results.get('total_cost', purchased * bet_amount)
        slack.send_purchase_complete(target_date, purchased, skipped, total_cost, dry_run)

        return {
            'statusCode': 200,
            'body': results
        }

    except Exception as e:
        logger.error(f"Lambda handler error: {e}")
        # エラー通知
        slack.send_error(target_date, str(e))
        return {
            'statusCode': 500,
            'body': {
                'status': 'error',
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            }
        }


# ローカルテスト用
if __name__ == '__main__':
    test_event = {
        'target_date': 'auto',
        'dry_run': True
    }

    result = handler(test_event, None)
    print(json.dumps(result, indent=2, ensure_ascii=False))
