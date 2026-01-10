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

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from slack_service import SlackService
from services.purchase_history import PurchaseHistoryService

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
    pred_prob: float = 0.0  # 予測確率
    standard_odds: float = 0.0  # 推論時点のオッズ


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


@dataclass
class YearlySummary:
    """年度別サマリー"""
    year: int
    total_bets: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    total_investment: int = 0
    total_payout: int = 0
    roi: float = 0.0
    profit: int = 0


@dataclass
class MonthlySummary:
    """月次サマリー"""
    year_month: str  # "202601"
    total_bets: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    total_investment: int = 0
    total_payout: int = 0
    roi: float = 0.0
    profit: int = 0


@dataclass
class OddsBandSummary:
    """オッズ帯別サマリー"""
    band_name: str  # "〜2倍", "2-5倍", "5-10倍", "10倍〜"
    total_bets: int = 0
    hits: int = 0
    hit_rate: float = 0.0
    total_investment: int = 0
    total_payout: int = 0
    roi: float = 0.0


@dataclass
class PredProbBandSummary:
    """予測確率帯別サマリー"""
    band_name: str  # "15-20%", "20-25%", "25-30%", "30%+"
    expected_rate: float  # 期待的中率（帯の中央値）
    total_bets: int = 0
    hits: int = 0
    actual_hit_rate: float = 0.0


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
                      target_date: str, bet_amount: int = 5000) -> List[BetDetail]:
        """購入馬券の評価（実際に購入済みのもののみ）"""
        evaluated = []

        # 購入履歴を取得
        purchase_history = PurchaseHistoryService()
        history = purchase_history.load_history(target_date)
        purchased_tickets = history.get('tickets', [])

        # 購入済みチケット（status=PURCHASED）のみをフィルタ
        purchased_set = set()
        for ticket in purchased_tickets:
            if ticket.get('status') == 'PURCHASED':
                key = (
                    ticket.get('race_course'),
                    ticket.get('race_number'),
                    ticket.get('horse_number'),
                    ticket.get('bet_type', '単勝')
                )
                purchased_set.add(key)

        if not purchased_set:
            logger.info(f"購入済みチケットなし（{target_date}）- 評価スキップ")
            return evaluated

        logger.info(f"購入済みチケット: {len(purchased_set)}件")

        for _, row in inference_df.iterrows():
            place_name = row['PlaceName']
            race_number = int(row['RaceNumber'])
            horse_number = int(row['HorseNumber'])
            horse_name = row.get('HorseName', '')

            # 購入履歴に存在するかチェック
            ticket_key = (place_name, race_number, horse_number, '単勝')
            if ticket_key not in purchased_set:
                logger.debug(f"未購入のためスキップ: {place_name} {race_number}R {horse_number}番")
                continue

            # 推論結果から予測確率とオッズを取得
            pred_prob = float(row.get('pred_prob', 0.0))
            standard_odds = float(row.get('StandardOdds', 0.0))

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
                finish_position=finish_position,
                pred_prob=pred_prob,
                standard_odds=standard_odds
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

    def get_all_evaluation_details(self, include_date: Optional[str] = None) -> List[dict]:
        """過去すべての評価結果からdetailsを取得

        Args:
            include_date: この日付も含める（当日データを含めたい場合）
        """
        all_details = []

        try:
            paginator = self.s3.get_paginator('list_objects_v2')
            prefix = "evaluation-results/"

            for page in paginator.paginate(Bucket=self.financial_bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if 'daily_' in key and key.endswith('.json'):
                        date_part = key.split('daily_')[1].split('.')[0]

                        response = self.s3.get_object(Bucket=self.financial_bucket, Key=key)
                        data = json.loads(response['Body'].read().decode('utf-8'))

                        if 'details' in data:
                            for detail in data['details']:
                                detail['date'] = date_part
                                all_details.append(detail)

        except Exception as e:
            logger.warning(f"評価履歴取得エラー: {e}")

        return all_details

    def calculate_yearly_summaries(self, all_details: List[dict],
                                   current_details: List[BetDetail] = None,
                                   current_date: str = None) -> List[YearlySummary]:
        """年度別サマリー計算"""
        yearly_data = {}

        # 過去データを年度別に集計
        for detail in all_details:
            date = detail.get('date', '')
            if len(date) >= 4:
                year = int(date[:4])
                if year not in yearly_data:
                    yearly_data[year] = {
                        'total_bets': 0, 'hits': 0,
                        'total_investment': 0, 'total_payout': 0
                    }
                yearly_data[year]['total_bets'] += 1
                if detail.get('is_hit'):
                    yearly_data[year]['hits'] += 1
                yearly_data[year]['total_investment'] += detail.get('amount', 0)
                yearly_data[year]['total_payout'] += detail.get('payout', 0)

        # 当日データを追加
        if current_details and current_date:
            year = int(current_date[:4])
            if year not in yearly_data:
                yearly_data[year] = {
                    'total_bets': 0, 'hits': 0,
                    'total_investment': 0, 'total_payout': 0
                }
            for d in current_details:
                yearly_data[year]['total_bets'] += 1
                if d.is_hit:
                    yearly_data[year]['hits'] += 1
                yearly_data[year]['total_investment'] += d.amount
                yearly_data[year]['total_payout'] += d.payout

        # YearlySummary生成
        summaries = []
        for year, data in sorted(yearly_data.items(), reverse=True):
            hit_rate = round(data['hits'] / data['total_bets'], 4) if data['total_bets'] > 0 else 0.0
            profit = data['total_payout'] - data['total_investment']
            roi = round(profit / data['total_investment'] * 100, 2) if data['total_investment'] > 0 else 0.0

            summaries.append(YearlySummary(
                year=year,
                total_bets=data['total_bets'],
                hits=data['hits'],
                hit_rate=hit_rate,
                total_investment=data['total_investment'],
                total_payout=data['total_payout'],
                roi=roi,
                profit=profit
            ))

        return summaries

    def calculate_monthly_summary(self, all_details: List[dict],
                                  current_details: List[BetDetail] = None,
                                  year_month: str = None) -> MonthlySummary:
        """月次サマリー計算"""
        if not year_month:
            year_month = datetime.now().strftime('%Y%m')

        data = {
            'total_bets': 0, 'hits': 0,
            'total_investment': 0, 'total_payout': 0
        }

        # 過去データから当月分を集計
        for detail in all_details:
            date = detail.get('date', '')
            if len(date) >= 6 and date[:6] == year_month:
                data['total_bets'] += 1
                if detail.get('is_hit'):
                    data['hits'] += 1
                data['total_investment'] += detail.get('amount', 0)
                data['total_payout'] += detail.get('payout', 0)

        # 当日データを追加（当月の場合）
        if current_details and year_month:
            for d in current_details:
                data['total_bets'] += 1
                if d.is_hit:
                    data['hits'] += 1
                data['total_investment'] += d.amount
                data['total_payout'] += d.payout

        hit_rate = round(data['hits'] / data['total_bets'], 4) if data['total_bets'] > 0 else 0.0
        profit = data['total_payout'] - data['total_investment']
        roi = round(profit / data['total_investment'] * 100, 2) if data['total_investment'] > 0 else 0.0

        return MonthlySummary(
            year_month=year_month,
            total_bets=data['total_bets'],
            hits=data['hits'],
            hit_rate=hit_rate,
            total_investment=data['total_investment'],
            total_payout=data['total_payout'],
            roi=roi,
            profit=profit
        )

    def calculate_odds_band_summaries(self, all_details: List[dict],
                                      current_details: List[BetDetail] = None) -> List[OddsBandSummary]:
        """オッズ帯別サマリー計算（累計）

        オッズ帯（4段階）:
        - 〜2倍
        - 2-5倍
        - 5-10倍
        - 10倍〜
        """
        bands = {
            '〜2倍': {'min': 0, 'max': 2.0, 'total_bets': 0, 'hits': 0, 'investment': 0, 'payout': 0},
            '2-5倍': {'min': 2.0, 'max': 5.0, 'total_bets': 0, 'hits': 0, 'investment': 0, 'payout': 0},
            '5-10倍': {'min': 5.0, 'max': 10.0, 'total_bets': 0, 'hits': 0, 'investment': 0, 'payout': 0},
            '10倍〜': {'min': 10.0, 'max': float('inf'), 'total_bets': 0, 'hits': 0, 'investment': 0, 'payout': 0},
        }

        def get_band(odds: float) -> str:
            for band_name, config in bands.items():
                if config['min'] <= odds < config['max']:
                    return band_name
            return '10倍〜'

        # 過去データ集計（standard_oddsを使用）
        for detail in all_details:
            odds = detail.get('standard_odds', 0.0) or detail.get('odds', 0.0)
            if odds <= 0:
                continue
            band = get_band(odds)
            bands[band]['total_bets'] += 1
            if detail.get('is_hit'):
                bands[band]['hits'] += 1
            bands[band]['investment'] += detail.get('amount', 0)
            bands[band]['payout'] += detail.get('payout', 0)

        # 当日データ追加
        if current_details:
            for d in current_details:
                odds = d.standard_odds if d.standard_odds > 0 else d.odds
                if odds <= 0:
                    continue
                band = get_band(odds)
                bands[band]['total_bets'] += 1
                if d.is_hit:
                    bands[band]['hits'] += 1
                bands[band]['investment'] += d.amount
                bands[band]['payout'] += d.payout

        summaries = []
        for band_name in ['〜2倍', '2-5倍', '5-10倍', '10倍〜']:
            data = bands[band_name]
            hit_rate = round(data['hits'] / data['total_bets'], 4) if data['total_bets'] > 0 else 0.0
            profit = data['payout'] - data['investment']
            roi = round(profit / data['investment'] * 100, 2) if data['investment'] > 0 else 0.0

            summaries.append(OddsBandSummary(
                band_name=band_name,
                total_bets=data['total_bets'],
                hits=data['hits'],
                hit_rate=hit_rate,
                total_investment=data['investment'],
                total_payout=data['payout'],
                roi=roi
            ))

        return summaries

    def calculate_pred_prob_band_summaries(self, all_details: List[dict],
                                           current_details: List[BetDetail] = None) -> List[PredProbBandSummary]:
        """予測確率帯別サマリー計算（累計）

        確率帯:
        - 15-20%
        - 20-25%
        - 25-30%
        - 30%+
        """
        bands = {
            '15-20%': {'min': 0.15, 'max': 0.20, 'expected': 0.175, 'total_bets': 0, 'hits': 0},
            '20-25%': {'min': 0.20, 'max': 0.25, 'expected': 0.225, 'total_bets': 0, 'hits': 0},
            '25-30%': {'min': 0.25, 'max': 0.30, 'expected': 0.275, 'total_bets': 0, 'hits': 0},
            '30%+': {'min': 0.30, 'max': 1.0, 'expected': 0.35, 'total_bets': 0, 'hits': 0},
        }

        def get_band(prob: float) -> Optional[str]:
            if prob < 0.15:
                return None  # 15%未満は対象外
            for band_name, config in bands.items():
                if config['min'] <= prob < config['max']:
                    return band_name
            return '30%+'

        # 過去データ集計
        for detail in all_details:
            prob = detail.get('pred_prob', 0.0)
            band = get_band(prob)
            if band is None:
                continue
            bands[band]['total_bets'] += 1
            if detail.get('is_hit'):
                bands[band]['hits'] += 1

        # 当日データ追加
        if current_details:
            for d in current_details:
                band = get_band(d.pred_prob)
                if band is None:
                    continue
                bands[band]['total_bets'] += 1
                if d.is_hit:
                    bands[band]['hits'] += 1

        summaries = []
        for band_name in ['15-20%', '20-25%', '25-30%', '30%+']:
            data = bands[band_name]
            actual_rate = round(data['hits'] / data['total_bets'], 4) if data['total_bets'] > 0 else 0.0

            summaries.append(PredProbBandSummary(
                band_name=band_name,
                expected_rate=data['expected'],
                total_bets=data['total_bets'],
                hits=data['hits'],
                actual_hit_rate=actual_rate
            ))

        return summaries

    def save_evaluation_result(self, target_date: str, summary: DailySummary,
                               details: List[BetDetail], cumulative: CumulativeSummary,
                               extended: Optional[dict] = None) -> str:
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

        # 拡張サマリーを追加
        if extended:
            result['extended'] = extended

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
                              cumulative: CumulativeSummary,
                              yearly: List[YearlySummary] = None,
                              monthly: MonthlySummary = None,
                              odds_bands: List[OddsBandSummary] = None,
                              pred_prob_bands: List[PredProbBandSummary] = None) -> bool:
        """日次評価通知（拡張版）"""
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
                msg += f"   連敗: {abs(cumulative.current_streak)}\n"
            else:
                msg += f"   連勝: {cumulative.current_streak}\n"

        # 年度別（当年のみ表示）
        if yearly:
            current_year = int(target_date[:4])
            for ys in yearly:
                if ys.year == current_year and ys.total_bets > 0:
                    msg += f"\n*【{ys.year}年】*\n"
                    msg += f"   的中: {ys.hits}/{ys.total_bets} ({ys.hit_rate*100:.1f}%)\n"
                    msg += f"   損益: ¥{ys.profit:+,} (ROI: {ys.roi:+.1f}%)\n"
                    break

        # 月次
        if monthly and monthly.total_bets > 0:
            year_month = monthly.year_month
            month_num = int(year_month[4:6])
            msg += f"\n*【{month_num}月】*\n"
            msg += f"   的中: {monthly.hits}/{monthly.total_bets} ({monthly.hit_rate*100:.1f}%)\n"
            msg += f"   損益: ¥{monthly.profit:+,} (ROI: {monthly.roi:+.1f}%)\n"

        # オッズ帯別（データがある帯のみ）
        if odds_bands:
            has_data = any(ob.total_bets > 0 for ob in odds_bands)
            if has_data:
                msg += f"\n*【オッズ帯別】* 累計\n"
                for ob in odds_bands:
                    if ob.total_bets > 0:
                        msg += f"   {ob.band_name}: {ob.hits}/{ob.total_bets} ({ob.hit_rate*100:.1f}%) ROI: {ob.roi:+.1f}%\n"

        # 予測確率帯別（データがある帯のみ）
        if pred_prob_bands:
            has_data = any(pb.total_bets > 0 for pb in pred_prob_bands)
            if has_data:
                msg += f"\n*【確率帯別】* 累計\n"
                for pb in pred_prob_bands:
                    if pb.total_bets > 0:
                        msg += f"   {pb.band_name}: {pb.hits}/{pb.total_bets} ({pb.actual_hit_rate*100:.1f}%) 期待:{pb.expected_rate*100:.1f}%\n"

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
            # 推論結果なし = 開催なし（平日など）
            # → 通知せずに正常終了（ログのみ）
            logger.info(f"No inference results for {target_date} (non-race day, skipping notification)")
            return {
                'statusCode': 200,
                'body': {
                    'status': 'no_inference',
                    'target_date': target_date,
                    'message': 'No inference results found (non-race day, no notification)'
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

        # 評価実行（購入履歴に基づいて実際に購入したもののみ評価）
        details = service.evaluate_bets(inference_df, results_df, target_date, bet_amount)

        if not details:
            # 購入済みチケットなし = 評価対象なし（通知せずに正常終了）
            logger.info(f"No purchased tickets for {target_date} - skipping notification")
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

        # 拡張サマリー計算
        all_details = service.get_all_evaluation_details()
        yearly_summaries = service.calculate_yearly_summaries(
            all_details, current_details=details, current_date=target_date
        )
        year_month = target_date[:6]
        monthly_summary = service.calculate_monthly_summary(
            all_details, current_details=details, year_month=year_month
        )
        odds_band_summaries = service.calculate_odds_band_summaries(
            all_details, current_details=details
        )
        pred_prob_band_summaries = service.calculate_pred_prob_band_summaries(
            all_details, current_details=details
        )

        # 拡張データを辞書形式で作成
        extended = {
            'yearly': [asdict(ys) for ys in yearly_summaries],
            'monthly': asdict(monthly_summary),
            'odds_bands': [asdict(ob) for ob in odds_band_summaries],
            'pred_prob_bands': [asdict(pb) for pb in pred_prob_band_summaries]
        }

        # S3に保存
        result_key = service.save_evaluation_result(
            target_date, summary, details, cumulative, extended=extended
        )

        # Slack通知
        slack.send_daily_evaluation(
            target_date, summary, cumulative,
            yearly=yearly_summaries,
            monthly=monthly_summary,
            odds_bands=odds_band_summaries,
            pred_prob_bands=pred_prob_band_summaries
        )

        return {
            'statusCode': 200,
            'body': {
                'status': 'success',
                'target_date': target_date,
                'summary': asdict(summary),
                'cumulative': asdict(cumulative),
                'extended': extended,
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
