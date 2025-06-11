#!/usr/bin/env python3
"""
IPATサイトの構造定義（Seleniumコードから抽出）
このファイルは実際のサイト構造を定義するための参考資料
"""

# ログイン関連のセレクタ
LOGIN_SELECTORS = {
    # 第1段階 - INET-ID入力画面
    "inet_id_input": 'input[name="inetid"]',
    "inet_id_submit": '.button',  # classがbuttonのボタン
    
    # 第2段階 - 認証情報入力画面
    "user_id_input": 'input[name="i"]',  # 加入者番号
    "password_input": 'input[name="p"]',  # 暗証番号
    "pars_input": 'input[name="r"]',     # P-ARS番号
    "login_submit": '.buttonModern',      # classがbuttonModernのボタン
}

# メインページのセレクタ
MAIN_PAGE_SELECTORS = {
    # お知らせ確認（ポップアップ）
    "ok_button": 'button:has-text("OK")',
    
    # 残高表示
    "balance": 'td:has-text("円")',
    
    # メニューボタン
    "normal_vote_button": 'button:has-text("通常"):has-text("投票")',
    "deposit_button": 'button:has-text("入出金")',
}

# 投票画面のセレクタ
VOTE_SELECTORS = {
    # 競馬場選択（テキストで判定）
    "racecourse_button": 'button',  # テキストに競馬場名が含まれるボタンを探す
    
    # レース選択（XR形式）
    "race_button": 'button',  # テキストが"XR"形式のボタンを探す
    
    # 馬選択
    "horse_info": '.ng-isolate-scope',  # 馬情報が含まれるクラス
    "horse_label": 'label',  # 馬番号のラベル（インデックスで選択）
    
    # 購入設定
    "set_button": 'button:has-text("セット")',
    "finish_input_button": 'button:has-text("入力終了")',
    
    # 金額入力（input要素のインデックス）
    "amount_inputs": {
        "tickets": 9,    # 投票票数
        "bet_tickets": 10,  # 賭け票数
        "total_amount": 11,  # 合計金額
    },
    
    # 購入確定
    "purchase_button": 'button:has-text("購入する")',
    "confirm_ok_button": 'button:has-text("OK")',
}

# 入金画面のセレクタ
DEPOSIT_SELECTORS = {
    # 入金指示リンク
    "deposit_link": 'a:has-text("入金指示")',
    
    # 入金額入力
    "deposit_amount_input": 'input[name="NYUKIN"]',
    
    # 次へボタン
    "next_button": 'a:has-text("次へ")',
    
    # パスワード入力
    "deposit_password_input": 'input[name="PASS_WORD"]',
    
    # 実行ボタン
    "execute_button": 'a:has-text("実行")',
}

# ページ遷移のフロー
PAGE_FLOW = {
    "login": [
        "INET-ID入力",
        "次へボタンクリック",
        "加入者番号・暗証番号・P-ARS番号入力",
        "ログインボタンクリック",
        "お知らせ確認（必要に応じて）",
    ],
    "vote": [
        "通常投票ボタンクリック",
        "競馬場選択",
        "レース選択",
        "馬選択",
        "セットボタンクリック",
        "入力終了ボタンクリック",
        "金額入力",
        "購入するボタンクリック",
        "OKボタンクリック",
    ],
    "deposit": [
        "入出金ボタンクリック",
        "新しいウィンドウへ切り替え",
        "入金指示リンククリック",
        "入金額入力",
        "次へボタンクリック",
        "パスワード入力",
        "実行ボタンクリック",
        "アラート確認",
    ],
}

# タイミング設定（秒）
WAIT_TIMES = {
    "page_load": 4,
    "after_click": 2,
    "after_login": 4,
    "after_purchase": 5,
}

# 実際の値（本番では環境変数から取得）
SAMPLE_CREDENTIALS = {
    "inet_id": "GRFWW8PA",
    "user_id": "61008176",
    "password": "9262",
    "pars": "0519",
}