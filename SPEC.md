# 書類作成AIエージェント 仕様書

## 1. ツール概要

**名称：** 書類作成AIエージェント
**目的：** 助成金申請に必要な書類の作成を、AIとの対話を通じてサポートするWebアプリ
**対象ユーザー：** 助成金申請を行う事業主・社労士・担当者等
**公開方法：** Streamlit CloudでURL配布（不特定多数が利用可能）

### ボットのペルソナ
- 「○○専門の助成金申請サポートAI」として動作
- **社労士などの専門家としての法的責任は負わない**旨を明示（AIによるサポート情報という立場）
- Gemini 2.5 Flashの思考プロセスはユーザーに表示しない

---

## 2. 技術スタック

| 項目 | 内容 |
|------|------|
| フロントエンド/バックエンド | Streamlit |
| AIモデル | Gemini 2.5 Flash（google-genai SDK） |
| 言語 | Python |
| デプロイ | Streamlit Cloud（GitHub連携・自動デプロイ） |
| リポジトリ | GitHub: `ramudaccha1224/jyoseikin-navi`（公開リポジトリ） |

### ⚠️ 重要：Streamlitバージョン固定
```
streamlit==1.43.0
```
**Streamlit 1.55.0にはモバイルブラウザのWebSocket接続バグがあり、スマホで開けなくなる。必ずバージョンを固定すること。**

### requirements.txt
```
streamlit==1.43.0
google-genai
python-dotenv
python-docx
openpyxl
pymupdf
```

### 環境変数 / Secrets
- `GEMINI_API_KEY`：Gemini APIキー
  - ローカル：`.env` ファイル
  - Streamlit Cloud：Settings → Secrets に設定

---

## 3. ファイル構成

```
書類作成エージェント/
├── app.py                    # メインアプリ（唯一のエントリポイント）
├── requirements.txt
├── .env                      # ローカル用（gitignore済み）
└── domains/                  # ドメイン（制度）ごとのデータフォルダ
    ├── キャリアアップ/
    │   ├── domain_config.json      # 制度設定・form_to_stageマッピング
    │   ├── form_structures.json    # 様式ごとの項目定義
    │   ├── basic_rules.json        # ルール・数値定義・事例（applies_to付き）
    │   ├── pdf_chunks.json         # PDFをチャンク分割したRAGデータ
    │   ├── templates/              # 様式PDF（プレビュー用）
    │   └── knowledge/              # 元となるPDF群（ビルドスクリプト用）
    └── 雇用管理制度/
        ├── domain_config.json
        ├── form_structures.json
        ├── basic_rules.json
        ├── pdf_chunks.json
        ├── templates/
        └── knowledge/
```

### 新しいドメインの追加方法
`domains/` 以下に同じ構造のフォルダを作るだけで、アプリが自動検出する（`scan_domains()` 関数）。

---

## 4. 各JSONファイルの構造

### domain_config.json
```json
{
  "display_name": "人材確保等支援助成金（雇用管理制度・...）",
  "applies_to_options": ["計画届", "支給申請", "全般"],
  "form_to_stage": {
    "様式第a-1号_〇〇.pdf": "計画届",
    "様式第a-6号_〇〇.pdf": "支給申請"
  }
}
```

### form_structures.json
```json
{
  "様式名.pdf": {
    "items": [
      {
        "item_id": "①",
        "label": "項目名",
        "type": "text | number | date | select | checkbox",
        "instruction": "記載方法の説明"
      }
    ]
  }
}
```

### basic_rules.json
```json
[
  {
    "rule_id": "rule_001",
    "category": "支給額",
    "content": "ルールの内容",
    "applies_to": ["計画届", "全般"]  // ← 省略可（省略時は全件適用）
  }
]
```

### pdf_chunks.json
```json
[
  {
    "source": "ファイル名.pdf",
    "content": "PDFから抽出したテキスト（チャンク単位）"
  }
]
```

---

## 5. アプリの画面フロー

```
[セットアップ画面]
  ↓ 制度を選択（selectbox）
  ↓ 様式を選択（selectbox）または「全般（様式を特定しない）」
  ↓「相談を開始する」ボタン
[チャット画面]
  ├── 左サイドバー：添削モード / 最初の画面に戻る / 様式プレビュー
  ├── メインエリア：チャット履歴 + テキスト入力
  └── 右カラム：様式の項目一覧ボタン（様式選択時のみ）
```

---

## 6. 主要機能の詳細

### 6-1. チャット（send_and_stream）
- Gemini 2.5 Flashでストリーミング応答
- フォールバック：2.5-flash → 2.0-flash（レート制限時）
- 思考チャンク（`part.thought == True`）は表示しない
- `content` や `content.parts` が `None` のチャンクはスキップ（NoneType対策）

### 6-2. applies_toフィルタリング
選択した様式のステージ（計画届/支給申請）に基づき、`basic_rules.json` のルールを絞り込む。
- `get_stage_for_form(selected_form, domain_config)` → ステージを取得
- `filter_rules_by_stage(rules, stage)` → 該当ルールのみ抽出
- ステージ不明・全般選択時は全ルールを使用
- **ハードコーディングなし：** マッピングは `domain_config.json` の `form_to_stage` に記述

### 6-3. RAG（バイグラム検索）
`get_relevant_chunks(query, pdf_chunks, max_chunks=3)` で関連チャンクを取得し、システムプロンプトに注入。
- 日本語対応：バイグラム（2文字）一致スコアで上位3件を選択

### 6-4. 回答タイプの判別（5タイプ）
システムプロンプトで以下5タイプを判別して回答スタイルを切り替える：
1. チェック型 → ルールのみ、事例引用禁止
2. 自由記述型 → 参考事例を引用して記入見本を作成
3. 数値・計算型 → 計算式明示＋ヒアリング後に計算
4. 日付・期間型 → 期限警告を最優先
5. 選択・フラグ型 → 定義の違いを解説して選択基準を提示

### 6-5. 添削モード
PDF / Word(.docx) / Excel(.xlsx) をアップロードして添削。
- applies_toフィルタリングも適用

### 6-6. 様式プレビュー
サイドバーから選択中の様式PDFを画像として表示（PyMuPDF使用、dpi=150）。

---

## 7. CSS・UI仕様

### PC/スマホ分離CSS
```css
/* PCはヘッダー全体を非表示 */
@media (min-width: 769px) {
    header[data-testid="stHeader"] { display: none !important; }
}
/* スマホはヘッダー表示（ハンバーガーメニューのため） */

/* 全デバイス共通：フッター・バッジ等を非表示 */
footer { display: none !important; }
#MainMenu { display: none !important; }
[data-testid="stDecoration"] { display: none !important; }
[data-testid="stDeployButton"] { display: none !important; }
[data-testid="stToolbarActions"] { display: none !important; }
```

### ⚠️ CSS注意事項
- `[data-testid="stSidebarCollapsedControl"] { display: flex !important; }` を追加するとスマホのレイアウトが完全に破損する。**絶対に追加しないこと。**
- メディアクエリを使う場合は、グローバルルールとの干渉に注意。
- CSSは `st.set_page_config()` の直後に配置すること（全画面に適用するため）。

### Streamlit 1.43.0のハンバーガーボタンのdata-testid
`[data-testid="stExpandSidebarButton"]`（旧バージョンは `stSidebarCollapsedControl`）

---

## 8. ビルドスクリプト（knowledge → JSON生成）

`domains/[ドメイン]/knowledge/` フォルダ内のPDFから各JSONを生成するスクリプトが存在する。
- `build_rule_knowledge.py`：basic_rules.json を生成（Gemini APIを使用）
- `create_chunks.py`：pdf_chunks.json を生成
- `PAGE_BATCH_SIZE = 10`：JSON切り詰め防止のため10ページずつ処理

---

## 9. 未実装機能（実装予定）

### Googleスプレッドシートへの質問ログ記録
ユーザーの質問を記録する機能（実装保留中）。

**仕様：**
- 記録内容：日時、制度名、様式名、質問内容（各カラム）
- 保存先：Googleスプレッドシート

**実装方針：**
```python
def log_question(domain_name: str, form_name: str, question: str):
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(st.secrets["SPREADSHEET_ID"]).sheet1
        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, domain_name, form_name, question])
    except Exception:
        pass  # ログ失敗してもアプリは継続
```

**追加が必要なもの：**
- `requirements.txt` に `gspread` を追加
- Streamlit Cloud Secrets に `gcp_service_account`（JSON）と `SPREADSHEET_ID` を追加
- `send_and_stream()` 内でユーザー質問送信時に `log_question()` を呼び出す

---

## 10. 既知の問題・注意事項

| 問題 | 原因 | 対処 |
|------|------|------|
| スマホで開けない | Streamlit 1.55.0のバグ | `streamlit==1.43.0` に固定 |
| JSON切り詰め | build時にGeminiが大量ページを一度に処理 | `PAGE_BATCH_SIZE=10` で分割処理 |
| NoneType エラー | Gemini 2.5 Flashがcontent=NoneのチャンクをStream送信 | `if not content or not content.parts: continue` で対処済み |
| スマホでハンバーガー非表示 | `header`を全非表示にしていた | PC/スマホでメディアクエリ分離 |
