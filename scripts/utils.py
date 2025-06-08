#!/usr/bin/env python3
"""ユーティリティ関数"""
import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
import logging
from playwright.async_api import Page, Error as PlaywrightError

logger = logging.getLogger(__name__)


class RetryConfig:
    """リトライ設定"""
    # 環境に応じてリトライ回数を変更
    MAX_RETRIES = 3 if os.environ.get('ENV', 'development') == 'production' else 1
    RETRY_DELAY = 5  # seconds
    EXPONENTIAL_BACKOFF = True


async def retry_async(func, *args, max_retries: int = RetryConfig.MAX_RETRIES, 
                     delay: int = RetryConfig.RETRY_DELAY, **kwargs):
    """非同期関数のリトライラッパー"""
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            
            if attempt < max_retries - 1:
                wait_time = delay * (2 ** attempt if RetryConfig.EXPONENTIAL_BACKOFF else 1)
                logger.info(f"Waiting {wait_time} seconds before retry...")
                await asyncio.sleep(wait_time)
    
    logger.error(f"All {max_retries} attempts failed")
    raise last_exception


async def take_screenshot(page: Page, name: str = "error", 
                         directory: str = "output/screenshots") -> Optional[str]:
    """エラー時のスクリーンショット取得"""
    try:
        # スクリーンショット保存ディレクトリ作成
        screenshot_dir = Path(directory)
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        
        # ファイル名生成
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{name}_{timestamp}.png"
        filepath = screenshot_dir / filename
        
        # スクリーンショット撮影
        await page.screenshot(path=str(filepath), full_page=True)
        logger.info(f"Screenshot saved: {filepath}")
        
        return str(filepath)
        
    except Exception as e:
        logger.error(f"Failed to take screenshot: {e}")
        return None


async def wait_and_click(page: Page, selector: str, timeout: int = 30000) -> bool:
    """要素を待機してクリック"""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.click(selector)
        return True
    except PlaywrightError as e:
        logger.error(f"Failed to click selector '{selector}': {e}")
        await take_screenshot(page, f"click_error_{selector.replace(' ', '_')}")
        return False


async def wait_and_fill(page: Page, selector: str, value: str, timeout: int = 30000) -> bool:
    """要素を待機して入力"""
    try:
        await page.wait_for_selector(selector, timeout=timeout)
        await page.fill(selector, value)
        return True
    except PlaywrightError as e:
        logger.error(f"Failed to fill selector '{selector}': {e}")
        await take_screenshot(page, f"fill_error_{selector.replace(' ', '_')}")
        return False


async def safe_navigate(page: Page, url: str, timeout: int = 60000) -> bool:
    """安全なページ遷移"""
    try:
        response = await page.goto(url, wait_until='networkidle', timeout=timeout)
        if response and response.status >= 400:
            logger.error(f"HTTP error {response.status} when navigating to {url}")
            return False
        return True
    except Exception as e:
        logger.error(f"Navigation failed to {url}: {e}")
        await take_screenshot(page, "navigation_error")
        return False


def create_logs_directory():
    """ログディレクトリの作成"""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    return logs_dir


def setup_file_logging(name: str = "bot") -> logging.Logger:
    """ファイルログの設定"""
    logs_dir = create_logs_directory()
    
    # ログファイル名
    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = logs_dir / f"{name}_{timestamp}.log"
    
    # ファイルハンドラ設定
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    
    # フォーマッタ設定
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(formatter)
    
    # ロガーに追加
    logger = logging.getLogger(name)
    logger.addHandler(file_handler)
    
    return logger