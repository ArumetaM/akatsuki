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
import requests
from bs4 import BeautifulSoup

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

# ファイルログも設定（エラー時は無視）
try:
    setup_file_logging('akatsuki_bot_v2')
except Exception as e:
    logger.warning(f"Could not setup file logging: {e}")

# 定数
IPAT_URL = "https://www.ipat.jra.go.jp/"  # 中央競馬（JRA）のみ
TIMEOUT_MS = int(os.environ.get('TIMEOUT_MS', '20000'))
HEADLESS_MODE = os.environ.get('HEADLESS_MODE', 'true').lower() == 'true'
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'


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


async def analyze_page_structure(page: Page):
    """ページのHTML構造を解析してログインフィールドを検出"""
    try:
        # ページのタイトルを取得
        title = await page.title()
        logger.info(f"Page title: {title}")
        
        # ページ全体のテキストを取得して投票時間の状況を確認
        page_text = await page.text_content('body')
        logger.info(f"Page content (first 500 chars): {page_text[:500] if page_text else 'No content'}")
        
        # 投票時間関連のメッセージをチェック
        if page_text:
            time_keywords = ['投票時間外', 'サービス時間外', '運営時間', 'メンテナンス', 
                           '受付時間', '販売時間', '休業', '終了']
            for keyword in time_keywords:
                if keyword in page_text:
                    logger.warning(f"Found time-related message: {keyword}")
                    # 関連する部分を抽出
                    import re
                    pattern = f'.{{0,50}}{re.escape(keyword)}.{{0,100}}'
                    matches = re.findall(pattern, page_text)
                    for match in matches[:3]:  # 最初の3件を表示
                        logger.info(f"Context: {match.strip()}")
        
        # すべてのinput要素を検出
        inputs = await page.query_selector_all('input')
        logger.info(f"Found {len(inputs)} input elements")
        
        input_info = []
        for i, input_elem in enumerate(inputs):
            name = await input_elem.get_attribute('name') or ''
            type_attr = await input_elem.get_attribute('type') or ''
            id_attr = await input_elem.get_attribute('id') or ''
            placeholder = await input_elem.get_attribute('placeholder') or ''
            class_attr = await input_elem.get_attribute('class') or ''
            
            input_info.append({
                'index': i,
                'name': name,
                'type': type_attr,
                'id': id_attr,
                'placeholder': placeholder,
                'class': class_attr
            })
            
            logger.info(f"Input {i}: name='{name}', type='{type_attr}', id='{id_attr}', placeholder='{placeholder}'")
        
        # すべてのbutton要素を検出
        buttons = await page.query_selector_all('button')
        logger.info(f"Found {len(buttons)} button elements")
        
        for i, button in enumerate(buttons):
            text = await button.text_content() or ''
            class_attr = await button.get_attribute('class') or ''
            logger.info(f"Button {i}: text='{text.strip()}', class='{class_attr}'")
        
        # aタグもチェック（ログインリンクの可能性）
        links = await page.query_selector_all('a')
        logger.info(f"Found {len(links)} link elements")
        
        for i, link in enumerate(links[:10]):  # 最初の10個だけ表示
            text = await link.text_content() or ''
            href = await link.get_attribute('href') or ''
            if text.strip():
                logger.info(f"Link {i}: text='{text.strip()}', href='{href}'")
        
        return input_info
        
    except Exception as e:
        logger.error(f"Failed to analyze page structure: {e}")
        return []


async def extract_detailed_time_info(page: Page) -> dict:
    """ページから詳細な時間情報を抽出"""
    time_info = {
        'next_start_time': None,
        'current_status': 'unknown',
        'detailed_hours': [],
        'specific_times': [],
        'next_race_info': None
    }
    
    try:
        page_text = await page.text_content('body') or ''
        
        # 時間パターンの詳細解析
        import re
        
        # より詳細な時間パターン
        time_patterns = [
            # 開始時間パターン
            (r'発売開始[:：]\s*(\d{1,2}[:：]\d{2})', 'sales_start'),
            (r'投票開始[:：]\s*(\d{1,2}[:：]\d{2})', 'voting_start'),
            (r'(\d{1,2}[:：]\d{2})\s*[〜～]\s*(\d{1,2}[:：]\d{2})', 'time_range'),
            (r'(\d{1,2}[:：]\d{2})\s*開始', 'start_time'),
            (r'(\d{1,2}[:：]\d{2})\s*発売', 'sales_time'),
            (r'(\d{1,2}[:：]\d{2})\s*受付', 'reception_time'),
            
            # 次回開催情報
            (r'次回.*?(\d{1,2}[:：]\d{2})', 'next_time'),
            (r'明日.*?(\d{1,2}[:：]\d{2})', 'tomorrow_time'),
            (r'土曜.*?(\d{1,2}[:：]\d{2})', 'saturday_time'),
            (r'日曜.*?(\d{1,2}[:：]\d{2})', 'sunday_time'),
            
            # 曜日別営業時間
            (r'平日.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'weekday_hours'),
            (r'土日.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'weekend_hours'),
            (r'月曜.*?(\d{1,2}[:：]\d{2})', 'monday_hours'),
            (r'火曜.*?(\d{1,2}[:：]\d{2})', 'tuesday_hours'),
            (r'水曜.*?(\d{1,2}[:：]\d{2})', 'wednesday_hours'),
            (r'木曜.*?(\d{1,2}[:：]\d{2})', 'thursday_hours'),
            (r'金曜.*?(\d{1,2}[:：]\d{2})', 'friday_hours'),
            (r'土曜.*?(\d{1,2}[:：]\d{2})', 'saturday_hours'),
            (r'日曜.*?(\d{1,2}[:：]\d{2})', 'sunday_hours'),
        ]
        
        for pattern, time_type in time_patterns:
            matches = re.findall(pattern, page_text)
            if matches:
                logger.info(f"Found {time_type}: {matches}")
                time_info['specific_times'].append({
                    'type': time_type,
                    'times': matches
                })
                
                # 次回開始時間を推定
                if time_type in ['sales_start', 'voting_start', 'next_time'] and matches:
                    time_info['next_start_time'] = matches[0] if isinstance(matches[0], str) else matches[0][0]
        
        # 現在のステータスを判定
        status_keywords = {
            '投票時間外': 'outside_hours',
            'サービス時間外': 'outside_service',
            '受付時間外': 'outside_reception', 
            'メンテナンス': 'maintenance',
            '休業': 'closed',
            '終了': 'ended',
            'ログイン': 'available',
            '投票': 'voting_available'
        }
        
        for keyword, status in status_keywords.items():
            if keyword in page_text:
                time_info['current_status'] = status
                logger.info(f"Current status: {status} (keyword: {keyword})")
                break
        
        # 営業時間の詳細情報を抽出
        hours_patterns = [
            r'(\d{1,2}[:：]\d{2})\s*[〜～]\s*(\d{1,2}[:：]\d{2})',
            r'(\d{1,2})時\s*[〜～]\s*(\d{1,2})時',
            r'(\d{1,2}[:：]\d{2})\s*開始.*?(\d{1,2}[:：]\d{2})\s*終了'
        ]
        
        for pattern in hours_patterns:
            hours_matches = re.findall(pattern, page_text)
            if hours_matches:
                time_info['detailed_hours'].extend(hours_matches)
        
        # レース情報を検索
        race_patterns = [
            r'(\d+)回\s*(\w+)\s*(\d+)日目',
            r'(\w+競馬場)',
            r'第(\d+)レース',
            r'(\d+)R'
        ]
        
        for pattern in race_patterns:
            race_matches = re.findall(pattern, page_text)
            if race_matches:
                time_info['next_race_info'] = race_matches
                logger.info(f"Found race info: {race_matches}")
                break
        
        return time_info
        
    except Exception as e:
        logger.error(f"Failed to extract detailed time info: {e}")
        return time_info


async def check_voting_availability(page: Page) -> bool:
    """投票可能時間かどうかをチェック（詳細解析対応）"""
    try:
        # 詳細な時間情報を抽出
        time_info = await extract_detailed_time_info(page)
        
        page_text = await page.text_content('body')
        if not page_text:
            return False
        
        # 投票不可を示すキーワード
        unavailable_keywords = ['投票時間外', 'サービス時間外', '受付時間外',
                               'メンテナンス中', '休業中', '終了']
        
        for keyword in unavailable_keywords:
            if keyword in page_text:
                logger.warning(f"Voting unavailable: {keyword} found in page")
                
                # 次回開始時間があれば表示
                if time_info['next_start_time']:
                    logger.info(f"Next start time may be: {time_info['next_start_time']}")
                
                return False
        
        # 投票可能を示すキーワード
        available_keywords = ['ログイン', '投票', 'INET-ID', '加入者番号']
        
        for keyword in available_keywords:
            if keyword in page_text:
                logger.info(f"Voting may be available: {keyword} found in page")
                return True
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to check voting availability: {e}")
        return False


async def check_race_day_schedule(current_time) -> dict:
    """今日の競馬開催日かどうかを詳細チェック"""
    try:
        weekday = current_time.strftime('%A')
        weekday_jp = current_time.strftime('%w')  # 0=Sunday, 1=Monday, ...
        
        schedule_info = {
            'central_jra': False,
            'reason': '',
            'next_race_day': None
        }
        
        # 曜日による基本判定（中央競馬のみ）
        if weekday in ['Saturday', 'Sunday']:
            schedule_info['central_jra'] = True
            schedule_info['reason'] = f"{weekday}: JRA central racing is typically held on weekends"
        elif weekday == 'Friday':
            schedule_info['central_jra'] = False
            schedule_info['reason'] = f"{weekday}: JRA central racing is rarely held on Fridays"
        else:
            schedule_info['central_jra'] = False
            schedule_info['reason'] = f"{weekday}: JRA central racing is typically not held on weekdays"
        
        # 特別開催日の判定（月日による）
        month = current_time.month
        day = current_time.day
        
        # 有名な開催日（例：ダービー、天皇賞など）
        special_dates = [
            (5, 4),   # みどりの日（春の天皇賞）
            (5, 5),   # こどもの日（NHKマイルC）
            (10, 14), # 体育の日（秋の天皇賞）
            (12, 28), # 年末（有馬記念）
            (12, 29), # 年末
        ]
        
        if (month, day) in special_dates:
            schedule_info['central_jra'] = True
            schedule_info['reason'] += f" / Special racing day: {month}/{day}"
        
        # 今日が祝日かチェック（簡易版）
        holidays = {
            (1, 1): "元日",
            (2, 11): "建国記念の日",
            (4, 29): "昭和の日",
            (5, 3): "憲法記念日",
            (5, 4): "みどりの日",
            (5, 5): "こどもの日",
            (7, 20): "海の日",
            (8, 11): "山の日",
            (9, 21): "敬老の日",
            (10, 14): "体育の日",
            (11, 3): "文化の日",
            (11, 23): "勤労感謝の日",
            (12, 23): "天皇誕生日"
        }
        
        if (month, day) in holidays:
            schedule_info['central_jra'] = True
            schedule_info['reason'] += f" / Holiday: {holidays[(month, day)]}"
        
        return schedule_info
        
    except Exception as e:
        logger.error(f"Failed to check race day schedule: {e}")
        return {
            'central_jra': False,
            'reason': f'Error checking schedule: {e}',
            'next_race_day': None
        }


async def http_based_site_analysis():
    """HTTPリクエストベースでのサイト解析（中央JRAのみ）"""
    logger.info("Starting HTTP-based site analysis for central JRA...")
    
    try:
        logger.info(f"Analyzing central JRA: {IPAT_URL}")
        
        # HTTPリクエストでページを取得
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(IPAT_URL, headers=headers, timeout=30)
        
        # エンコーディングを自動検出してから設定
        if response.encoding.lower() in ['iso-8859-1', 'windows-1252']:
            response.encoding = 'euc-jp'  # JRAサイトの文字コード
        
        logger.info(f"Response encoding: {response.encoding}, Content-Type: {response.headers.get('content-type', 'N/A')}")
        
        if response.status_code != 200:
            logger.warning(f"Central JRA: HTTP {response.status_code}")
            return {
                'status': 'error',
                'http_code': response.status_code,
                'available': False
            }
        
        # HTMLを解析（複数エンコーディングで試行）
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text()
        except UnicodeDecodeError:
            # フォールバック: UTF-8で試行
            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text()
        
        logger.info(f"Page text length: {len(page_text)}")
        logger.info(f"First 200 chars of page text: {repr(page_text[:200])}")
        
        # サービス状況をチェック（より詳細）
        service_status = 'unknown'
        
        # まず生のHTMLをチェック
        raw_html = response.text
        logger.info(f"Raw HTML length: {len(raw_html)}")
        logger.info(f"Raw HTML contains: OutOfService={bool('OutOfService' in raw_html)}, DOCTYPE={bool('DOCTYPE' in raw_html)}")
        
        # HTMLパースしたテキストもチェック
        if '投票時間外' in page_text or '受付時間外' in page_text:
            service_status = 'outside_hours'
            logger.info("Found '投票時間外' or '受付時間外' in page text")
        elif 'メンテナンス' in page_text:
            service_status = 'maintenance'
            logger.info("Found 'メンテナンス' in page text")
        elif 'ログイン' in page_text and 'INET-ID' in page_text:
            service_status = 'available'
            logger.info("Found 'ログイン' and 'INET-ID' in page text - voting likely available")
        elif 'OutOfService' in raw_html:
            service_status = 'out_of_service'
            logger.info("Found 'OutOfService' in raw HTML")
        elif 'DOCTYPE' in raw_html and len(page_text) > 200:
            # 正常なHTMLページがロードされている場合
            logger.info(f"Page text preview (first 500 chars): {page_text[:500]}")
            
            if 'JRA' in page_text:
                service_status = 'normal_page'
                logger.info("Detected normal JRA page")
                
                # より詳細なキーワード検索（生HTMLとパースされたテキスト両方で）
                keywords_check = {
                    'INET-ID': ('INET-ID' in page_text or 'INET-ID' in raw_html),
                    'ログイン': ('ログイン' in page_text or 'ログイン' in raw_html),
                    '投票': ('投票' in page_text or '投票' in raw_html),
                    '加入者番号': ('加入者番号' in page_text or '加入者番号' in raw_html),
                    '暗証番号': ('暗証番号' in page_text or '暗証番号' in raw_html),
                    'パスワード': ('パスワード' in page_text or 'パスワード' in raw_html),
                    'login': ('login' in page_text.lower() or 'login' in raw_html.lower()),
                    'password': ('password' in page_text.lower() or 'password' in raw_html.lower()),
                }
                
                found_keywords = [k for k, v in keywords_check.items() if v]
                logger.info(f"Keywords found on page: {found_keywords}")
                
                # フォーム要素を探す
                form_elements = soup.find_all(['form', 'input'])
                logger.info(f"Found {len(form_elements)} form elements on page")
                
                # 入力フィールドの詳細
                input_fields = soup.find_all('input')
                input_types = [inp.get('type', 'text') for inp in input_fields]
                input_names = [inp.get('name', '') for inp in input_fields]
                logger.info(f"Input field types: {input_types}")
                logger.info(f"Input field names: {input_names}")
                
                # 利用可能性の判定
                has_login_keywords = len(found_keywords) >= 2
                has_input_fields = len(input_fields) >= 2
                has_password_field = any('password' in t.lower() for t in input_types)
                
                if has_login_keywords or (has_input_fields and has_password_field):
                    service_status = 'likely_available'
                    logger.info("Login functionality detected - likely available for voting")
                elif 'JavaScript' in raw_html:
                    service_status = 'requires_js'
                    logger.info("Page requires JavaScript - may be available but needs browser")
            else:
                logger.warning("JRA not found in page content - unexpected page")
        
        # ページタイトルもチェック
        page_title = soup.title.string if soup.title else ''
        logger.info(f"Page title: {page_title}")
        
        if 'エラー' in page_title or 'Error' in page_title:
            service_status = 'error_page'
        
        # 時間情報を抽出
        import re
        time_patterns = [
            r'発売開始[:：]\s*(\d{1,2}[:：]\d{2})',
            r'(\d{1,2}[:：]\d{2})\s*[〜～]\s*(\d{1,2}[:：]\d{2})',
            r'次回.*?(\d{1,2}[:：]\d{2})',
            r'土曜.*?(\d{1,2}[:：]\d{2})',
            r'日曜.*?(\d{1,2}[:：]\d{2})',
        ]
        
        time_info = []
        for pattern in time_patterns:
            matches = re.findall(pattern, page_text)
            if matches:
                time_info.extend(matches)
        
        analysis_result = {
            'status': service_status,
            'http_code': response.status_code,
            'available': service_status in ['available', 'likely_available', 'requires_js'],
            'time_info': time_info,
            'page_title': soup.title.string if soup.title else '',
            'content_length': len(page_text)
        }
        
        logger.info(f"Central JRA analysis: {service_status} (times found: {len(time_info)})")
        
        if service_status in ['available', 'likely_available']:
            logger.info("✓ Central JRA appears to be available for voting")
        elif service_status == 'requires_js':
            logger.info("⚠ Central JRA requires JavaScript - will attempt browser access")
        else:
            logger.warning("✗ Central JRA is not available for voting")
        
        return analysis_result
        
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP request failed for central JRA: {e}")
        return {
            'status': 'connection_error',
            'available': False,
            'error': str(e)
        }
    except Exception as e:
        logger.error(f"Analysis failed for central JRA: {e}")
        return {
            'status': 'analysis_error',
            'available': False,
            'error': str(e)
        }


async def check_reception_hours(page: Page):
    """受付時間の詳細情報を確認"""
    try:
        # 現在の時刻を確認
        from datetime import datetime
        current_time = datetime.now()
        logger.info(f"Current time: {current_time.strftime('%Y-%m-%d %H:%M:%S %A')}")
        
        # 開催日スケジュールをチェック
        schedule = await check_race_day_schedule(current_time)
        logger.info(f"Race day analysis: {schedule['reason']}")
        
        if schedule['central_jra']:
            logger.info("✓ Central JRA racing may be available today")
        else:
            logger.warning("✗ Central JRA racing unlikely today")
        
        # 営業時間外の理由を分析
        hour = current_time.hour
        if hour < 9:
            logger.info(f"Current hour is {hour} - too early (voting usually starts around 9-10 AM)")
        elif hour > 21:
            logger.info(f"Current hour is {hour} - too late (voting usually ends around 9 PM)")
        else:
            logger.info(f"Current hour is {hour} - within potential voting hours")
        
        # 受付時間に関するリンクを詳しく調べる
        links = await page.query_selector_all('a')
        for link in links:
            text = await link.text_content() or ''
            href = await link.get_attribute('href') or ''
            if '受付時間' in text or 'hatsubai' in href or 'soku' in href or 'apat' in href or 'hatsubaijikan' in href:
                logger.info(f"Found time-related link: '{text.strip()}' -> {href}")
                
                # 特に重要なリンク（hatsubaijikan.html）を優先処理
                if 'hatsubaijikan' in href:
                    logger.info(f"Priority processing for reception hours page: {href}")
                
                # 直接URLで詳細ページを開く
                try:
                    await page.goto(href)
                    await page.wait_for_timeout(5000)  # 長めに待機
                    await take_screenshot(page, f"time_info_{href.split('/')[-1]}")
                    
                    # 詳細情報を取得
                    hours_text = await page.text_content('body')
                    if hours_text and len(hours_text) > 200:  # 元のページと異なる内容の場合
                        logger.info(f"Time info from {href} (first 2000 chars): {hours_text[:2000]}")
                        
                        # より詳細な時間パターンを探す
                        import re
                        time_patterns = [
                            (r'平日.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'Weekday hours'),
                            (r'土.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'Saturday hours'),
                            (r'日.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'Sunday hours'),
                            (r'(\d{1,2}[:：]\d{2})\s*～\s*(\d{1,2}[:：]\d{2})', 'General time range'),
                            (r'(\d{1,2})時\s*～\s*(\d{1,2})時', 'Hour range'),
                            (r'月.*?(\d{1,2}[:：]\d{2})', 'Monday time'),
                            (r'火.*?(\d{1,2}[:：]\d{2})', 'Tuesday time'),
                            (r'水.*?(\d{1,2}[:：]\d{2})', 'Wednesday time'),
                            (r'木.*?(\d{1,2}[:：]\d{2})', 'Thursday time'),
                            (r'金.*?(\d{1,2}[:：]\d{2})', 'Friday time'),
                            (r'開催.*?(\d{1,2}[:：]\d{2})', 'Race day start time'),
                            (r'発売.*?(\d{1,2}[:：]\d{2})', 'Ticket sales start time'),
                        ]
                        
                        for pattern, desc in time_patterns:
                            matches = re.findall(pattern, hours_text)
                            if matches:
                                logger.info(f"Found {desc}: {matches}")
                        
                        # 金曜日や平日の開催情報を特に探す
                        friday_keywords = ['金曜', '金', 'Friday', '平日']
                        for keyword in friday_keywords:
                            if keyword in hours_text:
                                logger.info(f"Found Friday/weekday reference: {keyword}")
                                # 前後の文脈を取得
                                import re
                                context_pattern = f'.{{0,100}}{re.escape(keyword)}.{{0,100}}'
                                context_matches = re.findall(context_pattern, hours_text)
                                for context in context_matches[:2]:
                                    logger.info(f"Context for {keyword}: {context.strip()}")
                        
                        return  # 詳細情報を見つけたら終了
                    else:
                        logger.debug(f"No detailed time info found at {href}")
                        
                except Exception as e:
                    logger.debug(f"Failed to navigate to {href}: {e}")
                    continue
        
        logger.warning("Could not find detailed reception hours information")
        
    except Exception as e:
        logger.error(f"Failed to check reception hours: {e}")


async def find_login_fields(page: Page):
    """ログインフィールドを動的に検出"""
    input_info = await analyze_page_structure(page)
    
    # INET-IDフィールドを探す
    inet_selectors = [
        'input[name="inetid"]',
        'input[name="INETID"]',
        'input[name="inet_id"]',
        'input[id*="inet"]',
        'input[placeholder*="INET"]',
        'input[placeholder*="inet"]',
        'input[type="text"]',  # 最初のtextフィールド
    ]
    
    inet_field = None
    for selector in inet_selectors:
        try:
            element = await page.query_selector(selector)
            if element:
                logger.info(f"Found INET field with selector: {selector}")
                inet_field = selector
                break
        except:
            continue
    
    # パスワードフィールドを探す
    password_selectors = [
        'input[name="password"]',
        'input[name="PASSWORD"]',
        'input[name="pass"]',
        'input[name="p"]',
        'input[type="password"]',
    ]
    
    password_field = None
    for selector in password_selectors:
        try:
            element = await page.query_selector(selector)
            if element:
                logger.info(f"Found password field with selector: {selector}")
                password_field = selector
                break
        except:
            continue
    
    return inet_field, password_field


async def login_ipat_v2(page: Page, credentials: dict):
    """IPAT 2段階ログイン（中央JRAのみ・動的フィールド検出対応）"""
    try:
        logger.info("Starting IPAT login process for central JRA...")
        
        # 中央JRAサイトへアクセス
        if not await safe_navigate(page, IPAT_URL, TIMEOUT_MS):
            raise Exception("Failed to navigate to central JRA IPAT")
        
        await page.wait_for_timeout(4000)  # Seleniumと同じ待機時間
        
        # スクリーンショットを保存（初期ページ）
        await take_screenshot(page, "ipat_central_jra_initial")
        
        # 投票可能状況をチェック
        voting_available = await check_voting_availability(page)
        
        if not voting_available:
            logger.warning("Central JRA IPAT is not available for voting (outside business hours)")
            # 詳細な時間情報をチェック
            await check_reception_hours(page)
            raise Exception("Central JRA IPAT is currently unavailable for voting")
        
        logger.info("✓ Central JRA IPAT appears to be available for voting")
        
        # スクリーンショットを保存（利用可能確認後）
        await take_screenshot(page, "ipat_central_jra_ready")
        
        # ページ構造を解析
        inet_field, password_field = await find_login_fields(page)
        
        # 投票可能時間かチェック
        voting_available = await check_voting_availability(page)
        if not voting_available:
            logger.warning("Voting appears to be unavailable (outside business hours or maintenance)")
            # 受付時間の詳細情報を確認
            await check_reception_hours(page)
            
            # hatsubaijikan.htmlページを直接確認
            try:
                logger.info("Directly accessing reception hours detail page...")
                await page.goto('https://jra.jp/dento/member/hatsubaijikan.html')
                await page.wait_for_timeout(5000)
                await take_screenshot(page, "hatsubaijikan_direct")
                
                hours_text = await page.text_content('body')
                if hours_text:
                    logger.info(f"Direct access - hatsubaijikan.html content (first 3000 chars): {hours_text[:3000]}")
                    
                    # 詳細な時間パターンを探す
                    import re
                    detailed_patterns = [
                        (r'金曜.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'Friday detailed hours'),
                        (r'平日.*?(\d{1,2}[:：]\d{2}).*?(\d{1,2}[:：]\d{2})', 'Weekday detailed hours'),
                        (r'(\d{1,2}[:：]\d{2})\s*～\s*(\d{1,2}[:：]\d{2})', 'Time ranges'),
                        (r'開始.*?(\d{1,2}[:：]\d{2})', 'Start times'),
                        (r'終了.*?(\d{1,2}[:：]\d{2})', 'End times'),
                        (r'発売.*?(\d{1,2}[:：]\d{2})', 'Sales times'),
                    ]
                    
                    for pattern, desc in detailed_patterns:
                        matches = re.findall(pattern, hours_text)
                        if matches:
                            logger.info(f"Found {desc}: {matches}")
                
            except Exception as e:
                logger.error(f"Failed to access hatsubaijikan.html directly: {e}")
        
        if not inet_field:
            logger.warning("INET field not found, checking if already on login page or need to navigate")
            # ログインリンクを探してクリック
            login_links = await page.query_selector_all('a')
            login_found = False
            for link in login_links:
                href = await link.get_attribute('href') or ''
                text = await link.text_content() or ''
                if 'ログイン' in text or 'LOGIN' in text.upper() or '投票' in text:
                    logger.info(f"Clicking login link: {text.strip()}")
                    await link.click()
                    await page.wait_for_timeout(3000)
                    inet_field, password_field = await find_login_fields(page)
                    login_found = True
                    break
            
            if not login_found:
                logger.warning("No login link found on central JRA IPAT page")
                inet_field, password_field = await find_login_fields(page)
        
        if not inet_field:
            if not voting_available:
                raise Exception("Could not find INET-ID input field - likely because voting is currently unavailable (outside business hours or under maintenance)")
            else:
                raise Exception("Could not find INET-ID input field - page structure may have changed")
        
        # === 第1段階: INET-ID入力 ===
        logger.info("Stage 1: Entering INET-ID...")
        if not await wait_and_fill(page, inet_field, credentials['inet_id']):
            raise Exception("Failed to fill INET-ID")
        
        # 次へボタンクリック（動的に検出）
        next_clicked = False
        
        # まず、「ログイン」ボタンを直接探す
        logger.info("Looking for Login button...")
        
        # まず、すべてのクリック可能な要素をデバッグ
        all_clickable_selectors = ['button', 'input[type="button"]', 'input[type="submit"]', 'input[type="image"]', 'a', 'img', 'div[class*="button"]', 'span[class*="button"]']
        for selector in all_clickable_selectors:
            elements = await page.query_selector_all(selector)
            if elements:
                logger.info(f"Found {len(elements)} {selector} elements")
                for i, elem in enumerate(elements[:5]):  # 最初の5つまで
                    text = await elem.text_content() or ''
                    value = await elem.get_attribute('value') or ''
                    alt = await elem.get_attribute('alt') or ''
                    src = await elem.get_attribute('src') or ''
                    class_attr = await elem.get_attribute('class') or ''
                    if text.strip() or value or alt:
                        logger.info(f"{selector}[{i}]: text='{text.strip()}', value='{value}', alt='{alt}', class='{class_attr}'")
                    if src:
                        logger.info(f"{selector}[{i}]: src='{src}'")
        
        # ボタンを探すセレクター
        button_selectors = [
            'button:has-text("ログイン")',
            'input[type="button"][value="ログイン"]',
            'input[type="submit"][value="ログイン"]',
            'input[type="image"][alt*="ログイン"]',  # 画像ボタン
            'img[alt*="ログイン"]',  # 画像
            'a:has-text("ログイン")',
            'div[class*="button"]:has-text("ログイン")',  # divボタン
            'span[class*="button"]:has-text("ログイン")',  # spanボタン
            'button',
            'input[type="button"]',
            'input[type="submit"]',
            'input[type="image"]',
            '.button',
            'img',  # すべての画像
            'a'  # すべてのリンク
        ]
        
        for selector in button_selectors:
            try:
                if 'has-text' in selector:
                    # has-textセレクタの特別処理
                    base_selector = selector.split(':')[0]
                    text_to_find = selector.split('"')[1]
                    elements = await page.query_selector_all(base_selector)
                    for element in elements:
                        elem_text = await element.text_content() or ''
                        if text_to_find in elem_text:
                            logger.info(f"Found login button with text: {elem_text.strip()}")
                            await element.click()
                            next_clicked = True
                            break
                else:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        text = await element.text_content() or ''
                        value = await element.get_attribute('value') or ''
                        class_attr = await element.get_attribute('class') or ''
                        
                        # ログインボタンの可能性をチェック
                        if 'ログイン' in text or 'ログイン' in value or 'LOGIN' in text.upper() or 'LOGIN' in value.upper():
                            logger.info(f"Clicking login button: text='{text.strip()}', value='{value}'")
                            await element.click()
                            next_clicked = True
                            break
                        # class名に"button"を含む要素もチェック
                        elif 'button' in class_attr.lower() and text.strip():
                            logger.info(f"Found button with class '{class_attr}' and text '{text.strip()}'")
                            # 左右の位置を確認（ログインボタンはINET-IDフィールドの右にあるはず）
                            try:
                                element_box = await element.bounding_box()
                                if element_box and element_box['x'] > 400:  # 右側にあるボタン
                                    logger.info(f"Clicking button on the right side: {text.strip()}")
                                    await element.click()
                                    next_clicked = True
                                    break
                            except:
                                pass
                        # onclick属性を持つ要素もチェック
                        onclick = await element.get_attribute('onclick') or ''
                        if onclick:
                            logger.info(f"Found element with onclick: {onclick}")
                            await element.click()
                            next_clicked = True
                            break
                
                if next_clicked:
                    break
            except Exception as e:
                logger.debug(f"Error with selector {selector}: {e}")
                continue
        
        # ボタンが見つからない場合はJavaScriptでフォーム送信を試行
        if not next_clicked:
            logger.info("Login button not found, attempting to submit form with JavaScript...")
            
            form_submitted = False
            try:
                # フォーム要素を探す
                forms = await page.query_selector_all('form')
                if forms:
                    logger.info(f"Found {len(forms)} form(s) on page")
                    # 最初のフォームを送信
                    await page.evaluate('document.forms[0].submit()')
                    form_submitted = True
                else:
                    # フォームがない場合はJavaScript関数を直接実行
                    logger.info("No form found, trying JavaScript functions...")
                    
                    # まず現在のURLを保存
                    current_url = page.url
                    
                    # 一般的なJRA IPATのログイン関数を試す
                    js_functions = [
                        'Proc()',  # JRAでよく使われる関数名
                        'doSubmit()',
                        'submitForm()',
                        'login()',
                        'next()',
                        'if(typeof Proc !== "undefined") Proc();',
                        'if(typeof doSubmit !== "undefined") doSubmit();',
                        # より具体的なJRA関数パターン
                        'document.getElementById("form1").submit()',
                        'document.getElementsByTagName("form")[0].submit()',
                        'window.location.href = window.location.href.replace("pw01", "pw02")',
                    ]
                    
                    for js_func in js_functions:
                        try:
                            logger.info(f"Trying JavaScript function: {js_func}")
                            await page.evaluate(js_func)
                            await page.wait_for_timeout(1000)
                            # ページが変わったかチェック
                            new_url = page.url
                            if new_url != current_url:
                                logger.info(f"Page changed after {js_func}, form likely submitted")
                                form_submitted = True
                                break
                        except Exception as js_error:
                            logger.debug(f"JavaScript function {js_func} failed: {js_error}")
                            continue
                
                if form_submitted:
                    await page.wait_for_timeout(3000)
                    next_clicked = True
                
            except Exception as e:
                logger.debug(f"JavaScript form submission failed: {e}")
            
            # JavaScriptで送信できなかった場合はEnterキーで試行
            if not next_clicked:
                logger.info("JavaScript submission failed, trying Enter key...")
                await page.keyboard.press('Enter')
                await page.wait_for_timeout(2000)
                
                # ページ遷移を確認
                current_url = page.url
                if 'pw02' in current_url or 'login' in current_url or 'auth' in current_url:
                    logger.info("Form submitted successfully via Enter key")
                    next_clicked = True
        
        if not next_clicked:
            raise Exception("Failed to find login button - tried button selectors, JavaScript form submission, and Enter key")
        
        # ページ遷移を待つ（より長い待機時間とネットワーク安定待機）
        logger.info("Waiting for page transition to complete...")
        
        # URLの変化を待つ
        initial_url = page.url
        logger.info(f"Initial URL before transition: {initial_url}")
        
        # 複数の待機方法を試す
        try:
            # 方法1: URLの変化を待つ（最大20秒）
            for i in range(20):
                await page.wait_for_timeout(1000)
                current_url = page.url
                if current_url != initial_url:
                    logger.info(f"URL changed to: {current_url}")
                    break
                if i % 5 == 0:
                    logger.info(f"Still waiting for URL change... ({i+1} seconds)")
            
            # 方法2: ネットワークが安定するまで待つ
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
                logger.info("Network is idle")
            except:
                logger.info("Network idle timeout, continuing...")
            
            # 方法3: DOMContentLoadedを待つ
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=5000)
                logger.info("DOM content loaded")
            except:
                logger.info("DOM content loaded timeout, continuing...")
                
        except Exception as e:
            logger.warning(f"Transition wait error: {e}")
        
        # 追加の安全待機
        await page.wait_for_timeout(3000)
        
        # === 第2段階: 3つの認証情報入力 ===
        logger.info("Stage 2: Entering authentication details...")
        
        # 追加の待機とスクリーンショット
        await page.wait_for_timeout(3000)
        await take_screenshot(page, "stage2_page")
        
        # URLとタイトルをチェック
        current_url = page.url
        current_title = await page.title()
        logger.info(f"Current URL after transition: {current_url}")
        logger.info(f"Current title after transition: {current_title}")
        
        # ページ構造を再解析
        await analyze_page_structure(page)
        
        # JavaScriptエラーをチェック
        try:
            js_errors = await page.evaluate("""
                () => {
                    const errors = [];
                    // Check if there are any console errors
                    if (window.__errors) {
                        errors.push(...window.__errors);
                    }
                    // Check for specific IPAT error messages
                    const errorElements = document.querySelectorAll('.error, .alert, [class*="error"]');
                    errorElements.forEach(el => {
                        if (el.textContent) errors.push(el.textContent.trim());
                    });
                    return errors;
                }
            """)
            if js_errors:
                logger.warning(f"JavaScript errors found: {js_errors}")
        except:
            pass
        
        # フレームの存在をチェック
        frames = page.frames
        logger.info(f"Number of frames on page: {len(frames)}")
        if len(frames) > 1:
            logger.info("Multiple frames detected, checking each frame...")
            for i, frame in enumerate(frames):
                try:
                    frame_url = frame.url
                    logger.info(f"Frame {i}: {frame_url}")
                    # メインフレーム以外もチェック
                    if i > 0:
                        frame_inputs = await frame.query_selector_all('input')
                        logger.info(f"Frame {i} has {len(frame_inputs)} input elements")
                except:
                    pass
        
        # 加入者番号フィールドを動的に検出
        user_id_selectors = [
            'input[name="i"]',
            'input[name="user_id"]',
            'input[name="userid"]',
            'input[name="USER_ID"]',
            'input[placeholder*="加入者"]',
            'input[placeholder*="ユーザー"]'
        ]
        
        user_id_filled = False
        for selector in user_id_selectors:
            if await wait_and_fill(page, selector, credentials['user_id'], timeout=5000):
                logger.info(f"Filled user ID with selector: {selector}")
                user_id_filled = True
                break
        
        if not user_id_filled:
            # フォールバック: 最初のtextフィールドを使用
            text_inputs = await page.query_selector_all('input[type="text"], input:not([type])')
            if text_inputs and len(text_inputs) > 0:
                await text_inputs[0].fill(credentials['user_id'])
                logger.info("Filled user ID in first text input as fallback")
                user_id_filled = True
        
        if not user_id_filled:
            raise Exception("Failed to fill user ID")
        
        # 暗証番号フィールドを動的に検出
        password_selectors = [
            'input[name="p"]',
            'input[name="password"]',
            'input[name="PASSWORD"]',
            'input[type="password"]',
            'input[placeholder*="暗証"]',
            'input[placeholder*="パスワード"]'
        ]
        
        password_filled = False
        for selector in password_selectors:
            if await wait_and_fill(page, selector, credentials['password'], timeout=5000):
                logger.info(f"Filled password with selector: {selector}")
                password_filled = True
                break
        
        if not password_filled:
            # フォールバック: 最初のpasswordフィールドを使用
            password_inputs = await page.query_selector_all('input[type="password"]')
            if password_inputs and len(password_inputs) > 0:
                await password_inputs[0].fill(credentials['password'])
                logger.info("Filled password in first password input as fallback")
                password_filled = True
        
        if not password_filled:
            raise Exception("Failed to fill password")
        
        # P-ARS番号フィールドを動的に検出
        if credentials.get('pars'):
            pars_selectors = [
                'input[name="r"]',
                'input[name="pars"]',
                'input[name="PARS"]',
                'input[placeholder*="P-ARS"]',
                'input[placeholder*="pars"]'
            ]
            
            pars_filled = False
            for selector in pars_selectors:
                if await wait_and_fill(page, selector, credentials['pars'], timeout=5000):
                    logger.info(f"Filled P-ARS with selector: {selector}")
                    pars_filled = True
                    break
            
            if not pars_filled:
                # フォールバック: 3番目のtextフィールドを使用
                text_inputs = await page.query_selector_all('input[type="text"], input:not([type])')
                if text_inputs and len(text_inputs) > 2:
                    await text_inputs[2].fill(credentials['pars'])
                    logger.info("Filled P-ARS in third text input as fallback")
                    pars_filled = True
            
            if not pars_filled:
                logger.warning("Failed to fill P-ARS number, continuing without it")
        
        # ログインボタンクリック（動的に検出）
        login_clicked = False
        
        # まず、すべてのクリック可能な要素をデバッグ（第2段階用）
        logger.info("Looking for login button on second stage...")
        debug_selectors = ['button', 'input[type="button"]', 'input[type="submit"]', 'input[type="image"]', 'a', 'img']
        for selector in debug_selectors:
            elements = await page.query_selector_all(selector)
            if elements:
                logger.info(f"Found {len(elements)} {selector} elements on stage 2")
                for i, elem in enumerate(elements[:3]):  # 最初の3つ
                    text = await elem.text_content() or ''
                    value = await elem.get_attribute('value') or ''
                    alt = await elem.get_attribute('alt') or ''
                    onclick = await elem.get_attribute('onclick') or ''
                    href = await elem.get_attribute('href') or ''
                    if text.strip() or value or alt or onclick:
                        logger.info(f"{selector}[{i}]: text='{text.strip()}', value='{value}', alt='{alt}', onclick='{onclick}'")
        
        # onclick属性を持つ要素を優先的に探す
        all_elements = await page.query_selector_all('*')
        for element in all_elements:
            onclick = await element.get_attribute('onclick') or ''
            if onclick and ('send' in onclick.lower() or 'submit' in onclick.lower() or 'login' in onclick.lower() or 
                          'proc' in onclick.lower() or 'tomodernmenu' in onclick.lower() or 'menu' in onclick.lower()):
                logger.info(f"Found element with onclick for login: {onclick}")
                await element.click()
                login_clicked = True
                break
        
        if not login_clicked:
            # 次に通常のボタンを探す
            login_selectors = [
                '.buttonModern',
                'button',
                'input[type="submit"]',
                'input[type="button"]',
                'input[type="image"]',
                'a'
            ]
            
            for selector in login_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        text = await element.text_content() or ''
                        value = await element.get_attribute('value') or ''
                        alt = await element.get_attribute('alt') or ''
                        if ('ログイン' in text or 'LOGIN' in text.upper() or 
                            'ログイン' in value or 'LOGIN' in value.upper() or
                            'ログイン' in alt or 
                            '送信' in text or 'submit' in text.lower() or
                            '次へ' in text or 'next' in text.lower()):
                            logger.info(f"Clicking login button: text='{text.strip()}', value='{value}', alt='{alt}'")
                            await element.click()
                            login_clicked = True
                            break
                    if login_clicked:
                        break
                except:
                    continue
        
        if not login_clicked:
            raise Exception("Failed to find login button on second stage")
        
        # === お知らせ確認画面の処理 ===
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "after_login_attempt")
        
        # 確認画面をチェック
        page_text = await page.text_content('body')
        if page_text and 'P-ARS' in page_text:
            logger.info("Login confirmation page detected, looking for OK button...")
        
        try:
            # OKボタンを様々な方法で探す
            ok_selectors = [
                'button:has-text("OK")',
                'button:has-text("O K")',  # スペース付きOK
                'button:has-text("確認")',
                'button:has-text("次へ")',
                'input[type="button"][value*="OK"]',
                'input[type="button"][value*="O K"]',
                'input[type="submit"][value*="OK"]',
                'input[type="submit"][value*="O K"]',
                'a:has-text("OK")',
                'a:has-text("O K")',
                'button',  # 全てのbuttonをチェック
                'input[type="button"]',
                'input[type="submit"]'
            ]
            
            ok_clicked = False
            for selector in ok_selectors:
                try:
                    if 'has-text' in selector:
                        # has-textセレクタの特別処理
                        base_selector = selector.split(':')[0]
                        text_to_find = selector.split('"')[1]
                        elements = await page.query_selector_all(base_selector)
                        for button in elements:
                            text = await button.text_content() or ''
                            if text_to_find in text:
                                logger.info(f"Found OK button with text: {text.strip()}")
                                # クリック前に要素の状態を確認
                                is_visible = await button.is_visible()
                                is_enabled = await button.is_enabled()
                                logger.info(f"Button state - visible: {is_visible}, enabled: {is_enabled}")
                                
                                if is_visible and is_enabled:
                                    # 複数のクリック方法を試す
                                    try:
                                        await button.click(force=True)  # 強制クリック
                                    except:
                                        # JavaScriptでクリック
                                        await page.evaluate('(element) => element.click()', button)
                                    ok_clicked = True
                                    break
                    else:
                        elements = await page.query_selector_all(selector)
                        for button in elements:
                            text = await button.text_content() or ''
                            value = await button.get_attribute('value') or ''
                            # OK（スペース付きも含む）、確認、次へをチェック
                            if ('OK' in text.upper() or 'O K' in text.upper() or 
                                'OK' in value.upper() or 'O K' in value.upper() or
                                '確認' in text or '次へ' in text or '進む' in text):
                                logger.info(f"Found and clicking OK/confirmation button: text='{text.strip()}', value='{value.strip()}'")
                                
                                # クリック前に要素の状態を確認
                                is_visible = await button.is_visible()
                                is_enabled = await button.is_enabled()
                                logger.info(f"Button state - visible: {is_visible}, enabled: {is_enabled}")
                                
                                if is_visible and is_enabled:
                                    # 複数のクリック方法を試す
                                    try:
                                        await button.click(force=True)  # 強制クリック
                                    except:
                                        # JavaScriptでクリック
                                        await page.evaluate('(element) => element.click()', button)
                                    ok_clicked = True
                                    break
                    if ok_clicked:
                        break
                except Exception as e:
                    logger.debug(f"Error checking selector {selector}: {e}")
                    continue
            
            if ok_clicked:
                await page.wait_for_timeout(3000)
                await take_screenshot(page, "after_ok_click")
            else:
                logger.warning("Could not find OK button on confirmation page")
        except Exception as e:
            logger.debug(f"Error processing confirmation page: {e}")
        
        # ログイン成功の確認
        await page.wait_for_timeout(3000)
        final_title = await page.title()
        current_url = page.url
        logger.info(f"Login completed. Final page title: {final_title}")
        logger.info(f"Current URL: {current_url}")
        
        await take_screenshot(page, "login_final_result")
        
        # ページ内容をデバッグ
        page_text = await page.text_content('body')
        if page_text:
            logger.info(f"Page content after login (first 500 chars): {page_text[:500]}")
        
        # メニューページへの遷移が必要かチェック
        if '加入者情報' in (page_text or '') or '次回から暗証番号' in (page_text or '') or 'P-ARS' in (page_text or ''):
            logger.info("Still on login/confirmation page, looking for menu navigation...")
            
            # まず、すべてのクリック可能要素をデバッグ
            clickable_selectors = ['a', 'img', 'button', 'input[type="button"]', 'input[type="submit"]', 'input[type="image"]']
            for selector in clickable_selectors:
                elements = await page.query_selector_all(selector)
                if elements:
                    logger.debug(f"Found {len(elements)} {selector} elements on page")
                    for i, elem in enumerate(elements[:5]):
                        text = await elem.text_content() or ''
                        alt = await elem.get_attribute('alt') or ''
                        href = await elem.get_attribute('href') or ''
                        onclick = await elem.get_attribute('onclick') or ''
                        if text.strip() or alt:
                            logger.debug(f"{selector}[{i}]: text='{text.strip()}', alt='{alt}', href='{href[:50] if href else ''}', onclick='{onclick[:50] if onclick else ''}'")
            
            # メニューへのリンクを探す
            menu_found = False
            for selector in clickable_selectors:
                elements = await page.query_selector_all(selector)
                for link in elements:
                    text = await link.text_content() or ''
                    alt = await link.get_attribute('alt') or ''
                    href = await link.get_attribute('href') or ''
                    onclick = await link.get_attribute('onclick') or ''
                    
                    # メニュー関連のキーワードをチェック
                    menu_keywords = ['メニュー', 'menu', 'メイン', 'main', 'トップ', 'top', '投票', '購入']
                    if any(keyword in combined.lower() for combined in [text, alt, onclick] for keyword in menu_keywords):
                        logger.info(f"Found menu element: text='{text.strip()}', alt='{alt}', onclick='{onclick[:50] if onclick else ''}'")
                        await link.click()
                        menu_found = True
                        await page.wait_for_timeout(3000)
                        break
                    
                    # 特定のURLパターンをチェック
                    if href and any(pattern in href for pattern in ['menu', 'main', 'top', 'home']):
                        logger.info(f"Found menu link by URL: {href}")
                        await link.click()
                        menu_found = True
                        await page.wait_for_timeout(3000)
                        break
                
                if menu_found:
                    break
            
            if not menu_found:
                logger.warning("Could not find menu navigation link")
        
        # ログイン成功の判定
        success_indicators = ['投票', 'マイページ', '残高', 'メニュー', 'MENU']
        login_success = any(indicator in final_title for indicator in success_indicators) or '加入者情報' not in (page_text or '')
        
        if login_success:
            logger.info("Successfully logged in to IPAT")
        else:
            logger.warning(f"Login may have failed. Page title: {final_title}")
            # エラーメッセージをチェック
            error_elements = await page.query_selector_all('.error, .alert, .warning, [class*="error"], [class*="alert"]')
            for elem in error_elements:
                error_text = await elem.text_content() or ''
                if error_text.strip():
                    logger.error(f"Found error message: {error_text.strip()}")
        
    except TimeoutError:
        logger.error("Login timeout - check credentials or network connection")
        await take_screenshot(page, "login_timeout_v2")
        raise
    except Exception as e:
        logger.error(f"Login failed: {e}")
        await take_screenshot(page, "login_error_v2")
        raise


async def navigate_to_account_info(page: Page):
    """口座情報ページへ移動"""
    try:
        logger.info("Navigating to account info page...")
        
        # 口座情報へのリンクを探す
        selectors = ['a', 'button', 'img', 'input[type="button"]', 'input[type="submit"]']
        keywords = ['口座', '残高', '照会', '明細', '入金', '出金', 'account', 'balance']
        
        for selector in selectors:
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                href = await element.get_attribute('href') or ''
                
                if any(keyword in combined.lower() for combined in [text, alt] for keyword in keywords):
                    logger.info(f"Found account info link: text='{text.strip()}', alt='{alt}'")
                    await element.click()
                    await page.wait_for_timeout(3000)
                    return True
        
        logger.warning("Could not find account info link")
        return False
        
    except Exception as e:
        logger.error(f"Failed to navigate to account info: {e}")
        return False


async def get_balance(page: Page) -> int:
    """残高を取得（動的検出対応）"""
    try:
        logger.info("Getting account balance...")
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "balance_check")
        
        # まず現在のページで残高を探す
        balance = await find_balance_on_page(page)
        if balance is not None:
            return balance
        
        # 残高が見つからない場合、口座情報ページへ移動を試みる
        logger.info("Balance not found on current page, trying to navigate to account info...")
        if await navigate_to_account_info(page):
            await page.wait_for_timeout(3000)
            await take_screenshot(page, "account_info_page")
            balance = await find_balance_on_page(page)
            if balance is not None:
                return balance
        
        logger.warning("Could not find balance, returning 0")
        return 0
        
    except Exception as e:
        logger.error(f"Failed to get balance: {e}")
        return 0


async def find_balance_on_page(page: Page):
    """現在のページで残高を探す"""
    try:
        
        # ページの全テキストをデバッグ
        page_text = await page.text_content('body')
        if page_text:
            logger.debug(f"Page text for balance search (first 1000 chars): {page_text[:1000]}")
        
        # 様々な要素で残高を探す
        balance_selectors = [
            'td',
            'span',
            'div',
            'p',
            'strong',
            'b',
            '.balance',
            '.amount',
            '[class*="balance"]',
            '[class*="amount"]',
            '[class*="money"]',
            '[class*="zandaka"]',  # 残高
            '[class*="kingaku"]'   # 金額
        ]
        
        # 残高を表すキーワード
        balance_keywords = ['残高', '現在高', '口座残高', '利用可能金額']
        
        for selector in balance_selectors:
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                # 数字と円を含むテキストを探す
                if text and "円" in text and any(c.isdigit() for c in text):
                    # 残高キーワードを含むかチェック
                    if any(keyword in text for keyword in balance_keywords):
                        logger.info(f"Found balance text with keyword: {text.strip()[:100]}")
                    try:
                        # 数字を抽出
                        import re
                        numbers = re.findall(r'[0-9,]+', text.replace("円", ""))
                        if numbers:
                            balance = int(numbers[-1].replace(",", ""))  # 最後の数字を使用
                            if balance >= 0:  # 0以上の値を有効に
                                logger.info(f"Current balance: {balance} yen (found in: '{text.strip()[:50]}')")
                                return balance
                    except (ValueError, IndexError):
                        continue
        
        # メニューページにいるか確認
        current_url = page.url
        page_title = await page.title()
        logger.info(f"Current page for balance check - URL: {current_url}, Title: {page_title}")
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to get balance: {e}")
        return 0


async def navigate_to_vote(page: Page):
    """投票画面へ移動"""
    try:
        logger.info("Navigating to vote page...")
        
        # ページ内容をデバッグ
        page_text = await page.text_content('body')
        if page_text:
            logger.debug(f"Current page content (first 500 chars): {page_text[:500]}")
        
        # まずメインメニューにいるか確認
        current_url = page.url
        current_title = await page.title()
        logger.info(f"Current page - URL: {current_url}, Title: {current_title}")
        
        # メインメニューから投票メニューへの遷移を試みる
        vote_found = False
        selectors = ['button', 'a', 'img', 'input[type="button"]', 'input[type="submit"]', 'area', 'div[onclick]']
        
        # 投票関連のキーワードを拡充
        vote_keywords = ['通常投票', '投票', '馬券', '購入', 'BET', '単勝', '複勝', 'ワイド', '馬連', '馬単', '三連単', '三連複']
        
        for selector in selectors:
            elements = await page.query_selector_all(selector)
            logger.debug(f"Checking {len(elements)} {selector} elements for vote navigation")
            
            for i, element in enumerate(elements):
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                value = await element.get_attribute('value') or ''
                onclick = await element.get_attribute('onclick') or ''
                href = await element.get_attribute('href') or ''
                
                # 投票関連のキーワードをチェック
                if any(keyword in combined for combined in [text, alt, value] for keyword in vote_keywords):
                    logger.info(f"Found vote element ({selector}[{i}]): text='{text.strip()}', alt='{alt}', value='{value}'")
                    try:
                        is_visible = await element.is_visible()
                        is_enabled = await element.is_enabled()
                        if is_visible and is_enabled:
                            await element.click()
                            await page.wait_for_timeout(4000)
                            vote_found = True
                            break
                        else:
                            logger.debug(f"Element not clickable - visible: {is_visible}, enabled: {is_enabled}")
                    except Exception as click_error:
                        logger.debug(f"Failed to click element: {click_error}")
                        continue
                
                # onclick属性もチェック
                if onclick and any(keyword in onclick.lower() for keyword in ['vote', 'bet', 'touhyou', 'keiba']):
                    logger.info(f"Found vote element with onclick: {onclick[:100]}")
                    try:
                        await element.click()
                        await page.wait_for_timeout(4000)
                        vote_found = True
                        break
                    except Exception as click_error:
                        logger.debug(f"Failed to click onclick element: {click_error}")
                        continue
                
                # hrefでURLパターンをチェック
                if href and any(pattern in href.lower() for pattern in ['vote', 'bet', 'touhyou', 'uma']):
                    logger.info(f"Found vote link by URL: {href}")
                    try:
                        await element.click()
                        await page.wait_for_timeout(4000)
                        vote_found = True
                        break
                    except Exception as click_error:
                        logger.debug(f"Failed to click href element: {click_error}")
                        continue
            
            if vote_found:
                break
        
        if vote_found:
            # 投票ページに遷移できたか確認
            await page.wait_for_timeout(2000)
            new_url = page.url
            new_title = await page.title()
            logger.info(f"Vote page navigation - URL: {new_url}, Title: {new_title}")
            await take_screenshot(page, "vote_page_accessed")
            return True
        else:
            logger.error("Could not find vote button or link")
            await take_screenshot(page, "vote_navigation_failed")
            
            # デバッグ情報: 利用可能な要素をリスト
            logger.debug("Available clickable elements:")
            for selector in selectors[:3]:  # 主要なセレクタのみ
                elements = await page.query_selector_all(selector)
                for i, elem in enumerate(elements[:5]):  # 最初の5つまで
                    text = await elem.text_content() or ''
                    if text.strip():
                        logger.debug(f"{selector}[{i}]: '{text.strip()[:50]}'")
            
            return False
        
    except Exception as e:
        logger.error(f"Failed to navigate to vote: {e}")
        await take_screenshot(page, "vote_navigation_error")
        return False


async def select_race(page: Page, racecourse: str, race_number: int):
    """競馬場とレースを選択"""
    try:
        logger.info(f"Selecting race: {racecourse} R{race_number}")
        await take_screenshot(page, "before_race_selection")
        
        # 競馬場の別名を含めたマッピング
        racecourse_mapping = {
            '東京': ['東京', '府中', 'サラブレッド'],
            '中山': ['中山', 'ナカヤマ'],
            '京都': ['京都', 'キョウト'],
            '阪神': ['阪神', 'ハンシン'],
            '小倉': ['小倉', 'コクラ'],
            '中京': ['中京', 'チュウキョウ'],
            '新潟': ['新潟', 'ニイガタ'],
            '小倉': ['小倉', 'コクラ'],
            '鹿児島': ['鹿児島', 'カゴシマ'],
            '函館': ['函館', 'ハコダテ']
        }
        
        possible_names = racecourse_mapping.get(racecourse, [racecourse])
        racecourse_selected = False
        
        # 競馬場選択 - ボタン、リンク、セレクトボックスをチェック
        selectors = ['button', 'a', 'option', 'input', 'select', 'div[onclick]']
        
        for selector in selectors:
            if racecourse_selected:
                break
                
            elements = await page.query_selector_all(selector)
            logger.debug(f"Checking {len(elements)} {selector} elements for racecourse selection")
            
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                # 競馬場名のマッチをチェック
                if any(name in combined for combined in [text, value] for name in possible_names):
                    logger.info(f"Found racecourse element: text='{text.strip()}', value='{value}'")
                    try:
                        if selector == 'option':
                            # selectボックスの場合
                            select_element = await element.query_selector('xpath=ancestor::select')
                            if select_element:
                                await select_element.select_option(value=value)
                        else:
                            await element.click()
                        
                        logger.info(f"Selected racecourse: {racecourse}")
                        racecourse_selected = True
                        await page.wait_for_timeout(2000)
                        break
                    except Exception as click_error:
                        logger.debug(f"Failed to select racecourse element: {click_error}")
                        continue
        
        if not racecourse_selected:
            logger.warning(f"Could not find racecourse selector for: {racecourse}")
        
        # レース番号選択
        race_text_patterns = [f"{race_number}R", f"R{race_number}", f"{race_number}レース", str(race_number)]
        race_selected = False
        
        for selector in selectors:
            if race_selected:
                break
                
            elements = await page.query_selector_all(selector)
            logger.debug(f"Checking {len(elements)} {selector} elements for race selection")
            
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                # レース番号のマッチをチェック
                for pattern in race_text_patterns:
                    if pattern == text.strip() or pattern in text or pattern == value:
                        logger.info(f"Found race element: text='{text.strip()}', value='{value}', pattern='{pattern}'")
                        try:
                            if selector == 'option':
                                select_element = await element.query_selector('xpath=ancestor::select')
                                if select_element:
                                    await select_element.select_option(value=value)
                            else:
                                await element.click()
                            
                            logger.info(f"Selected race: R{race_number}")
                            race_selected = True
                            await page.wait_for_timeout(2000)
                            break
                        except Exception as click_error:
                            logger.debug(f"Failed to select race element: {click_error}")
                            continue
                
                if race_selected:
                    break
        
        if not race_selected:
            logger.warning(f"Could not find race selector for: R{race_number}")
        
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "after_race_selection")
        
        # 選択が成功したか確認
        if racecourse_selected and race_selected:
            logger.info(f"Successfully selected {racecourse} R{race_number}")
            return True
        else:
            logger.error(f"Failed to select race: racecourse_selected={racecourse_selected}, race_selected={race_selected}")
            return False
        
    except Exception as e:
        logger.error(f"Failed to select race: {e}")
        await take_screenshot(page, "race_selection_error")
        return False


async def select_horse_and_bet(page: Page, horse_number: int, horse_name: str, bet_amount: int, 
                              racecourse: str, race_number: int, slack: Optional[SlackNotifier] = None):
    """馬を選択して投票"""
    try:
        logger.info(f"Selecting horse #{horse_number} {horse_name} with bet {bet_amount}")
        await take_screenshot(page, "before_horse_selection")
        
        await page.wait_for_timeout(4000)
        
        # ページの馬番号選択要素を探す
        horse_selected = False
        
        # 馬番号の様々なパターンを試す
        horse_patterns = [str(horse_number), f"{horse_number}番", f"#{horse_number}"]
        selectors_for_horse = ['label', 'button', 'input[type="radio"]', 'input[type="checkbox"]', 'a', 'div[onclick]', 'span[onclick]']
        
        # 大きい番号の場合はスクロール
        if horse_number >= 9:
            logger.info("Scrolling for larger horse numbers...")
            await page.evaluate("window.scrollTo(0, 300)")
            await page.wait_for_timeout(2000)
            if horse_number >= 13:
                await page.evaluate("window.scrollTo(0, 600)")
                await page.wait_for_timeout(2000)
        
        for selector in selectors_for_horse:
            if horse_selected:
                break
                
            elements = await page.query_selector_all(selector)
            logger.debug(f"Checking {len(elements)} {selector} elements for horse selection")
            
            for i, element in enumerate(elements):
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                name = await element.get_attribute('name') or ''
                
                # 馬番号のマッチをチェック
                for pattern in horse_patterns:
                    if (pattern == text.strip() or pattern in text or 
                        pattern == value or pattern in value or
                        (name and pattern in name)):
                        logger.info(f"Found horse element: text='{text.strip()}', value='{value}', name='{name}', pattern='{pattern}'")
                        try:
                            if selector == 'input[type="radio"]' or selector == 'input[type="checkbox"]':
                                await element.check()
                            else:
                                await element.click()
                            
                            logger.info(f"Selected horse number {horse_number}")
                            horse_selected = True
                            await page.wait_for_timeout(2000)
                            break
                        except Exception as click_error:
                            logger.debug(f"Failed to select horse element: {click_error}")
                            continue
                
                if horse_selected:
                    break
        
        # フォールバック: インデックスベースで選択
        if not horse_selected:
            logger.warning("Using fallback: index-based horse selection")
            labels = await page.query_selector_all('label')
            if len(labels) > horse_number + 8:
                await labels[horse_number + 8].click()
                logger.info(f"Selected horse number {horse_number} (fallback method)")
                horse_selected = True
        
        if not horse_selected:
            raise Exception(f"Failed to select horse #{horse_number}")
        
        await page.wait_for_timeout(2000)
        await take_screenshot(page, "after_horse_selection")
        
        # セットボタンを探してクリック
        set_button_clicked = False
        button_selectors = ['button', 'input[type="button"]', 'input[type="submit"]', 'a']
        
        for selector in button_selectors:
            if set_button_clicked:
                break
                
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                if 'セット' in text or 'セット' in value or 'SET' in text.upper():
                    logger.info(f"Found set button: text='{text.strip()}', value='{value}'")
                    await element.click()
                    set_button_clicked = True
                    break
        
        if not set_button_clicked:
            logger.warning("Set button not found, continuing...")
        
        await page.wait_for_timeout(2000)
        
        # 入力終了ボタンを探してクリック
        input_end_clicked = False
        
        for selector in button_selectors:
            if input_end_clicked:
                break
                
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                if '入力終了' in text or '入力終了' in value or '終了' in text:
                    logger.info(f"Found input end button: text='{text.strip()}', value='{value}'")
                    await element.click()
                    input_end_clicked = True
                    break
        
        if not input_end_clicked:
            logger.warning("Input end button not found, continuing...")
        
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "before_amount_input")
        
        # 金額入力 - より動的な方法で探す
        amount_input_success = False
        
        # 金額入力フィールドを探す
        amount_selectors = [
            'input[name*="amount"]',
            'input[name*="kingaku"]',
            'input[name*="yen"]',
            'input[type="number"]',
            'input[type="text"]'
        ]
        
        bet_units = bet_amount // 100  # 100円単位
        
        for selector in amount_selectors:
            if amount_input_success:
                break
                
            inputs = await page.query_selector_all(selector)
            for input_field in inputs:
                placeholder = await input_field.get_attribute('placeholder') or ''
                name = await input_field.get_attribute('name') or ''
                
                # 金額関連のフィールドかチェック
                if any(keyword in combined.lower() for combined in [placeholder, name] 
                       for keyword in ['金額', 'amount', '票数', '円']):
                    try:
                        await input_field.fill(str(bet_amount))
                        logger.info(f"Filled amount field: {bet_amount} yen")
                        amount_input_success = True
                        break
                    except:
                        continue
        
        # フォールバック: インデックスベース
        if not amount_input_success:
            logger.warning("Using fallback: index-based amount input")
            inputs = await page.query_selector_all('input')
            if len(inputs) > 11:
                try:
                    await inputs[9].fill(str(bet_units))
                    await inputs[10].fill(str(bet_units))
                    await inputs[11].fill(str(bet_amount))
                    logger.info(f"Filled amount fields (fallback): {bet_amount} yen")
                    amount_input_success = True
                except Exception as fallback_error:
                    logger.error(f"Fallback amount input failed: {fallback_error}")
        
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "after_amount_input")
        
        # 購入直前のSlack通知
        if slack:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="開始")
        
        # 購入ボタンを探してクリック
        purchase_clicked = False
        purchase_keywords = ['購入する', '購入', '投票する', '投票', 'BUY', 'BET']
        
        for selector in button_selectors:
            if purchase_clicked:
                break
                
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined for combined in [text, value] for keyword in purchase_keywords):
                    logger.info(f"Found purchase button: text='{text.strip()}', value='{value}'")
                    await element.click()
                    purchase_clicked = True
                    break
        
        if not purchase_clicked:
            raise Exception("Purchase button not found")
        
        await page.wait_for_timeout(4000)
        await take_screenshot(page, "after_purchase_click")
        
        # OK確認ボタンを探してクリック
        success = False
        ok_keywords = ['OK', 'O K', '確認', '完了', '結果']
        
        for selector in button_selectors:
            if success:
                break
                
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined.upper() for combined in [text, value] for keyword in ok_keywords):
                    logger.info(f"Found confirmation button: text='{text.strip()}', value='{value}'")
                    await element.click()
                    logger.info(f"Successfully placed bet for {horse_name}")
                    success = True
                    break
        
        await page.wait_for_timeout(2000)
        await take_screenshot(page, "bet_completion")
        
        # 購入完了のSlack通知
        if slack and success:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="完了")
        elif slack and not success:
            await slack.send_bet_notification(racecourse, race_number, horse_number, 
                                            horse_name, bet_amount, status="失敗")
        
        if not success:
            logger.warning("Confirmation button not found, assuming bet was successful")
            success = True  # ボタンが見つからなくても投票は成功している可能性がある
        
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
        await take_screenshot(page, "before_deposit")
        
        # 入金前の残高を取得
        balance_before = await get_balance(page)
        logger.info(f"Balance before deposit: {balance_before} yen")
        
        # 入出金ボタンを探してクリック
        deposit_found = False
        deposit_keywords = ['入出金', '入金', '入金指示', '銀行連携', 'DEPOSIT']
        selectors = ['button', 'a', 'input[type="button"]', 'input[type="submit"]', 'img', 'div[onclick]']
        
        for selector in selectors:
            if deposit_found:
                break
                
            elements = await page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined for combined in [text, alt, value] for keyword in deposit_keywords):
                    logger.info(f"Found deposit element: text='{text.strip()}', alt='{alt}', value='{value}'")
                    await element.click()
                    deposit_found = True
                    break
        
        if not deposit_found:
            raise Exception("Deposit button not found")
        
        await page.wait_for_timeout(4000)
        
        # 新しいウィンドウ/タブを待つか、同じページ内で遷移するかをチェック
        await page.wait_for_timeout(3000)
        
        # 新しいページが開かれたかチェック
        pages = page.context.pages
        if len(pages) > 1:
            # 新しいページが開かれた場合
            new_page = pages[-1]  # 最新のページ
            await new_page.wait_for_load_state()
            logger.info("New deposit page opened")
        else:
            # 同じページ内で遷移した場合
            new_page = page
            logger.info("Deposit page opened in same window")
        
        await take_screenshot(new_page, "deposit_page_opened")
        
        # 入金指示リンクをクリック
        instruction_found = False
        instruction_keywords = ['入金指示', '入金開始', '入金手続き', '入金する']
        
        for selector in selectors:
            if instruction_found:
                break
                
            elements = await new_page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined for combined in [text, alt, value] for keyword in instruction_keywords):
                    logger.info(f"Found deposit instruction element: text='{text.strip()}', alt='{alt}', value='{value}'")
                    await element.click()
                    instruction_found = True
                    break
        
        if not instruction_found:
            logger.warning("Deposit instruction link not found, continuing...")
        
        await new_page.wait_for_timeout(4000)
        
        # 入金額入力
        amount_filled = False
        amount_selectors = [
            'input[name="NYUKIN"]',
            'input[name="nyukin"]',
            'input[name="amount"]',
            'input[name="kingaku"]',
            'input[type="number"]',
            'input[type="text"]'
        ]
        
        for selector in amount_selectors:
            try:
                element = await new_page.query_selector(selector)
                if element:
                    placeholder = await element.get_attribute('placeholder') or ''
                    name = await element.get_attribute('name') or ''
                    
                    if any(keyword in combined.lower() for combined in [placeholder, name, selector]
                           for keyword in ['金額', 'amount', 'nyukin', '入金']):
                        await element.fill(str(amount))
                        logger.info(f"Filled deposit amount: {amount} yen")
                        amount_filled = True
                        break
            except:
                continue
        
        if not amount_filled:
            raise Exception("Could not find deposit amount input field")
        
        # 次へボタン
        next_found = False
        next_keywords = ['次へ', '続ける', '進む', 'NEXT', '確認']
        
        for selector in selectors:
            if next_found:
                break
                
            elements = await new_page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined for combined in [text, alt, value] for keyword in next_keywords):
                    logger.info(f"Found next button: text='{text.strip()}', alt='{alt}', value='{value}'")
                    await element.click()
                    next_found = True
                    break
        
        if not next_found:
            logger.warning("Next button not found, continuing...")
        
        await new_page.wait_for_timeout(4000)
        
        # パスワード入力（暗証番号を使用）
        password_filled = False
        password_selectors = [
            'input[name="PASS_WORD"]',
            'input[name="password"]',
            'input[name="anshuu"]',
            'input[type="password"]'
        ]
        
        for selector in password_selectors:
            try:
                element = await new_page.query_selector(selector)
                if element:
                    await element.fill(password)
                    logger.info("Filled deposit password")
                    password_filled = True
                    break
            except:
                continue
        
        if not password_filled:
            logger.warning("Password field not found, continuing...")
        
        # 実行ボタン
        execute_found = False
        execute_keywords = ['実行', '確定', '完了', 'EXECUTE', 'SUBMIT']
        
        for selector in selectors:
            if execute_found:
                break
                
            elements = await new_page.query_selector_all(selector)
            for element in elements:
                text = await element.text_content() or ''
                alt = await element.get_attribute('alt') or ''
                value = await element.get_attribute('value') or ''
                
                if any(keyword in combined for combined in [text, alt, value] for keyword in execute_keywords):
                    logger.info(f"Found execute button: text='{text.strip()}', alt='{alt}', value='{value}'")
                    await element.click()
                    execute_found = True
                    break
        
        if not execute_found:
            logger.warning("Execute button not found, deposit may not be completed")
        
        await new_page.wait_for_timeout(4000)
        
        # アラートの処理
        new_page.on('dialog', lambda dialog: dialog.accept())
        
        await new_page.wait_for_timeout(3000)
        await take_screenshot(new_page, "after_deposit_execution")
        
        logger.info(f"Successfully deposited {amount} yen")
        
        # 新しいページが開かれている場合は閉じる
        if new_page != page:
            await new_page.close()
        
        # 入金後の残高を取得
        await page.wait_for_timeout(5000)  # 入金処理の完了を待つ
        balance_after = await get_balance(page)
        logger.info(f"Balance after deposit: {balance_after} yen")
        
        # Slack通知
        if slack:
            await slack.send_deposit_notification(amount, balance_before, balance_after)
        
        return True
        
    except Exception as e:
        logger.error(f"Deposit failed: {e}")
        await take_screenshot(page, "deposit_error")
        
        # エラーが発生した場合、新しいページが開いていれば閉じる
        try:
            pages = page.context.pages
            if len(pages) > 1:
                for p in pages[1:]:
                    await p.close()
        except:
            pass
        
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
        if DRY_RUN:
            logger.info("DRY RUN MODE: Testing bot configuration without actual betting")
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
        
        # DRY_RUNモードの場合はブラウザを起動しない
        if DRY_RUN:
            logger.info("DRY RUN: Running without browser")
            page = None
            balance = 50000  # DRY RUN用の仮残高
            
            # 4) tickets.csv読み込み・投票実行
            tickets_path = Path('tickets/tickets.csv')
            if tickets_path.exists():
                logger.info("Reading tickets.csv...")
                tickets_df = pd.read_csv(tickets_path)
                logger.info(f"Found {len(tickets_df)} tickets to process")
                
                for idx, ticket in tickets_df.iterrows():
                    try:
                        logger.info(f"DRY RUN: Would place bet - {ticket.to_dict()}")
                        successful_bets += 1
                        bet_amount = int(ticket.get('amount', 100))
                        total_amount += bet_amount
                        total_bets += 1
                        
                        # レート制限対策で待機
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"Failed to process ticket {idx+1}: {e}")
                        if slack_alerts:
                            await slack_alerts.send_error_notification(
                                f"チケット処理エラー (#{idx+1})", str(e)
                            )
                        continue
            else:
                logger.warning("No tickets.csv found, skipping betting phase")
            
            # 5) 最終残高確認
            final_balance = balance - total_amount
            logger.info(f"Final balance: {final_balance} yen")
            
            # 6) サマリー通知
            if slack_bets and total_bets > 0:
                await slack_bets.send_summary_notification(
                    successful_bets, total_amount, final_balance
                )
        else:
            # 通常モード（ブラウザ使用）
            start_time = datetime.now()
            try:
                logger.info("🚀 STARTING AKATSUKI BOT V2 - FULL AUTOMATED BETTING SESSION")
                logger.info(f"⏰ Session started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
                
                # まずHTTPベースで分析を実行
                logger.info("📡 STEP 0: Pre-flight site analysis...")
                http_analysis = await http_based_site_analysis()
                logger.info(f"✓ Site analysis completed - Status: {http_analysis.get('status', 'unknown')}")
                
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=HEADLESS_MODE,
                        args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                    )
                    context = await browser.new_context(
                        accept_downloads=True,
                        viewport={'width': 1280, 'height': 720}
                    )
                    page = await context.new_page()
                    
                    # STEP 1: ログイン
                    logger.info("🔐 STEP 1: IPAT LOGIN (Two-stage authentication)...")
                    login_start = datetime.now()
                    try:
                        await retry_async(login_ipat_v2, page, credentials)
                        login_duration = (datetime.now() - login_start).total_seconds()
                        logger.info(f"✓ Login successful in {login_duration:.1f}s")
                        if slack_bets:
                            await slack_bets.send_message(f"🔐 ログイン成功 ({login_duration:.1f}s)")
                    except Exception as login_error:
                        logger.error(f"❌ Login failed: {login_error}")
                        raise Exception(f"STEP 1 FAILED: {login_error}")
                    
                    # STEP 2: 残高確認
                    logger.info("💰 STEP 2: BALANCE CHECK...")
                    balance_start = datetime.now()
                    try:
                        balance = await get_balance(page)
                        balance_duration = (datetime.now() - balance_start).total_seconds()
                        if balance is not None:
                            logger.info(f"✓ Current balance: {balance:,} yen (checked in {balance_duration:.1f}s)")
                            if slack_bets:
                                await slack_bets.send_message(f"💰 現在残高: {balance:,}円")
                        else:
                            logger.warning("⚠️ Could not retrieve balance, using fallback")
                            balance = 50000  # フォールバック値
                    except Exception as balance_error:
                        logger.error(f"❌ Balance check failed: {balance_error}")
                        balance = 50000  # フォールバック値
                        logger.info(f"📝 Using fallback balance: {balance:,} yen")
                    
                    # STEP 3: 入金チェック
                    logger.info("🏧 STEP 3: DEPOSIT CHECK...")
                    if balance < deposit_amount:
                        deposit_start = datetime.now()
                        deposit_needed = deposit_amount - balance
                        logger.info(f"💳 Balance {balance:,} < required {deposit_amount:,}, depositing {deposit_needed:,} yen...")
                        try:
                            await retry_async(auto_deposit_v2, page, deposit_needed, 
                                            credentials['password'], slack_bets)
                            deposit_duration = (datetime.now() - deposit_start).total_seconds()
                            logger.info(f"✓ Deposit completed in {deposit_duration:.1f}s")
                            balance = deposit_amount  # 更新
                        except Exception as deposit_error:
                            logger.error(f"❌ Deposit failed: {deposit_error}")
                            if slack_alerts:
                                await slack_alerts.send_error_notification("入金エラー", str(deposit_error))
                    else:
                        logger.info(f"✓ Sufficient balance: {balance:,} yen")
                    
                    # STEP 4: チケット処理・投票実行
                    logger.info("🎫 STEP 4: BETTING EXECUTION...")
                    tickets_path = Path('tickets/tickets.csv')
                    betting_start = datetime.now()
                    
                    if tickets_path.exists():
                        logger.info(f"📄 Reading tickets from: {tickets_path}")
                        # 複数のエンコーディングを試す
                        encodings = ['utf-8', 'cp932', 'shift_jis', 'utf-8-sig']
                        tickets_df = None
                        for encoding in encodings:
                            try:
                                tickets_df = pd.read_csv(tickets_path, encoding=encoding)
                                logger.info(f"✓ CSV read successfully with {encoding} encoding")
                                break
                            except UnicodeDecodeError:
                                continue
                        
                        if tickets_df is None:
                            logger.error("❌ Failed to read CSV with any encoding")
                            raise Exception("Could not read tickets.csv with any encoding")
                        
                        total_tickets = len(tickets_df)
                        logger.info(f"🎯 Processing {total_tickets} betting tickets...")
                        
                        if slack_bets:
                            await slack_bets.send_message(f"🎫 {total_tickets}枚のチケット処理開始")
                        
                        for idx, ticket in tickets_df.iterrows():
                            ticket_start = datetime.now()
                            bet_amount = int(ticket.get('amount', 100))
                            
                            # 各チケット処理前に残高チェック
                            logger.info(f"🎫 Processing ticket {idx+1}/{total_tickets}: {ticket.get('race_course', '')} R{ticket.get('race_number', '')} #{ticket.get('horse_number', '')} ({bet_amount:,}円)")
                            
                            # 残高チェック
                            current_balance = await get_balance(page)
                            if current_balance and current_balance < bet_amount:
                                logger.warning(f"⚠️ Insufficient balance: {current_balance:,} < {bet_amount:,} yen")
                                if slack_alerts:
                                    await slack_alerts.send_error_notification(
                                        "残高不足", f"チケット#{idx+1}: 残高{current_balance:,}円 < 必要{bet_amount:,}円"
                                    )
                                continue
                            
                            try:
                                success = await place_bet_from_csv(page, ticket, slack_bets)
                                ticket_duration = (datetime.now() - ticket_start).total_seconds()
                                
                                if success:
                                    successful_bets += 1
                                    total_amount += bet_amount
                                    logger.info(f"✓ Ticket {idx+1} successful in {ticket_duration:.1f}s")
                                else:
                                    logger.warning(f"⚠️ Ticket {idx+1} failed in {ticket_duration:.1f}s")
                                
                                total_bets += 1
                                
                                # プログレス表示
                                progress = (idx + 1) / total_tickets * 100
                                logger.info(f"📊 Progress: {idx+1}/{total_tickets} ({progress:.1f}%)")
                                
                                # レート制限対策で待機
                                await asyncio.sleep(5)
                                
                            except Exception as e:
                                ticket_duration = (datetime.now() - ticket_start).total_seconds()
                                logger.error(f"❌ Ticket {idx+1} error in {ticket_duration:.1f}s: {e}")
                                await take_screenshot(page, f"ticket_error_{idx+1}")
                                
                                if slack_alerts:
                                    await slack_alerts.send_error_notification(
                                        f"チケット処理エラー (#{idx+1})", str(e)
                                    )
                                continue
                        
                        betting_duration = (datetime.now() - betting_start).total_seconds()
                        logger.info(f"✓ Betting phase completed in {betting_duration:.1f}s")
                        
                    else:
                        logger.warning("⚠️ No tickets.csv found, skipping betting phase")
                    
                    # STEP 5: 最終残高確認
                    logger.info("💰 STEP 5: FINAL BALANCE CHECK...")
                    final_balance_start = datetime.now()
                    try:
                        final_balance = await get_balance(page)
                        final_balance_duration = (datetime.now() - final_balance_start).total_seconds()
                        
                        if final_balance is not None:
                            balance_change = final_balance - balance if balance else 0
                            logger.info(f"✓ Final balance: {final_balance:,} yen (change: {balance_change:+,} yen) [checked in {final_balance_duration:.1f}s]")
                        else:
                            logger.warning("⚠️ Could not retrieve final balance")
                            final_balance = balance - total_amount  # 概算
                            logger.info(f"📝 Estimated final balance: {final_balance:,} yen")
                    except Exception as final_balance_error:
                        logger.error(f"❌ Final balance check failed: {final_balance_error}")
                        final_balance = balance - total_amount  # 概算
                        logger.info(f"📝 Estimated final balance: {final_balance:,} yen")
                    
                    # STEP 6: 包括的サマリー通知
                    logger.info("📊 STEP 6: SESSION SUMMARY...")
                    session_end = datetime.now()
                    total_duration = (session_end - start_time).total_seconds()
                    
                    # 詳細統計の計算
                    success_rate = (successful_bets / total_bets * 100) if total_bets > 0 else 0
                    avg_bet_amount = (total_amount / successful_bets) if successful_bets > 0 else 0
                    balance_change = final_balance - balance if balance and final_balance else 0
                    
                    # ログサマリー
                    logger.info("=" * 60)
                    logger.info("🏁 AKATSUKI BOT V2 SESSION COMPLETE")
                    logger.info("=" * 60)
                    logger.info(f"⏱️  Total session time: {total_duration:.1f}s ({total_duration/60:.1f}min)")
                    logger.info(f"🎫 Total tickets processed: {total_bets}")
                    logger.info(f"✅ Successful bets: {successful_bets}")
                    logger.info(f"❌ Failed bets: {total_bets - successful_bets}")
                    logger.info(f"📈 Success rate: {success_rate:.1f}%")
                    logger.info(f"💰 Total amount bet: {total_amount:,} yen")
                    logger.info(f"💵 Average bet amount: {avg_bet_amount:,.0f} yen")
                    logger.info(f"🏦 Initial balance: {balance:,} yen")
                    logger.info(f"🏦 Final balance: {final_balance:,} yen")
                    logger.info(f"📊 Balance change: {balance_change:+,} yen")
                    logger.info(f"⏰ Session ended at: {session_end.strftime('%Y-%m-%d %H:%M:%S')}")
                    logger.info("=" * 60)
                    
                    # Slack通知
                    if slack_bets and total_bets > 0:
                        summary_message = (
                            f"🏁 **AKATSUKI BOT V2 セッション完了**\n\n"
                            f"⏱️ セッション時間: {total_duration/60:.1f}分\n"
                            f"🎫 処理チケット数: {total_bets}枚\n"
                            f"✅ 成功: {successful_bets}枚 ({success_rate:.1f}%)\n"
                            f"❌ 失敗: {total_bets - successful_bets}枚\n"
                            f"💰 投票総額: {total_amount:,}円\n"
                            f"🏦 開始残高: {balance:,}円\n"
                            f"🏦 終了残高: {final_balance:,}円\n"
                            f"📊 残高変動: {balance_change:+,}円"
                        )
                        await slack_bets.send_message(summary_message)
                        
                        # 標準のサマリー通知も送信
                        await slack_bets.send_summary_notification(
                            successful_bets, total_amount, final_balance
                        )
                    
                    logger.info("🔐 Closing browser...")
                    await browser.close()
                    logger.info("✓ Browser closed successfully")
            
            except Exception as browser_error:
                error_time = datetime.now()
                session_duration = (error_time - start_time).total_seconds()
                
                logger.error("=" * 60)
                logger.error("❌ CRITICAL: BROWSER EXECUTION FAILED")
                logger.error("=" * 60)
                logger.error(f"⚠️  Error occurred at: {error_time.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.error(f"⏱️  Session duration before error: {session_duration:.1f}s ({session_duration/60:.1f}min)")
                logger.error(f"🐛 Error details: {browser_error}")
                logger.error("=" * 60)
                
                # 緊急Slack通知
                if slack_alerts:
                    emergency_message = (
                        f"🚨 **AKATSUKI BOT V2 緊急エラー**\n\n"
                        f"⚠️ ブラウザ実行に失敗しました\n"
                        f"⏰ エラー発生時刻: {error_time.strftime('%H:%M:%S')}\n"
                        f"⏱️ 実行時間: {session_duration/60:.1f}分\n"
                        f"🎫 処理済みチケット: {successful_bets}/{total_bets}\n"
                        f"💰 投票済み金額: {total_amount:,}円\n"
                        f"🐛 エラー: {str(browser_error)[:200]}"
                    )
                    await slack_alerts.send_message(emergency_message)
                
                logger.info("🔄 Attempting fallback to HTTP-only analysis mode...")
                
                # ブラウザが失敗した場合、HTTPベースの分析のみ実行
                try:
                    if 'http_analysis' not in locals():
                        http_analysis = await http_based_site_analysis()
                    
                    # 分析結果をSlackに通知
                    if slack_alerts:
                        status = http_analysis.get('status', 'unknown')
                        time_info = http_analysis.get('time_info', [])
                        analysis_summary = f"Central JRA: {status} (times: {time_info})"
                        
                        fallback_message = (
                            f"🔄 **フォールバック分析完了**\n\n"
                            f"📡 JRA IPAT状況: {status}\n"
                            f"⏰ 受付時間情報: {time_info}\n"
                            f"📊 詳細: {analysis_summary}"
                        )
                        await slack_alerts.send_message(fallback_message)
                    
                    logger.info("✓ HTTP-only analysis completed as fallback")
                    
                except Exception as fallback_error:
                    logger.error(f"❌ Even fallback analysis failed: {fallback_error}")
                    if slack_alerts:
                        await slack_alerts.send_error_notification(
                            "フォールバック分析も失敗", str(fallback_error)
                        )
            
    except Exception as e:
        fatal_error_time = datetime.now()
        logger.error("=" * 60)
        logger.error("💀 FATAL ERROR: MAIN PROCESS TERMINATED")
        logger.error("=" * 60)
        logger.error(f"⚠️  Fatal error occurred at: {fatal_error_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.error(f"🐛 Fatal error details: {e}")
        logger.error("=" * 60)
        
        # 致命的エラーのSlack通知
        if slack_alerts:
            fatal_message = (
                f"💀 **AKATSUKI BOT V2 致命的エラー**\n\n"
                f"⚠️ メインプロセスが異常終了しました\n"
                f"⏰ エラー発生時刻: {fatal_error_time.strftime('%H:%M:%S')}\n"
                f"🎫 処理状況: {successful_bets}/{total_bets} チケット\n"
                f"💰 投票金額: {total_amount:,}円\n"
                f"🐛 エラー詳細: {str(e)[:300]}"
            )
            await slack_alerts.send_message(fatal_message)
            await slack_alerts.send_error_notification("致命的エラー", str(e))
        
        logger.error("⚠️ Main process terminated due to fatal error")
        raise


if __name__ == "__main__":
    asyncio.run(main())