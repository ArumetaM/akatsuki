#!/usr/bin/env python3
"""Slack通知機能"""
import os
import logging
from typing import Optional
from datetime import datetime
import aiohttp
import json

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Slack通知クラス"""
    
    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id
        self.base_url = "https://slack.com/api"
        
    async def send_message(self, text: str, blocks: Optional[list] = None) -> bool:
        """Slackにメッセージを送信"""
        try:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            
            data = {
                "channel": self.channel_id,
                "text": text
            }
            
            if blocks:
                data["blocks"] = blocks
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat.postMessage",
                    headers=headers,
                    json=data
                ) as response:
                    result = await response.json()
                    
                    if result.get("ok"):
                        logger.info(f"Slack message sent successfully")
                        return True
                    else:
                        logger.error(f"Slack API error: {result.get('error', 'Unknown error')}")
                        return False
                        
        except Exception as e:
            logger.error(f"Failed to send Slack message: {e}")
            return False
    
    async def send_deposit_notification(self, amount: int, balance_before: int, balance_after: int):
        """入金通知を送信"""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "💰 入金処理開始"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*入金額:*\n¥{amount:,}"
                    },
                    {
                        "type": "mrkdwn", 
                        "text": f"*残高（入金前）:*\n¥{balance_before:,}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*残高（入金後）:*\n¥{balance_after:,}"
                    }
                ]
            }
        ]
        
        text = f"入金処理: ¥{amount:,} (残高: ¥{balance_before:,} → ¥{balance_after:,})"
        await self.send_message(text, blocks)
    
    async def send_bet_notification(self, racecourse: str, race_number: int, 
                                  horse_number: int, horse_name: str, amount: int, status: str = "開始"):
        """投票通知を送信"""
        emoji = "🎯" if status == "開始" else "✅" if status == "完了" else "❌"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} 投票{status}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*競馬場:*\n{racecourse}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*レース:*\n{race_number}R"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*馬番:*\n{horse_number}番"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*馬名:*\n{horse_name}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*投票額:*\n¥{amount:,}"
                    }
                ]
            }
        ]
        
        text = f"{status}: {racecourse} {race_number}R {horse_number}番 {horse_name} ¥{amount:,}"
        await self.send_message(text, blocks)
    
    async def send_error_notification(self, error_type: str, error_message: str):
        """エラー通知を送信"""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "⚠️ エラー発生"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*エラータイプ:*\n{error_type}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*詳細:*\n{error_message}"
                    }
                ]
            }
        ]
        
        text = f"エラー: {error_type} - {error_message}"
        await self.send_message(text, blocks)
    
    async def send_summary_notification(self, total_bets: int, total_amount: int, final_balance: int):
        """実行完了サマリー通知を送信"""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📊 投票完了サマリー"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*総投票数:*\n{total_bets}件"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*総投票額:*\n¥{total_amount:,}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*最終残高:*\n¥{final_balance:,}"
                    }
                ]
            }
        ]
        
        text = f"投票完了: {total_bets}件 総額¥{total_amount:,} 残高¥{final_balance:,}"
        await self.send_message(text, blocks)
    
    async def send_session_start_notification(self):
        """セッション開始通知を送信"""
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚀 AKATSUKI BOT セッション開始"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*開始時刻:*\n{current_time}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": "*状態:*\n稼働中"
                    }
                ]
            }
        ]
        
        text = f"AKATSUKI BOT セッション開始 - {current_time}"
        await self.send_message(text, blocks)
    
    async def send_login_notification(self, success: bool, duration: float = None, error_message: str = None):
        """ログイン通知を送信"""
        if success:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🔐 ログイン成功"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": "*状態:*\n認証完了"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*処理時間:*\n{duration:.1f}秒" if duration else "*処理時間:*\n-"
                        }
                    ]
                }
            ]
            text = f"ログイン成功 ({duration:.1f}秒)" if duration else "ログイン成功"
        else:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "❌ ログイン失敗"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": "*状態:*\n認証エラー"
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*エラー:*\n{error_message}" if error_message else "*エラー:*\n不明"
                        }
                    ]
                }
            ]
            text = f"ログイン失敗: {error_message}" if error_message else "ログイン失敗"
        
        await self.send_message(text, blocks)
    
    async def send_balance_notification(self, balance: int, context: str = "確認"):
        """残高通知を送信"""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"💰 残高{context}"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*現在残高:*\n¥{balance:,}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*確認時刻:*\n{datetime.now().strftime('%H:%M:%S')}"
                    }
                ]
            }
        ]
        
        text = f"残高{context}: ¥{balance:,}"
        await self.send_message(text, blocks)
    
    async def send_deposit_start_notification(self, amount: int, current_balance: int):
        """入金開始通知を送信"""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🏧 入金処理開始"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*現在残高:*\n¥{current_balance:,}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*入金予定額:*\n¥{amount:,}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*入金後予定残高:*\n¥{current_balance + amount:,}"
                    }
                ]
            }
        ]
        
        text = f"入金開始: ¥{amount:,} (現在残高: ¥{current_balance:,})"
        await self.send_message(text, blocks)
    
    async def send_navigation_notification(self, page_name: str, success: bool = True):
        """ページ遷移通知を送信"""
        emoji = "✅" if success else "⚠️"
        
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} {page_name}へ{'遷移成功' if success else '遷移失敗'}"
                }
            }
        ]
        
        text = f"{page_name}へ{'遷移成功' if success else '遷移失敗'}"
        await self.send_message(text, blocks)