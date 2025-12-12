"""
S3ベースの購入履歴管理サービス

Lambda経由でもローカル実行でも同じS3履歴チェックが適用される共通サービス層。
IPAT投票履歴との二重チェックにより、冪等性を確保。
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@dataclass
class PurchaseRecord:
    """購入履歴レコード"""
    race_course: str
    race_number: int
    horse_number: int
    bet_type: str
    amount: int
    status: str  # "PURCHASED" or "FAILED"
    purchased_at: str
    error_message: Optional[str] = None


class PurchaseHistoryService:
    """
    S3ベースの購入履歴管理サービス

    購入前チェックと購入後記録を担当。
    Lambda/ローカル両方で動作可能。
    """

    # デフォルトバケット名
    DEFAULT_BUCKET = "jrdb-main-financial-data"

    # S3パスプレフィックス
    S3_PREFIX = "purchase-history"

    def __init__(self, bucket_name: Optional[str] = None, region: str = "ap-northeast-1"):
        """
        Args:
            bucket_name: S3バケット名（Noneの場合は環境変数またはデフォルト）
            region: AWSリージョン
        """
        self.bucket_name = (
            bucket_name
            or os.environ.get("PURCHASE_HISTORY_BUCKET")
            or os.environ.get("OUTPUT_BUCKET")
            or self.DEFAULT_BUCKET
        )
        self.s3_client = boto3.client("s3", region_name=region)
        self._cache: Dict[str, Dict[str, Any]] = {}  # target_date -> history

        logger.info(f"PurchaseHistoryService initialized with bucket: {self.bucket_name}")

    def _get_s3_key(self, target_date: str) -> str:
        """S3キーを生成（YYYYMMDD形式）"""
        # purchase-history/YYYYMMDD/tickets.json
        return f"{self.S3_PREFIX}/{target_date}/tickets.json"

    def load_history(self, target_date: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        S3から購入履歴を読み込み

        Args:
            target_date: 対象日（YYYYMMDD形式）
            use_cache: キャッシュを使用するか

        Returns:
            購入履歴（存在しない場合は空の構造）
        """
        if use_cache and target_date in self._cache:
            logger.debug(f"Using cached history for {target_date}")
            return self._cache[target_date]

        key = self._get_s3_key(target_date)

        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
            history = json.loads(response["Body"].read().decode("utf-8"))
            logger.info(f"Loaded purchase history from s3://{self.bucket_name}/{key}: {len(history.get('tickets', []))} records")
            self._cache[target_date] = history
            return history

        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.info(f"No purchase history found for {target_date}, starting fresh")
                empty_history = {
                    "target_date": target_date,
                    "tickets": [],
                    "last_updated": None
                }
                self._cache[target_date] = empty_history
                return empty_history
            logger.error(f"Failed to load purchase history: {e}")
            raise

    def save_history(self, target_date: str, history: Dict[str, Any]) -> None:
        """
        購入履歴をS3に保存

        Args:
            target_date: 対象日（YYYYMMDD形式）
            history: 購入履歴
        """
        key = self._get_s3_key(target_date)
        history["last_updated"] = datetime.now(timezone.utc).isoformat()

        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=key,
                Body=json.dumps(history, ensure_ascii=False, indent=2),
                ContentType="application/json"
            )
            logger.info(f"Saved purchase history to s3://{self.bucket_name}/{key}")
            self._cache[target_date] = history

        except ClientError as e:
            logger.error(f"Failed to save purchase history: {e}")
            raise

    def is_already_purchased(self, ticket: Any, target_date: str) -> bool:
        """
        S3履歴で購入済みかチェック

        Args:
            ticket: Ticketオブジェクト（racecourse, race_number, horse_number, bet_type, amount属性を持つ）
            target_date: 対象日（YYYYMMDD形式）

        Returns:
            購入済みならTrue
        """
        history = self.load_history(target_date)
        tickets = history.get("tickets", [])

        for record in tickets:
            # 購入成功（PURCHASED）のレコードのみチェック
            # UNVERIFIED（画面成功・照会失敗）はIPATの履歴チェックに任せる
            # FAILEDは明確に失敗なのでスキップ
            if record.get("status") != "PURCHASED":
                continue

            if self._matches(ticket, record):
                logger.info(f"S3 history match found: {ticket.racecourse} {ticket.race_number}R {ticket.horse_number}番")
                return True

        return False

    def _matches(self, ticket: Any, record: Dict[str, Any]) -> bool:
        """
        5項目一致判定（既存のTicket.matchesと同じロジック）

        Args:
            ticket: Ticketオブジェクト
            record: 購入履歴レコード

        Returns:
            一致すればTrue
        """
        return (
            ticket.racecourse == record.get("race_course") and
            ticket.race_number == record.get("race_number") and
            ticket.horse_number == record.get("horse_number") and
            ticket.bet_type == record.get("bet_type") and
            ticket.amount == record.get("amount")
        )

    def record_purchase(self, ticket: Any, target_date: str) -> None:
        """
        購入成功を即時記録

        Args:
            ticket: 購入したTicketオブジェクト
            target_date: 対象日（YYYYMMDD形式）
        """
        history = self.load_history(target_date)

        record = {
            "race_course": ticket.racecourse,
            "race_number": ticket.race_number,
            "horse_number": ticket.horse_number,
            "bet_type": ticket.bet_type,
            "amount": ticket.amount,
            "status": "PURCHASED",
            "purchased_at": datetime.now(timezone.utc).isoformat()
        }

        history["tickets"].append(record)
        self.save_history(target_date, history)

        logger.info(f"Recorded purchase: {ticket.racecourse} {ticket.race_number}R {ticket.horse_number}番 {ticket.amount}円")

    def record_purchase_error(self, ticket: Any, target_date: str, error_message: str) -> None:
        """
        購入エラーを記録（監査証跡用）

        Args:
            ticket: 購入を試みたTicketオブジェクト
            target_date: 対象日（YYYYMMDD形式）
            error_message: エラーメッセージ
        """
        history = self.load_history(target_date)

        record = {
            "race_course": ticket.racecourse,
            "race_number": ticket.race_number,
            "horse_number": ticket.horse_number,
            "bet_type": ticket.bet_type,
            "amount": ticket.amount,
            "status": "FAILED",
            "purchased_at": datetime.now(timezone.utc).isoformat(),
            "error_message": error_message
        }

        history["tickets"].append(record)
        self.save_history(target_date, history)

        logger.warning(f"Recorded purchase error: {ticket.racecourse} {ticket.race_number}R {ticket.horse_number}番 - {error_message}")

    def record_unverified_purchase(self, ticket: Any, target_date: str) -> None:
        """
        購入未確認を記録（画面成功だが照会未確認）

        Args:
            ticket: 購入を試みたTicketオブジェクト
            target_date: 対象日（YYYYMMDD形式）
        """
        history = self.load_history(target_date)

        record = {
            "race_course": ticket.racecourse,
            "race_number": ticket.race_number,
            "horse_number": ticket.horse_number,
            "bet_type": ticket.bet_type,
            "amount": ticket.amount,
            "status": "UNVERIFIED",
            "purchased_at": datetime.now(timezone.utc).isoformat(),
            "note": "Screen showed success but inquiry verification failed"
        }

        history["tickets"].append(record)
        self.save_history(target_date, history)

        logger.warning(f"Recorded unverified purchase: {ticket.racecourse} {ticket.race_number}R {ticket.horse_number}番 - inquiry verification failed")

    def get_purchase_summary(self, target_date: str) -> Dict[str, int]:
        """
        購入サマリーを取得

        Args:
            target_date: 対象日（YYYYMMDD形式）

        Returns:
            {purchased: int, failed: int, total: int}
        """
        history = self.load_history(target_date)
        tickets = history.get("tickets", [])

        purchased = sum(1 for t in tickets if t.get("status") == "PURCHASED")
        failed = sum(1 for t in tickets if t.get("status") == "FAILED")

        return {
            "purchased": purchased,
            "failed": failed,
            "total": len(tickets)
        }

    def clear_cache(self, target_date: Optional[str] = None) -> None:
        """
        キャッシュをクリア

        Args:
            target_date: 特定の日付のみクリア（Noneで全クリア）
        """
        if target_date:
            self._cache.pop(target_date, None)
        else:
            self._cache.clear()
        logger.debug(f"Cache cleared: {target_date or 'all'}")
