"""
AWS Lambda Handler for purchase result evaluation

購入した馬券の的中/不的中を評価し、ROI・的中率を算出するLambda関数

入力イベント:
- target_date: 評価対象日（YYYYMMDD形式、'auto'で当日）
- backfill: 過去データ遡及評価モード（true/false）
- start_date: 遡及開始日（backfill時のみ）

処理フロー:
1. S3から購入履歴取得（purchase-results/）
2. S3から推論結果取得（inference-results/）
3. S3からレース結果取得（source-data HJCファイル）
4. 的中判定・払戻金計算
5. 評価結果をS3に保存（evaluation-results/）
6. Slack通知（日次サマリー）
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from slack_service import SlackService

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 競馬場コード変換テーブル
PLACE_CODE_MAP = {
    '札幌': '01', '函館': '02', '福島': '03', '新潟': '04',
    '東京': '05', '中山': '06', '中京': '07', '京都': '08',
    '阪神': '09', '小倉': '10'
}
CODE_TO_PLACE_MAP = {v: k for k, v in PLACE_CODE_MAP.items()}


@dataclass
class BetDetail:
    """購入詳細"""
    race_course: str
    race_number: int
    horse_number: int
    horse_name: str
    amount: int
    is_hit: bool = False
    payout: int = 0
    odds: float = 0.0
    finish_position: int = 0


@dataclass
class DailySummary:
    """日次サマリー"""
    total_bets: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    total_investment: int = 0
    total_payout: int = 0
    roi: float = 0.0
    profit: int = 0


@dataclass
class CumulativeSummary:
    """累計サマリー"""
    total_bets: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    total_investment: int = 0
    total_payout: int = 0
    roi: float = 0.0
    profit: int = 0
    current_streak: int = 0  # 正:連勝、負:連敗
    max_drawdown: int = 0


class EvaluatorService:
    """評価サービス"""

    def __init__(self, region: str = 'ap-northeast-1'):
        self.s3 = boto3.client('s3', region_name=region)
        self.source_bucket = os.environ.get('SOURCE_BUCKET', 'jrdb-main-source-data')
        self.financial_bucket = os.environ.get('FINANCIAL_BUCKET', 'jrdb-main-financial-data')

    def get_inference_results(self, target_date: str) -> Optional[pd.DataFrame]:
        """推論結果CSVをS3から取得"""
        year = target_date[:4]
        month = target_date[4:6]
        day = target_date[6:8]
        key = f"inference-results/{year}/{month}/{day}/phase58_bets_{target_date}.csv"

        try:
            response = self.s3.get_object(Bucket=self.financial_bucket, Key=key)
            df = pd.read_csv(response['Body'])
            logger.info(f"推論結果取得: {len(df)}件")
            return df
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"推論結果なし: {key}")
                return None
            raise

    def get_race_results(self, target_date: str) -> Optional[pd.DataFrame]:
        """レース結果（HJCファイル）をS3から取得"""
        year = target_date[:4]
        month = target_date[4:6]
        day = target_date[6:8]
        yy = target_date[2:4]  # HJCファイル名は YYMMDD 形式
        key = f"csv/{year}/{month}/{day}/HJC/HJC_{yy}{month}{day}.csv"

        try:
            response = self.s3.get_object(Bucket=self.source_bucket, Key=key)
            df = pd.read_csv(response['Body'])
            logger.info(f"レース結果取得: {len(df)}件")
            return df
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"レース結果なし: {key}")
                return None
            raise

    def evaluate_bets(self, inference_df: pd.DataFrame, results_df: pd.DataFrame,
                      bet_amount: int = 5000) -> List[BetDetail]:
        """購入馬券の評価"""
        evaluated = []

        for _, row in inference_df.iterrows():
            place_name = row['PlaceName']
            race_number = int(row['RaceNumber'])
            horse_number = int(row['HorseNumber'])
            horse_name = row.get('HorseName', '')

            # 場コード変換
            place_code = PLACE_CODE_MAP.get(place_name, '')
            if not place_code:
                logger.warning(f"不明な競馬場: {place_name}")
                continue

            # レース結果を検索
            race_result = results_df[
                (results_df['PlaceCode'].astype(str).str.zfill(2) == place_code) &
                (results_df['RaceNumber'] == race_number)
            ]

            if race_result.empty:
                logger.warning(f"レース結果なし: {place_name} {race_number}R")
                continue

            race_row = race_result.iloc[0]

            # 単勝的中判定（Win_HorseNumber1が1着）
            win_horse = int(race_row.get('Win_HorseNumber1', 0))
            is_hit = (win_horse == horse_number)

            # 払戻金計算
            payout = 0
            odds = 0.0
            finish_position = 0

            if is_hit:
                # 単勝払戻金（100円単位）* 購入口数
                win_payout = int(race_row.get('Win_Payout1', 0))
                units = bet_amount // 100  # 100円単位
                payout = win_payout * units
                odds = win_payout / 100.0
                finish_position = 1

            detail = BetDetail(
                race_course=place_name,
                race_number=race_number,
                horse_number=horse_number,
                horse_name=horse_name,
                amount=bet_amount,
                is_hit=is_hit,
                payout=payout,
                odds=odds,
                finish_position=finish_position
            )
            evaluated.append(detail)

        return evaluated

    def calculate_daily_summary(self, details: List[BetDetail]) -> DailySummary:
        """日次サマリー計算"""
        if not details:
            return DailySummary()

        total_bets = len(details)
        hits = sum(1 for d in details if d.is_hit)
        total_investment = sum(d.amount for d in details)
        total_payout = sum(d.payout for d in details)

        return DailySummary(
            total_bets=total_bets,
            hits=hits,
            hit_rate=round(hits / total_bets, 4) if total_bets > 0 else 0.0,
            total_investment=total_investment,
            total_payout=total_payout,
            roi=round((total_payout - total_investment) / total_investment * 100, 2) if total_investment > 0 else 0.0,
            profit=total_payout - total_investment
        )

    def get_cumulative_summary(self, current_date: str) -> CumulativeSummary:
        """累計サマリー取得（過去の評価結果を集計）"""
        cumulative = CumulativeSummary()

        # 過去の評価結果を取得して集計
        try:
            paginator = self.s3.get_paginator('list_objects_v2')
            prefix = "evaluation-results/"

            running_balance = 0
            max_balance = 0
            streak = 0
            last_hit = None
            all_details = []

            for page in paginator.paginate(Bucket=self.financial_bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if 'daily_' in key and key.endswith('.json'):
                        # 対象日以前のデータのみ
                        date_part = key.split('daily_')[1].split('.')[0]
                        if date_part >= current_date:
                            continue

                        response = self.s3.get_object(Bucket=self.financial_bucket, Key=key)
                        data = json.loads(response['Body'].read().decode('utf-8'))

                        if 'details' in data:
                            for detail in data['details']:
                                cumulative.total_bets += 1
                                if detail.get('is_hit'):
                                    cumulative.hits += 1
                                cumulative.total_investment += detail.get('amount', 0)
                                cumulative.total_payout += detail.get('payout', 0)
                                all_details.append(detail)

            # 集計計算
            if cumulative.total_bets > 0:
                cumulative.hit_rate = round(cumulative.hits / cumulative.total_bets, 4)

            if cumulative.total_investment > 0:
                cumulative.roi = round(
                    (cumulative.total_payout - cumulative.total_investment) /
                    cumulative.total_investment * 100, 2
                )

            cumulative.profit = cumulative.total_payout - cumulative.total_investment

            # 連勝/連敗計算（時系列順にソート）
            all_details.sort(key=lambda x: (x.get('race_course', ''), x.get('race_number', 0)))
            for detail in reversed(all_details):
                is_hit = detail.get('is_hit', False)
                if last_hit is None:
                    streak = 1 if is_hit else -1
                    last_hit = is_hit
                elif is_hit == last_hit:
                    streak += 1 if is_hit else -1
                else:
                    break
            cumulative.current_streak = streak

            # 最大ドローダウン計算
            running_balance = 0
            max_balance = 0
            for detail in all_details:
                profit = detail.get('payout', 0) - detail.get('amount', 0)
                running_balance += profit
                if running_balance > max_balance:
                    max_balance = running_balance
                drawdown = max_balance - running_balance
                if drawdown > cumulative.max_drawdown:
                    cumulative.max_drawdown = drawdown

        except Exception as e:
            logger.warning(f"累計サマリー取得エラー: {e}")

        return cumulative

    def save_evaluation_result(self, target_date: str, summary: DailySummary,
                               details: List[BetDetail], cumulative: CumulativeSummary) -> str:
        """評価結果をS3に保存"""
        year = target_date[:4]
        month = target_date[4:6]
        day = target_date[6:8]
        key = f"evaluation-results/{year}/{month}/{day}/daily_{target_date}.json"

        result = {
            'date': target_date,
            'summary': asdict(summary),
            'details': [asdict(d) for d in details],
            'cumulative': asdict(cumulative),
            'timestamp': datetime.now().isoformat()
        }

        self.s3.put_object(
            Bucket=self.financial_bucket,
            Key=key,
            Body=json.dumps(result, ensure_ascii=False, indent=2),
            ContentType='application/json'
        )

        logger.info(f"評価結果保存: s3://{self.financial_bucket}/{key}")
        return key


class EvaluatorSlackService(SlackService):
    """評価用Slack通知サービス（継承）"""

    def send_daily_evaluation(self, target_date: str, summary: DailySummary,
                              cumulative: CumulativeSummary) -> bool:
        """日次評価通知"""
        formatted_date = self._format_date(target_date)

        msg = f":bar_chart: {formatted_date} 購入結果評価\n\n"

        # 本日
        msg += "*【本日】*\n"
        msg += f"   的中: {summary.hits}/{summary.total_bets} ({summary.hit_rate*100:.1f}%)\n"
        msg += f"   投資: ¥{summary.total_investment:,}\n"
        msg += f"   払戻: ¥{summary.total_payout:,}\n"
        msg += f"   損益: ¥{summary.profit:+,} (ROI: {summary.roi:+.1f}%)\n\n"

        # 累計（データがある場合）
        if cumulative.total_bets > 0:
            msg += "*【累計】*\n"
            msg += f"   的中: {cumulative.hits}/{cumulative.total_bets} ({cumulative.hit_rate*100:.1f}%)\n"
            msg += f"   投資: ¥{cumulative.total_investment:,}\n"
            msg += f"   払戻: ¥{cumulative.total_payout:,}\n"
            msg += f"   損益: ¥{cumulative.profit:+,} (ROI: {cumulative.roi:+.1f}%)\n"
            if cumulative.current_streak < 0:
                msg += f"   連敗: {abs(cumulative.current_streak)}"
            else:
                msg += f"   連勝: {cumulative.current_streak}"

        return self._send_message(self.ops_channel, msg)

    def send_no_data(self, target_date: str, reason: str) -> bool:
        """データなし通知"""
        formatted_date = self._format_date(target_date)
        return self._send_message(
            self.ops_channel,
            f":information_source: {formatted_date} 評価データなし\n   {reason}"
        )


def get_target_date(event: Dict[str, Any]) -> str:
    """イベントからtarget_dateを取得"""
    target_date = event.get('target_date', 'auto')
    if target_date == 'auto':
        return datetime.now().strftime('%Y%m%d')
    return target_date


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Lambda handler"""
    logger.info(f"Evaluator Lambda started: {json.dumps(event)}")

    slack = EvaluatorSlackService()
    target_date = get_target_date(event)

    try:
        service = EvaluatorService()

        # 推論結果取得
        inference_df = service.get_inference_results(target_date)
        if inference_df is None or len(inference_df) == 0:
            slack.send_no_data(target_date, "推論結果なし（購入なし）")
            return {
                'statusCode': 200,
                'body': {
                    'status': 'no_inference',
                    'target_date': target_date,
                    'message': 'No inference results found'
                }
            }

        # レース結果取得
        results_df = service.get_race_results(target_date)
        if results_df is None or len(results_df) == 0:
            slack.send_no_data(target_date, "レース結果なし（HJCファイル未取得）")
            return {
                'statusCode': 200,
                'body': {
                    'status': 'no_results',
                    'target_date': target_date,
                    'message': 'No race results found'
                }
            }

        # 購入金額取得（環境変数 or デフォルト）
        bet_amount = int(os.environ.get('BET_AMOUNT', '5000'))

        # 評価実行
        details = service.evaluate_bets(inference_df, results_df, bet_amount)

        if not details:
            slack.send_no_data(target_date, "評価対象なし")
            return {
                'statusCode': 200,
                'body': {
                    'status': 'no_matches',
                    'target_date': target_date,
                    'message': 'No matching races found'
                }
            }

        # サマリー計算
        summary = service.calculate_daily_summary(details)
        cumulative = service.get_cumulative_summary(target_date)

        # 当日分を累計に加算
        cumulative.total_bets += summary.total_bets
        cumulative.hits += summary.hits
        cumulative.total_investment += summary.total_investment
        cumulative.total_payout += summary.total_payout
        if cumulative.total_bets > 0:
            cumulative.hit_rate = round(cumulative.hits / cumulative.total_bets, 4)
        if cumulative.total_investment > 0:
            cumulative.roi = round(
                (cumulative.total_payout - cumulative.total_investment) /
                cumulative.total_investment * 100, 2
            )
        cumulative.profit = cumulative.total_payout - cumulative.total_investment

        # 連勝/連敗更新
        if summary.hits == summary.total_bets:
            # 全的中
            if cumulative.current_streak > 0:
                cumulative.current_streak += summary.total_bets
            else:
                cumulative.current_streak = summary.hits
        elif summary.hits == 0:
            # 全不的中
            if cumulative.current_streak < 0:
                cumulative.current_streak -= summary.total_bets
            else:
                cumulative.current_streak = -summary.total_bets
        else:
            # 混合：最後の結果で更新
            last_hit = details[-1].is_hit
            cumulative.current_streak = 1 if last_hit else -1

        # S3に保存
        result_key = service.save_evaluation_result(target_date, summary, details, cumulative)

        # Slack通知
        slack.send_daily_evaluation(target_date, summary, cumulative)

        return {
            'statusCode': 200,
            'body': {
                'status': 'success',
                'target_date': target_date,
                'summary': asdict(summary),
                'cumulative': asdict(cumulative),
                'result_key': result_key
            }
        }

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Evaluator error: {e}\n{error_trace}")
        slack.send_error(target_date, str(e))
        return {
            'statusCode': 500,
            'body': {
                'status': 'error',
                'target_date': target_date,
                'error': str(e)
            }
        }


# ローカルテスト用
if __name__ == '__main__':
    test_event = {
        'target_date': '20251123'
    }
    result = handler(test_event, None)
    print(json.dumps(result, indent=2, ensure_ascii=False))
