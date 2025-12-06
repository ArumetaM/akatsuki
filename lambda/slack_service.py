"""
Slack通知サービス（Lambda同期版）

akatsuki購入Lambda用のSlack通知機能
- 処理開始・完了・エラー時の通知
- 購入成功時の個別通知
- Secrets ManagerからSlackトークン取得
"""
import os
import json
import logging
import requests
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class SlackService:
    """Slack通知サービス（同期版）"""

    def __init__(self):
        self.bot_token = None
        self.ops_channel = None
        self.alerts_channel = None
        self.bets_channel = None
        self._initialize()

    def _initialize(self):
        """Slack認証情報と設定の初期化"""
        try:
            # シークレットマネージャーからBot token取得
            try:
                token = self._get_slack_token()
                self.bot_token = token
                if token:
                    logger.info("SlackボットトークンをSecrets Managerから取得しました")
            except Exception as e:
                logger.warning(f"Secrets Managerからのボットトークン取得に失敗: {str(e)}")
                # 環境変数からのフォールバック
                self.bot_token = os.environ.get("SLACK_BOT_TOKEN")
                if self.bot_token:
                    logger.info("Slackボットトークンを環境変数から取得しました")

            # 環境変数からチャンネルID取得
            self.ops_channel = os.environ.get("SLACK_channel_id_ops")
            self.alerts_channel = os.environ.get("SLACK_channel_id_alerts")
            self.bets_channel = os.environ.get("SLACK_channel_id_bets_live")

            if not self.bot_token:
                logger.warning("Slackボットトークンが設定されていません")
            if not self.ops_channel:
                logger.warning("Slackチャンネル ID (SLACK_channel_id_ops) が設定されていません")
            if not self.bets_channel:
                logger.warning("Slack購入チャンネル ID (SLACK_channel_id_bets_live) が設定されていません")

        except Exception as e:
            logger.error(f"Slack設定の初期化に失敗しました: {str(e)}")

    def _get_slack_token(self) -> str:
        """Secrets Managerからトークン取得"""
        try:
            client = boto3.client('secretsmanager', region_name='ap-northeast-1')
            response = client.get_secret_value(SecretId='keiba_secret')
            secrets = json.loads(response['SecretString'])
            return secrets.get('slack_bot_user_oauth_token', '')
        except ClientError as e:
            logger.warning(f"Secrets Manager access failed: {e}")
            return ''

    def is_configured(self) -> bool:
        """Slack通知が設定されているかチェック"""
        return bool(self.bot_token and self.ops_channel)

    def send_purchase_start(self, target_date: str, ticket_count: int, dry_run: bool = True) -> bool:
        """購入処理開始通知"""
        formatted_date = self._format_date(target_date)
        mode = "（DRY_RUN）" if dry_run else ""

        msg = f":ticket: 購入処理開始{mode}: {formatted_date}\n"
        msg += f"   購入候補: {ticket_count}件"

        return self._send_message(self.ops_channel, msg)

    def send_purchase_complete(self, target_date: str, purchased: int, skipped: int, total_cost: int, dry_run: bool = True) -> bool:
        """購入処理完了通知"""
        formatted_date = self._format_date(target_date)
        mode = "（DRY_RUN）" if dry_run else ""

        if dry_run:
            msg = f":white_check_mark: 購入処理完了{mode}: {formatted_date}\n"
            msg += f"   購入予定: {purchased}件\n"
            msg += f"   スキップ: {skipped}件\n"
            msg += f"   合計金額: ¥{total_cost:,}"
        else:
            msg = f":white_check_mark: 購入処理完了: {formatted_date}\n"
            msg += f"   購入済み: {purchased}件\n"
            msg += f"   スキップ: {skipped}件\n"
            msg += f"   合計金額: ¥{total_cost:,}"

        return self._send_message(self.ops_channel, msg)

    def send_no_bets(self, target_date: str) -> bool:
        """購入候補なし通知"""
        formatted_date = self._format_date(target_date)
        msg = f":information_source: 購入候補なし: {formatted_date}\n"
        msg += "   推論結果が見つかりません"

        return self._send_message(self.ops_channel, msg)

    def send_bet_notification(self, race_course: str, race_number: int,
                             horse_number: int, horse_name: str,
                             amount: int, success: bool = True) -> bool:
        """個別購入通知（bets-liveチャンネル）"""
        if success:
            msg = f":horse_racing: {race_course} {race_number}R\n"
            msg += f"   {horse_number}番 {horse_name}\n"
            msg += f"   ¥{amount:,}"
        else:
            msg = f":x: 購入失敗: {race_course} {race_number}R\n"
            msg += f"   {horse_number}番 {horse_name}"

        return self._send_message(self.bets_channel, msg)

    def send_error(self, target_date: str, error_msg: str) -> bool:
        """エラー通知"""
        formatted_date = self._format_date(target_date)
        return self._send_message(
            self.alerts_channel or self.ops_channel,
            f":x: 購入処理エラー: {formatted_date}\n   {error_msg}"
        )

    def send_deposit_failed(self, target_date: str, requested_amount: int, actual_balance: int) -> bool:
        """入金失敗通知（銀行口座残高不足の可能性）"""
        formatted_date = self._format_date(target_date)
        msg = f":rotating_light: 入金失敗（銀行口座残高不足の可能性）: {formatted_date}\n"
        msg += f"   入金リクエスト額: ¥{requested_amount:,}\n"
        msg += f"   現在のIPAT残高: ¥{actual_balance:,}\n"
        msg += f"   :warning: 銀行口座への入金を確認してください"
        return self._send_message(self.alerts_channel or self.ops_channel, msg)

    def send_purchase_verification_failed(
        self,
        target_date: str,
        race_course: str,
        race_number: int,
        horse_number: int,
        horse_name: str,
        amount: int
    ) -> bool:
        """購入検証失敗通知（画面では成功したが照会で確認できなかった）"""
        formatted_date = self._format_date(target_date)
        msg = f":warning: 購入検証失敗: {formatted_date}\n"
        msg += f"   {race_course} {race_number}R {horse_number}番 {horse_name}\n"
        msg += f"   金額: ¥{amount:,}\n"
        msg += f"   :exclamation: 画面上は成功表示でしたが、照会メニューで確認できませんでした\n"
        msg += f"   手動でIPATを確認してください"

        return self._send_message(self.alerts_channel or self.ops_channel, msg)

    def _format_date(self, date: str) -> str:
        """日付フォーマット（YYYYMMDD → YYYY/MM/DD）"""
        if len(date) == 8:
            return f"{date[:4]}/{date[4:6]}/{date[6:8]}"
        return date

    def _send_message(self, channel: str, text: str) -> bool:
        """Slack APIでメッセージ送信"""
        if not self.is_configured():
            logger.info(f"Slack通知スキップ（未設定）: {text}")
            return False

        if not channel:
            logger.warning(f"チャンネル未設定のためスキップ: {text}")
            return False

        try:
            response = requests.post(
                'https://slack.com/api/chat.postMessage',
                headers={
                    'Authorization': f'Bearer {self.bot_token}',
                    'Content-Type': 'application/json'
                },
                json={
                    'channel': channel,
                    'text': text
                },
                timeout=30
            )

            result = response.json()
            if not result.get('ok'):
                logger.error(f"Slack API エラー: {result.get('error', 'unknown')}")
                return False

            logger.info(f"Slack通知送信完了: {text[:50]}...")
            return True

        except requests.exceptions.Timeout:
            logger.error("Slack API タイムアウト")
            return False
        except Exception as e:
            logger.error(f"Slack通知失敗: {e}")
            return False
