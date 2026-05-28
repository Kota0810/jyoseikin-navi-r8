import streamlit as st
import json
import os
import io
import unicodedata
from datetime import date
from google.genai import Client, types
from dotenv import load_dotenv
from db import (
    create_tables,
    create_conversation, add_message, touch_conversation,
    update_conversation_title,
    get_conversations_by_user, get_messages_by_conversation, get_conversation,
)
from auth import login, logout, require_login, require_admin

# =============================================================
# アプリ年度識別（R7=令和7年度版 / R8=令和8年度版）
# Streamlit Cloud Secrets / .env の APP_YEAR で切替。未設定時は R7 扱い。
# =============================================================
APP_YEAR = os.getenv("APP_YEAR", "R7")
YEAR_LABEL = {"R7": "令和7年度版", "R8": "令和8年度版"}.get(APP_YEAR, APP_YEAR)
YEAR_COLOR = {"R7": "#1E88E5", "R8": "#E53935"}.get(APP_YEAR, "#666666")


def render_year_badge(size: str = "normal") -> None:
    """画面に年度バッジを表示。size: 'normal' | 'small'"""
    if size == "small":
        font, pad = "0.85em", "2px 10px"
    else:
        font, pad = "0.95em", "4px 12px"
    st.markdown(
        f"<div style='text-align:center;margin:4px 0;'>"
        f"<span style='background:{YEAR_COLOR};color:white;padding:{pad};"
        f"border-radius:12px;font-size:{font};font-weight:bold;'>{YEAR_LABEL}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

# =============================================================
# Streamlit ページ設定（最初のStreamlitコマンドとして呼び出す必要がある）
# =============================================================
st.set_page_config(
    page_title=f"書類作成AIエージェント（{YEAR_LABEL}）",
    layout="wide",
    page_icon="🛡️",
)

load_dotenv()
# ローカル: .env から取得 / Streamlit Cloud: st.secrets から取得
try:
    api_key = st.secrets.get("GEMINI_API_KEY", None) or os.getenv("GEMINI_API_KEY")
except Exception:
    api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    st.error("⚠️ GEMINI_API_KEY が設定されていません。Streamlit Cloud の Settings → Secrets に設定してください。")
    st.stop()

client = Client(api_key=api_key)

# DB テーブルをアプリ起動時に1回だけ初期化（st.cache_resource でキャッシュ）
@st.cache_resource
def _init_db():
    create_tables()

_init_db()


# =============================================================
# データロード
# =============================================================
def _domain_mtime(domain_key: str) -> str:
    """form_structures.json の更新時刻を返す（キャッシュ無効化用）"""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains", domain_key)
    try:
        return str(int(os.path.getmtime(os.path.join(base_dir, "form_structures.json"))))
    except Exception:
        return "0"


@st.cache_data
def load_knowledge(domain_key: str, mtime: str = ""):
    """ドメインの知識JSONを読み込む。mtime はキャッシュ無効化用（ファイル更新時自動リセット）"""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains", domain_key)
    with open(os.path.join(base_dir, "form_structures.json"), "r", encoding="utf-8") as f:
        form_map = json.load(f)
    with open(os.path.join(base_dir, "basic_rules.json"), "r", encoding="utf-8") as f:
        rules_and_cases = json.load(f)
    with open(os.path.join(base_dir, "pdf_chunks.json"), "r", encoding="utf-8") as f:
        pdf_chunks = json.load(f)
    with open(os.path.join(base_dir, "domain_config.json"), "r", encoding="utf-8") as f:
        domain_config = json.load(f)
    return form_map, rules_and_cases, pdf_chunks, domain_config


def scan_domains() -> dict:
    """domains/ フォルダをスキャンして {domain_key: display_name} の辞書を返す。
    必須JSON (domain_config / form_structures / basic_rules / pdf_chunks) が揃っているドメインのみ返す。
    未完成のドメインを除外することで FileNotFoundError を防ぐ。"""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    domains_dir = os.path.join(base_dir, "domains")
    required = ("domain_config.json", "form_structures.json", "basic_rules.json", "pdf_chunks.json")
    result = {}
    if not os.path.isdir(domains_dir):
        return result
    for entry in sorted(os.listdir(domains_dir)):
        domain_dir = os.path.join(domains_dir, entry)
        if not os.path.isdir(domain_dir):
            continue
        if not all(os.path.isfile(os.path.join(domain_dir, fn)) for fn in required):
            continue
        try:
            with open(os.path.join(domain_dir, "domain_config.json"), "r", encoding="utf-8") as f:
                config = json.load(f)
            result[entry] = config.get("display_name", entry)
        except Exception:
            pass  # 読み込みに失敗したドメインはスキップ
    return result


# =============================================================
# 半角換算で文字列を切り詰め（日本語＝2、英数字＝1）
# =============================================================
def truncate_half_width(text: str, max_hw: int = 120) -> str:
    count = 0
    for i, ch in enumerate(text):
        w = unicodedata.east_asian_width(ch)
        count += 2 if w in ("F", "W", "A") else 1
        if count > max_hw:
            return text[:i] + "..."
    return text


# =============================================================
# applies_to フィルタリング
# =============================================================
def get_stage_for_form(selected_form: str, cfg: dict) -> str:
    """選択様式 → 計画届 / 支給申請 / 全般 を返す。マッピング未定義なら空文字（＝全件使用）"""
    return cfg.get("form_to_stage", {}).get(selected_form, "")


def filter_rules_by_stage(rules: list, stage: str) -> list:
    """
    stage が空または '全般（様式を特定しない）' の場合は全件返す。
    stage が確定している場合は applies_to に stage または '全般' を含むルールのみ返す。
    applies_to フィールド自体が存在しない古いレコードは念のため全件に含める。
    """
    if not stage or stage == "全般（様式を特定しない）":
        return rules
    return [
        r for r in rules
        if not r.get("applies_to")                    # 旧フォーマット（フィールドなし）は通す
        or "全般" in r.get("applies_to", [])
        or stage in r.get("applies_to", [])
    ]


# =============================================================
# RAG: バイグラムによる関連チャンク抽出（日本語対応）
# =============================================================
def get_relevant_chunks(query: str, pdf_chunks: list, max_chunks: int = 3) -> str:
    scored = []
    for chunk in pdf_chunks:
        content = chunk.get("content", "")
        source  = chunk.get("source", "")
        score = sum(1 for i in range(len(query) - 1) if query[i:i+2] in content)
        if score > 0:
            scored.append((score, content, source))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [f"[出典: {src}]\n{cont}" for _, cont, src in scored[:max_chunks]]
    return "\n---\n".join(results)


# =============================================================
# システムプロンプト構築（5タイプ判別ロジック統合）
# =============================================================
def build_system_prompt(selected_grant, selected_form, form_map, rules_and_cases, relevant_chunks):
    form_data = form_map.get(selected_form, {})
    return f"""
あなたは『{selected_grant}』専門の助成金申請サポートAIです。
公式資料に基づいた専門的な知識をもとに、ユーザーが申請書を正確に完成できるよう伴走支援してください。
なお、あなたはAIであるため、専門家（社会保険労務士等）としての法的責任は負えません。回答はあくまでサポート情報としてご活用ください。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【最重要：対話の鉄則】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

■ 文脈最優先の原則（コンテキスト優先）
  - ユーザーの入力が短い（「わからない」「ない」「その予定はない」等）場合、
    または「その」「それ」「そこ」等の代名詞を含む場合は、
    必ず直前の「会話履歴」を参照して意図を解釈すること。
  - JSONデータ内のキーワードを検索して「どの項目ですか？」と聞き返すことは厳禁。

■ 能動的ヒアリング（逆質問）の原則
  - 「支給額は？」等の制度全般に関する質問には、まず基本情報を即答したうえで、
    正確な計算のために必要な情報をAI側から能動的に一問ずつヒアリングすること。

■ 5タイプ判別と回答スタイル
  ▶ タイプ1【チェック型】→ ルールのみ。事例引用厳禁。
  ▶ タイプ2【自由記述型】→ 参考事例を引用して記入見本を作成。
  ▶ タイプ3【数値・計算型】→ 計算式明示。ヒアリング後に具体的計算結果を提示。
  ▶ タイプ4【日付・期間型】→ 期限警告を最優先。
  ▶ タイプ5【選択・フラグ型】→ 定義の違いを解説し選択基準を提示。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【対象様式データ】（様式: {selected_form}）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(form_data, ensure_ascii=False, indent=2)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【基本ルール・数値定義（各種公式資料より抽出）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{json.dumps(rules_and_cases, ensure_ascii=False)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【参考事例・申請記入例（自由記述項目への回答時に優先活用）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{relevant_chunks if relevant_chunks else "（関連する参考事例なし）"}
"""


# =============================================================
# 添削用システムプロンプト構築
# =============================================================
def build_review_prompt(selected_form, form_map, rules_and_cases):
    form_items = form_map.get(selected_form, {}).get("items", [])
    today = date.today()
    reiwa_year = today.year - 2018
    today_str = f"{today.year}年{today.month}月{today.day}日（令和{reiwa_year}年{today.month}月{today.day}日）"
    return f"""
あなたは助成金申請書類の専門添削員（プロの社会保険労務士）です。
アップロードされた書類を【様式基準】と【ルール基準】に照らして厳密に添削してください。

【本日の日付】{today_str}
※ 日付の過去・未来の判定は必ず上記の本日の日付を基準にしてください。

【添削手順】
STEP1: 書類の各項目を識別し、【様式基準】のitem_idと照合する。
STEP2: 各記載内容が様式基準の instruction に沿っているか確認する。
STEP3: 数値・日付・計算値が【ルール基準】と矛盾していないか確認する。
STEP4: 結果を ⚠️要修正 / 💡改善提案 / ✅問題なし の3段階で報告。

【様式基準】（{selected_form}）
{json.dumps(form_items, ensure_ascii=False, indent=2)}

【ルール基準】（支給要領）
{json.dumps(rules_and_cases, ensure_ascii=False)}

添削レポートは日本語で、項目ごとに箇条書きでまとめてください。
"""


# =============================================================
# ファイル添削処理（PDF / DOCX / XLSX）
# =============================================================
def review_document(uploaded_file, selected_form, form_map, rules_and_cases):
    file_name      = uploaded_file.name.lower()
    stage          = get_stage_for_form(selected_form, domain_config)
    filtered_rules = filter_rules_by_stage(rules_and_cases, stage)
    review_sys     = build_review_prompt(selected_form, form_map, filtered_rules)

    if file_name.endswith(".pdf"):
        pdf_bytes = uploaded_file.read()
        pdf_instruction = """このPDF申請書類を添削してください。

【書類読み取りの重要ルール】
- ○（丸印）・チェック（✓）は、書類に明確に記入されているものだけを「選択済み」と判定してください。
- 複数の選択肢が並んでいる場合（例：策定・変更）、印のある選択肢のみを選択済みとし、印のない選択肢は「未選択」として扱ってください。
- 書式の枠線・印刷の丸記号（○で囲まれた番号など）は選択の〇とは区別してください。
- 印刷のかすれや判読が難しい場合は、「判読困難」と記載し、無理に判定しないでください。
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Content(role="user", parts=[
                types.Part(inline_data=types.Blob(mime_type="application/pdf", data=pdf_bytes)),
                types.Part(text=pdf_instruction),
            ])],
            config=types.GenerateContentConfig(system_instruction=review_sys),
        )
        return response.text

    elif file_name.endswith(".docx"):
        try:
            from docx import Document
            doc  = Document(io.BytesIO(uploaded_file.read()))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            return "❌ `pip install python-docx` が必要です。"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"以下のWord文書を添削してください：\n\n{text}",
            config=types.GenerateContentConfig(system_instruction=review_sys),
        )
        return response.text

    elif file_name.endswith((".xlsx", ".xlsm", ".xls")):
        try:
            import pandas as pd
            file_bytes = io.BytesIO(uploaded_file.read())
            xl = pd.ExcelFile(file_bytes)
            all_text = []
            for sn in xl.sheet_names:
                df = xl.parse(sn, header=None, dtype=str).fillna("")
                rows = []
                for _, row in df.iterrows():
                    line = " | ".join(str(v) for v in row if str(v).strip())
                    if line.strip():
                        rows.append(line)
                if rows:
                    all_text.append(f"【シート: {sn}】\n" + "\n".join(rows))
            excel_text = "\n\n".join(all_text)
        except Exception as e:
            return f"❌ Excelファイルの読み込みに失敗しました：{e}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"以下のExcelシートを添削してください：\n\n{excel_text}",
            config=types.GenerateContentConfig(system_instruction=review_sys),
        )
        return response.text

    elif file_name.endswith(".csv"):
        try:
            import pandas as pd
            # BOM付きUTF-8・Shift-JISどちらも試みる
            raw = uploaded_file.read()
            for enc in ("utf-8-sig", "shift_jis", "utf-8"):
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding=enc, dtype=str).fillna("")
                    break
                except Exception:
                    continue
            else:
                return "❌ CSVのエンコーディングを判定できませんでした。UTF-8またはShift-JISで保存してください。"
            rows = []
            for _, row in df.iterrows():
                line = " | ".join(str(v) for v in row if str(v).strip())
                if line.strip():
                    rows.append(line)
            csv_text = "\n".join(rows)
        except Exception as e:
            return f"❌ CSVファイルの読み込みに失敗しました：{e}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"以下のCSVデータを添削してください：\n\n{csv_text}",
            config=types.GenerateContentConfig(system_instruction=review_sys),
        )
        return response.text

    return "❌ 対応形式は PDF / Word(.docx) / Excel(.xlsx .xls .xlsm) / CSV(.csv) のみです。"


# =============================================================
# Gemini 用コンテンツ履歴の構築
# =============================================================
MAX_HISTORY_MESSAGES = 20  # 直近10往復（user + assistant 各10件）


def build_gemini_contents(messages: list, current_prompt: str) -> list:
    contents = []
    history = messages[:-1][-MAX_HISTORY_MESSAGES:]  # 直近10往復に制限
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=current_prompt)]))
    return contents


# =============================================================
# AI応答処理（共通関数化）
# =============================================================
MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

def send_and_stream(prompt: str) -> bool:
    """ユーザーの質問を処理してストリーミング応答を返す共通関数。成功時True"""
    stage           = get_stage_for_form(st.session_state.selected_form, domain_config)
    filtered_rules  = filter_rules_by_stage(rules_and_cases, stage)
    relevant_chunks = get_relevant_chunks(prompt, pdf_chunks)
    system_prompt = build_system_prompt(
        st.session_state.selected_grant,
        st.session_state.selected_form,
        form_map, filtered_rules, relevant_chunks,
    )
    gemini_contents = build_gemini_contents(st.session_state.messages, prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full = ""

        # モデルを順に試行（2.5-flash → 2.0-flash フォールバック）
        last_error = None
        for model_name in MODELS:
            full = ""
            try:
                for chunk in client.models.generate_content_stream(
                    model=model_name,
                    contents=gemini_contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt),
                ):
                    # Gemini 2.5 の思考チャンク（thought=True）をスキップ
                    if not getattr(chunk, "candidates", None):
                        continue
                    content = chunk.candidates[0].content
                    if not content or not content.parts:
                        continue
                    for part in content.parts:
                        if getattr(part, "thought", False):
                            continue  # 思考プロセスはユーザーに表示しない
                        if part.text:
                            full += part.text
                            placeholder.markdown(full + "▌")
                placeholder.markdown(full or "（回答を生成できませんでした）")
                if full:
                    st.session_state.messages.append({"role": "assistant", "content": full})
                    # DB に AI 応答を保存
                    conv_id = st.session_state.get("current_conv_id")
                    if conv_id:
                        add_message(conv_id, "assistant", full)
                        touch_conversation(conv_id)
                return True
            except Exception as e:
                last_error = e
                err_str = str(e)
                # レート制限エラーの場合は次のモデルで再試行
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    placeholder.markdown(f"⏳ {model_name} のレート制限に到達。別モデルで再試行中...")
                    continue
                # レート制限以外のエラーはそのまま表示
                break

        placeholder.empty()
        st.error(f"⚠️ エラーが発生しました: {last_error}")
        st.session_state.last_error = str(last_error)
        return False


# =============================================================
# 様式PDFプレビュー（モーダル表示）
# =============================================================
def get_template_path(form_key: str):
    """form_structuresのキーに対応するテンプレートPDFのパスを返す"""
    base_dir   = os.path.dirname(os.path.abspath(__file__))
    domain_key = st.session_state.get("selected_domain_key", "")
    pdf_path   = os.path.join(base_dir, "domains", domain_key, "templates", form_key)
    return pdf_path if os.path.isfile(pdf_path) else None


@st.dialog("確認")
def confirm_reset_dialog():
    """最初の画面に戻る前の確認ダイアログ"""
    st.warning("現在表示されている内容はすべて消去されます。最初の画面に戻りますか？")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("はい", use_container_width=True, type="primary"):
            st.session_state.app_state     = "setup"
            st.session_state.messages      = []
            st.session_state.review_result = ""
            st.session_state.pending_item  = None
            st.rerun()
    with c2:
        if st.button("いいえ", use_container_width=True):
            st.rerun()


@st.dialog("様式プレビュー", width="large")
def show_template_dialog(pdf_path: str):
    """PDFをページごとに画像変換してモーダル表示"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        st.error("PDF表示に必要なライブラリが読み込めませんでした。")
        return
    doc = fitz.open(pdf_path)
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=150)
        st.image(pix.tobytes("png"), caption=f"ページ {page_num + 1}", use_container_width=True)
    doc.close()



# 古い会話の自動削除スケジューラー（毎日午前2時、プロセス全体で1回だけ起動）
# ※ st.cache_resource を使うことでユーザーセッションをまたいで1インスタンスに限定する
@st.cache_resource
def _start_scheduler():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from db import delete_old_conversations
        scheduler = BackgroundScheduler()
        scheduler.add_job(lambda: delete_old_conversations(days=90), "cron", hour=2, minute=0)
        scheduler.start()
    except Exception:
        pass  # スケジューラー起動失敗はアプリ動作に影響させない

_start_scheduler()

# =============================================================
# グローバルCSS（全画面共通・Streamlit UI要素を非表示）
# =============================================================
st.markdown("""
<style>
    /* PC（769px以上）：ヘッダー全体を非表示 */
    @media (min-width: 769px) {
        header[data-testid="stHeader"]      { display: none !important; }
    }
    /* スマホ（768px以下）：ヘッダーは表示してハンバーガーを使えるようにする */

    /* フッター・ブランドバー（全デバイス共通） */
    footer                                  { display: none !important; }
    #MainMenu                               { display: none !important; }

    /* 右上ツールバー・右下バッジ（全デバイス共通） */
    /* ※ ハンバーガーボタン(stSidebarCollapsedControl)はここに含まれないため安全 */
    [data-testid="stDecoration"]            { display: none !important; }
    [data-testid="stDeployButton"]          { display: none !important; }
    [data-testid="stToolbarActions"]        { display: none !important; }
    .viewerBadge_container__1QSob          { display: none !important; }
    .styles_viewerBadge__CvC9N             { display: none !important; }
</style>
""", unsafe_allow_html=True)

available_domains = scan_domains()

# 選択済みドメインの知識をロード（未選択時は空で初期化）
_domain_key = st.session_state.get("selected_domain_key", "")
if _domain_key:
    form_map, rules_and_cases, pdf_chunks, domain_config = load_knowledge(
        _domain_key, mtime=_domain_mtime(_domain_key)
    )
else:
    form_map, rules_and_cases, pdf_chunks, domain_config = {}, [], [], {}

# ── セッション初期化 ──────────────────────────────────────────
_defaults = {
    # 認証
    "app_state":           "login",   # 初期は必ずログイン画面
    "authenticated":       False,
    "user_id":             None,
    "display_name":        "",
    "is_admin":            False,
    # 会話
    "current_conv_id":     None,
    "messages":            [],
    "selected_domain_key": "",
    "selected_grant":      "",
    "selected_form":       "",
    "review_result":       "",
    "pending_item":        None,
    "input_key":           0,
    "last_error":          "",
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# =============================================================
# ログイン画面
# =============================================================
if st.session_state.app_state == "login":
    st.markdown(
        "<h1 style='text-align:center;'>🛡️ 書類作成AIエージェント</h1>",
        unsafe_allow_html=True,
    )
    render_year_badge()
    st.markdown(
        "<p style='text-align:center;color:gray;'>"
        "AIによる書類作成サポートです。<br>"
        "情報の正確性については保証されておりません。必要に応じて最新の公式情報をご確認ください。"
        "</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    col_l, col_c, col_r = st.columns([1, 2, 1])
    with col_c:
        with st.form("login_form"):
            input_username = st.text_input("ログインID", placeholder="ユーザーID")
            input_password = st.text_input("パスワード", type="password")
            submitted = st.form_submit_button("ログイン", use_container_width=True, type="primary")

        if submitted:
            user = login(input_username, input_password)
            if user:
                st.session_state.authenticated  = True
                st.session_state.user_id        = user["id"]
                st.session_state.display_name   = user["display_name"]
                st.session_state.is_admin       = bool(user["is_admin"])
                st.session_state.app_state      = "setup"
                st.rerun()
            else:
                st.error("ログインIDまたはパスワードが正しくありません。")


# =============================================================
# 初期設定画面
# =============================================================
elif st.session_state.app_state == "setup":
    require_login()

    # ── サイドバー（ユーザー情報・管理画面・過去の会話） ──
    with st.sidebar:
        st.markdown("### 🛡️ 書類作成AIエージェント")
        render_year_badge(size="small")
        st.caption(f"👤 {st.session_state.display_name}")
        col_lo1, col_lo2 = st.columns([3, 2])
        with col_lo2:
            if st.button("ログアウト", use_container_width=True, key="setup_logout"):
                logout()
                st.rerun()
        if st.session_state.is_admin:
            if st.button("🔧 管理画面へ", use_container_width=True, key="setup_admin"):
                st.session_state.app_state = "admin"
                st.rerun()
        st.divider()

        # 過去の会話一覧
        st.markdown("**📂 過去の会話**")
        _conversations_setup = get_conversations_by_user(st.session_state.user_id, limit=20)
        if _conversations_setup:
            for _conv in _conversations_setup:
                _label = _conv["title"]
                _caption = _conv["updated_at"][:10] if _conv.get("updated_at") else ""
                if st.button(_label, key=f"setup_conv_{_conv['id']}", use_container_width=True, help=_caption):
                    _msgs = get_messages_by_conversation(_conv["id"])
                    st.session_state.messages        = [{"role": m["role"], "content": m["content"]} for m in _msgs]
                    st.session_state.current_conv_id = _conv["id"]
                    st.session_state.selected_domain_key = _conv["domain_key"]
                    st.session_state.selected_form   = _conv["form_name"]
                    _avail = scan_domains()
                    st.session_state.selected_grant  = _avail.get(_conv["domain_key"], _conv["domain_key"])
                    st.session_state.app_state       = "chat"
                    st.session_state.review_result   = ""
                    st.session_state.pending_item    = None
                    st.rerun()
        else:
            st.caption("まだ会話がありません。")

    st.markdown(
        "<h1 style='text-align:center;'>🛡️ 書類作成AIエージェント</h1>",
        unsafe_allow_html=True,
    )
    render_year_badge()
    st.markdown(
        "<p style='text-align:center;color:gray;'>"
        "AIによる書類作成サポートです。<br>"
        "情報の正確性については保証されておりません。必要に応じて最新の公式情報をご確認ください。"
        "</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    if not available_domains:
        st.error("⚠️ domains/ フォルダにドメインが見つかりません。セットアップを確認してください。")
        st.stop()

    st.subheader("1. 制度を選択")
    domain_keys   = list(available_domains.keys())
    domain_labels = list(available_domains.values())
    prev_domain   = st.session_state.get("selected_domain_key", "")
    default_idx   = domain_keys.index(prev_domain) if prev_domain in domain_keys else 0
    selected_idx  = st.selectbox(
        "制度",
        range(len(domain_keys)),
        format_func=lambda i: domain_labels[i],
        index=default_idx,
        label_visibility="collapsed",
    )
    _sel_domain_key   = domain_keys[selected_idx]
    _sel_domain_label = domain_labels[selected_idx]

    # 選択ドメインの様式一覧を取得（form_structures.json が更新されると自動的にキャッシュ再読込）
    _fm, _, _, _sel_cfg = load_knowledge(_sel_domain_key, mtime=_domain_mtime(_sel_domain_key))

    st.subheader("2. 相談・添削したい様式を選択")
    # domain_config.json の form_order があればその順に並べる（未指定のものは末尾に追加）
    _form_order   = _sel_cfg.get("form_order", [])
    _sorted_forms = sorted(
        _fm.keys(),
        key=lambda f: _form_order.index(f) if f in _form_order else len(_form_order),
    )
    form_options   = ["全般（様式を特定しない）"] + _sorted_forms
    prev_form      = st.session_state.get("selected_form", "")
    default_form_idx = form_options.index(prev_form) if prev_form in form_options else 0
    selected_form  = st.selectbox(
        "様式", form_options, index=default_form_idx, label_visibility="collapsed",
    )
    st.info("💡 様式を特定するとAIの回答精度と添削の正確さが向上します。", icon="ℹ️")

    if st.button("相談を開始する →", use_container_width=True, type="primary"):
        # タイトルを「制度名/様式名」形式で設定（様式未指定の場合は制度名のみ）
        _conv_title = (
            f"{_sel_domain_label}/{selected_form}"
            if selected_form != "全般（様式を特定しない）"
            else _sel_domain_label
        )
        # DB に新規スレッドを作成
        conv_id = create_conversation(
            st.session_state.user_id,
            _sel_domain_key,
            selected_form,
            title=_conv_title,
        )
        st.session_state.app_state           = "chat"
        st.session_state.selected_domain_key = _sel_domain_key
        st.session_state.selected_grant      = _sel_domain_label
        st.session_state.selected_form       = selected_form
        st.session_state.current_conv_id     = conv_id
        st.session_state.messages            = []
        st.session_state.review_result       = ""
        st.rerun()


# =============================================================
# チャット画面 & 添削画面
# =============================================================
elif st.session_state.app_state == "chat":
    require_login()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 左サイドバー（新規チャット・添削モード・様式表示）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with st.sidebar:
        st.markdown("### 🛡️ 書類作成AIエージェント")
        render_year_badge(size="small")

        # ── ユーザー情報・ログアウト ──
        st.caption(f"👤 {st.session_state.display_name}")
        col_lo1, col_lo2 = st.columns([3, 2])
        with col_lo2:
            if st.button("ログアウト", use_container_width=True):
                logout()
                st.rerun()
        if st.session_state.is_admin:
            if st.button("🔧 管理画面へ", use_container_width=True):
                st.session_state.app_state = "admin"
                st.rerun()

        st.divider()

        # ── 添削モード（黄色背景） ──
        st.markdown("""
        <style>
            [data-testid="stSidebar"] [data-testid="stExpander"]:has(summary:first-child) {
                background-color: #FFF3CD;
                border-radius: 8px;
                padding: 2px;
            }
        </style>
        """, unsafe_allow_html=True)

        with st.expander("📝 添削モード"):
            st.caption("申請書類をアップロードして添削します。")
            uploaded_file = st.file_uploader(
                "申請書類", type=["pdf", "docx", "xlsx", "xls", "xlsm", "csv"], label_visibility="collapsed",
            )
            if uploaded_file:
                st.success(f"📎 {uploaded_file.name}")
                if st.button("🔍 添削実行", type="primary", use_container_width=True):
                    with st.spinner("添削中..."):
                        st.session_state.review_result = review_document(
                            uploaded_file, st.session_state.selected_form,
                            form_map, rules_and_cases,
                        )
                    st.rerun()

        # ── 制度の選択画面に戻る（確認ダイアログ付き） ──
        if st.button("← 制度の選択画面に戻る", use_container_width=True):
            confirm_reset_dialog()

        # ── 様式を画像で表示する ──
        template_path = get_template_path(st.session_state.selected_form)
        if template_path:
            if st.button("📋 様式を画像で表示する", use_container_width=True):
                show_template_dialog(template_path)

        st.divider()

        # ── 過去の会話スレッド一覧 ──
        st.markdown("**📂 過去の会話**")
        if st.button("＋ 新しい会話を始める", use_container_width=True, type="primary"):
            st.session_state.app_state = "setup"
            st.session_state.current_conv_id = None
            st.session_state.messages = []
            st.session_state.review_result = ""
            st.session_state.pending_item = None
            st.rerun()

        _conversations = get_conversations_by_user(st.session_state.user_id, limit=20)
        _current_conv  = st.session_state.get("current_conv_id")
        for _conv in _conversations:
            _is_current = (_conv["id"] == _current_conv)
            _label = f"{'▶ ' if _is_current else ''}{_conv['title']}"
            _caption = _conv["updated_at"][:10] if _conv.get("updated_at") else ""
            if st.button(_label, key=f"conv_{_conv['id']}", use_container_width=True,
                         help=_caption, disabled=_is_current):
                # 過去スレッドを選択して復元
                _msgs = get_messages_by_conversation(_conv["id"])
                st.session_state.messages        = [{"role": m["role"], "content": m["content"]} for m in _msgs]
                st.session_state.current_conv_id = _conv["id"]
                st.session_state.selected_domain_key = _conv["domain_key"]
                st.session_state.selected_form   = _conv["form_name"]
                # domain_key から表示名を復元
                _avail = scan_domains()
                st.session_state.selected_grant  = _avail.get(_conv["domain_key"], _conv["domain_key"])
                st.session_state.app_state       = "chat"
                st.session_state.review_result   = ""
                st.session_state.pending_item    = None
                st.rerun()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # メインエリア（チャット） + 右カラム（項目一覧）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    form_items = form_map.get(st.session_state.selected_form, {}).get("items", [])

    # 右カラムの有無でレイアウトを切り替え
    if form_items:
        col_main, col_right = st.columns([3, 1])
    else:
        col_main = st.container()
        col_right = None

    # ── メインカラム ──────────────────────────────────────────
    with col_main:

        # カスタムCSS（右カラム独立スクロール・ユーザーメッセージ色・様式タイトル）
        st.markdown("""
        <style>
            /* ── 様式タイトル強調 ── */
            .form-title {
                font-size: 22px;
                font-weight: 700;
                color: #FF6B35;
                margin: 0 0 5px 0;
            }

            /* ── 右カラムだけ固定＋独立スクロール ── */
            [data-testid="stColumn"]:has(.right-col-header) > div:first-child {
                position: sticky;
                top: 60px;
                max-height: calc(100vh - 80px);
                overflow-y: auto;
            }

            /* ── ユーザー投稿の背景色（複数セレクタで確実に適用） ── */
            [data-testid="stChatMessage"]:has([data-testid*="user"]),
            [data-testid="stChatMessage"]:has([data-testid*="User"]),
            [data-testid="stChatMessage"][aria-label*="user"] {
                background-color: #d0d0d0 !important;
            }
        </style>
        """, unsafe_allow_html=True)

        # ヘッダー
        st.markdown(f"### 💬 {st.session_state.selected_grant}")
        st.markdown(
            f"<p class='form-title'>📋 {st.session_state.selected_form}</p>",
            unsafe_allow_html=True,
        )

        # 添削レポート（あれば表示）
        if st.session_state.review_result:
            with st.expander("📋 添削レポート", expanded=True):
                st.markdown(st.session_state.review_result)
                if st.button("チャット履歴に追加"):
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": f"【📋 添削レポート】\n\n{st.session_state.review_result}",
                    })
                    st.session_state.review_result = ""
                    st.rerun()

        st.divider()

        # ── 前回のエラー表示 ──────────────────────────────────
        if st.session_state.last_error:
            st.error(f"⚠️ 前回のエラー: {st.session_state.last_error}")
            st.session_state.last_error = ""

        # ── チャット履歴の表示 ────────────────────────────────
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        # ── 項目ボタンからの自動送信処理 ──────────────────────
        if st.session_state.pending_item is not None:
            item = st.session_state.pending_item
            st.session_state.pending_item = None

            item_id = item.get("item_id", "")
            label   = item.get("label", "")
            prompt  = f"{item_id}「{label}」について教えてください"

            st.session_state.messages.append({"role": "user", "content": prompt})
            # DB にユーザーメッセージを保存
            conv_id = st.session_state.get("current_conv_id")
            if conv_id:
                add_message(conv_id, "user", prompt)
            with st.chat_message("user"):
                st.markdown(prompt)

            success = send_and_stream(prompt)
            if success:
                st.rerun()

        # ── ユーザー入力欄（text_area: 2倍の高さ）────────────
        st.markdown("**自由に質問してください。　右側の一覧から選択することも可能です。**")
        user_input = st.text_area(
            "入力欄",
            placeholder="例：離職率の計算方法は？ / ③(1)欄には何を書く？",
            height=120,
            label_visibility="collapsed",
            key=f"user_input_{st.session_state.input_key}",
        )

        c1, c2 = st.columns([1, 4])
        with c1:
            submit = st.button("送信", use_container_width=True, type="primary")

        if submit and user_input.strip():
            prompt = user_input.strip()
            st.session_state.messages.append({"role": "user", "content": prompt})
            # DB にユーザーメッセージを保存
            conv_id = st.session_state.get("current_conv_id")
            if conv_id:
                add_message(conv_id, "user", prompt)
            with st.chat_message("user"):
                st.markdown(prompt)
            success = send_and_stream(prompt)
            if success:
                st.session_state.input_key += 1
                st.rerun()

    # ── 右カラム（項目一覧・固定風） ─────────────────────────
    if col_right is not None:
        with col_right:
            st.markdown("""
            <style>
                .right-col-header {
                    font-size: 16px;
                    font-weight: 600;
                    color: #667eea;
                    margin-bottom: 10px;
                }
            </style>
            """, unsafe_allow_html=True)

            st.markdown('<div class="right-col-header">❓ 何について聞きたいですか？</div>', unsafe_allow_html=True)

            for i, item in enumerate(form_items):
                item_id = item.get("item_id", f"項目{i+1}")
                label   = item.get("label", "")
                display = truncate_half_width(f"{item_id}: {label}", 120)
                btn_label = f"📌 {display}"

                if st.button(btn_label, key=f"ri-{i}", use_container_width=True):
                    st.session_state.pending_item = item
                    st.rerun()


# =============================================================
# 管理画面
# =============================================================
elif st.session_state.app_state == "admin":
    require_admin()
    from admin import render_admin_page
    render_admin_page()
