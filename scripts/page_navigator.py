"""
PageNavigator - Playwright Page操作の抽象化レイヤー

重複コードを削減し、テストを容易にするための抽象化クラス
"""

import logging
from typing import List, Optional
from playwright.async_api import Page, ElementHandle

from constants import Timeouts


class PageNavigator:
    """Playwright Page操作の抽象化クラス"""

    def __init__(self, page: Page, logger: Optional[logging.Logger] = None):
        """
        Args:
            page: Playwright Pageオブジェクト
            logger: ロガー（Noneの場合はデフォルトロガーを使用）
        """
        self.page = page
        self.logger = logger or logging.getLogger(__name__)

    async def find_and_click_button(
        self,
        text: str,
        timeout: int = Timeouts.SELECTOR_WAIT,
        exact: bool = False
    ) -> bool:
        """
        テキストでボタンを探してクリック

        Args:
            text: ボタンのテキスト
            timeout: タイムアウト（ミリ秒）
            exact: 完全一致するか（Falseの場合は部分一致）

        Returns:
            bool: クリック成功したらTrue
        """
        try:
            # button要素を全て取得
            buttons = await self.page.query_selector_all('button')

            for button in buttons:
                button_text = await button.text_content()
                if button_text:
                    button_text = button_text.strip()
                    # 完全一致 or 部分一致でチェック
                    if (exact and button_text == text) or (not exact and text in button_text):
                        self.logger.info(f"✓ Clicking button: {text}")
                        await button.click()
                        await self.page.wait_for_timeout(Timeouts.SHORT)
                        return True

            self.logger.warning(f"⚠️ Button not found: {text}")
            return False

        except Exception as e:
            self.logger.error(f"❌ Error clicking button '{text}': {e}")
            return False

    async def find_and_click_by_text(
        self,
        text: str,
        element_types: List[str] = ['button', 'a', 'div[ng-click]'],
        timeout: int = Timeouts.SELECTOR_WAIT,
        exact: bool = False
    ) -> bool:
        """
        テキストで要素を探してクリック（複数の要素タイプを試行）

        Args:
            text: 要素のテキスト
            element_types: 検索する要素タイプのリスト
            timeout: タイムアウト（ミリ秒）
            exact: 完全一致するか

        Returns:
            bool: クリック成功したらTrue
        """
        for element_type in element_types:
            try:
                elements = await self.page.query_selector_all(element_type)

                for element in elements:
                    element_text = await element.text_content()
                    if element_text:
                        element_text = element_text.strip()
                        if (exact and element_text == text) or (not exact and text in element_text):
                            self.logger.info(f"✓ Clicking {element_type}: {text}")
                            await element.click()
                            await self.page.wait_for_timeout(Timeouts.SHORT)
                            return True

            except Exception as e:
                self.logger.debug(f"Failed to find {element_type} with text '{text}': {e}")
                continue

        self.logger.warning(f"⚠️ Element not found with text: {text}")
        return False

    async def wait_for_element(
        self,
        selector: str,
        state: str = 'visible',
        timeout: int = Timeouts.SELECTOR_WAIT
    ) -> Optional[ElementHandle]:
        """
        要素の表示を待機

        Args:
            selector: CSSセレクタ
            state: 待機する状態 ('visible', 'attached', 'hidden', 'detached')
            timeout: タイムアウト（ミリ秒）

        Returns:
            ElementHandle: 見つかった要素（失敗時はNone）
        """
        try:
            element = await self.page.wait_for_selector(
                selector,
                state=state,
                timeout=timeout
            )
            return element
        except Exception as e:
            self.logger.warning(f"⚠️ Element not found: {selector} ({e})")
            return None

    async def safe_fill(
        self,
        selector: str,
        value: str,
        timeout: int = Timeouts.SELECTOR_WAIT
    ) -> bool:
        """
        安全な入力処理（要素の存在確認後に入力）

        Args:
            selector: CSSセレクタ
            value: 入力値
            timeout: タイムアウト（ミリ秒）

        Returns:
            bool: 入力成功したらTrue
        """
        try:
            element = await self.wait_for_element(selector, timeout=timeout)
            if element:
                await element.fill(value)
                self.logger.debug(f"✓ Filled {selector}: {value}")
                return True
            else:
                self.logger.warning(f"⚠️ Cannot fill {selector}: element not found")
                return False
        except Exception as e:
            self.logger.error(f"❌ Error filling {selector}: {e}")
            return False

    async def query_selector_with_text(
        self,
        selector: str,
        text: str,
        exact: bool = False
    ) -> Optional[ElementHandle]:
        """
        セレクタとテキストで要素を検索

        Args:
            selector: CSSセレクタ
            text: 検索するテキスト
            exact: 完全一致するか

        Returns:
            ElementHandle: 見つかった要素（失敗時はNone）
        """
        try:
            elements = await self.page.query_selector_all(selector)

            for element in elements:
                element_text = await element.text_content()
                if element_text:
                    element_text = element_text.strip()
                    if (exact and element_text == text) or (not exact and text in element_text):
                        return element

            return None

        except Exception as e:
            self.logger.error(f"❌ Error querying {selector} with text '{text}': {e}")
            return None

    async def click_element_with_retry(
        self,
        element: ElementHandle,
        retries: int = 3,
        delay: int = Timeouts.SHORT
    ) -> bool:
        """
        要素をリトライ付きでクリック（DOM陳腐化対策）

        Args:
            element: クリックする要素
            retries: リトライ回数
            delay: リトライ間の待機時間（ミリ秒）

        Returns:
            bool: クリック成功したらTrue
        """
        for attempt in range(retries):
            try:
                await element.click()
                await self.page.wait_for_timeout(Timeouts.SHORT)
                return True
            except Exception as e:
                if attempt < retries - 1:
                    self.logger.warning(f"⚠️ Click failed (attempt {attempt + 1}/{retries}), retrying...")
                    await self.page.wait_for_timeout(delay)
                else:
                    self.logger.error(f"❌ Click failed after {retries} attempts: {e}")
                    return False

        return False

    async def get_all_text_content(self, selector: str) -> List[str]:
        """
        セレクタに一致するすべての要素のテキストを取得

        Args:
            selector: CSSセレクタ

        Returns:
            List[str]: テキストのリスト
        """
        try:
            elements = await self.page.query_selector_all(selector)
            texts = []

            for element in elements:
                text = await element.text_content()
                if text:
                    texts.append(text.strip())

            return texts

        except Exception as e:
            self.logger.error(f"❌ Error getting text content for {selector}: {e}")
            return []
