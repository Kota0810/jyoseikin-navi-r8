import streamlit as st
import json
import os
import io
import html
import re
import unicodedata
from datetime import date
from urllib.parse import quote
from google.genai import Client, types
from dotenv import load_dotenv
from db import (
    create_tables, SCHEMA_VERSION,
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

# =============================================================
# デザイントークン
# ライト固定 / アクセント＝ディープネイビー。色を足したくなったら
# まずこの表に定義してから使うこと（場当たり的な色指定を防ぐため）。
# =============================================================
INK        = "#12161D"   # 主要テキスト
INK_SUB    = "#39424F"   # 準主要テキスト（本文・ナビ）
INK_MUTED  = "#67717F"   # 補助テキスト（ここより薄い色は使わない）
LINE       = "#CBD2DC"   # 罫線（淡い下地の上でも沈まない濃さにする）
LINE_SOFT  = "#DFE4EA"   # 行間などの弱い罫線
SURFACE    = "#FFFFFF"   # カード（本文の面）
SURFACE_2  = "#F7F8FA"   # 沈んだ面
CANVAS     = "#F1F4F8"   # ページ背景（白いカードを浮かせるための下地）
NAVY       = "#1F3A5F"   # アクセント（主）
NAVY_DARK  = "#162943"   # アクセント（ホバー）
NAVY_TINT  = "#F0F3F8"   # アクセント（極薄・面）
NAVY_LINE  = "#DCE3ED"   # アクセント（薄い罫線）
DANGER     = "#B4232A"   # エラーのみ

# サイドバー（濃紺の面）。白い本文に対する縦のアンカーとして効かせる。
SB_BG      = "#16283F"   # サイドバー背景
SB_FG      = "#E9EDF3"   # サイドバー主要文字
SB_MUTED   = "#93A3B8"   # サイドバー補助文字

# 年度バッジ：R7 はグレー、R8 はネイビー
_BADGE_FG, _BADGE_BG = {
    "R7": (INK_MUTED, LINE_SOFT),
    "R8": (NAVY, NAVY_TINT),
}.get(APP_YEAR, (INK_MUTED, LINE_SOFT))

DISCLAIMER_TEXT = (
    "AIによる書類作成サポートです。情報の正確性については保証されておりません。"
    "必要に応じて最新の公式情報をご確認ください。"
)


def logo_svg(size: int = 28, on_dark: bool = False) -> str:
    """ブランドマーク（盾＋書面）。絵文字を使わずに同じ意味を担わせる。
    on_dark=True で濃紺サイドバー用の白抜きに切り替える。"""
    fill, stroke = (("#FFFFFF", SB_BG) if on_dark else (NAVY, "#FFFFFF"))
    return (
        f"<svg width='{size}' height='{size}' viewBox='0 0 32 32' fill='none' "
        f"xmlns='http://www.w3.org/2000/svg' aria-hidden='true' style='flex:0 0 auto;'>"
        f"<path d='M16 2.6 27 6.3v9.2c0 7.1-4.4 12.3-11 14.9-6.6-2.6-11-7.8-11-14.9V6.3L16 2.6Z' fill='{fill}'/>"
        f"<path d='M11.8 12.4h8.4M11.8 16.2h8.4M11.8 20h5' stroke='{stroke}' "
        f"stroke-width='1.7' stroke-linecap='round'/>"
        f"</svg>"
    )


def year_badge_html() -> str:
    return (
        f"<span class='year-badge' style='color:{_BADGE_FG};background:{_BADGE_BG};'>"
        f"{YEAR_LABEL}</span>"
    )


def render_brand_header(compact: bool = False) -> None:
    """メインエリア上部のブランドロックアップ（ロゴ／名称／年度／免責）。"""
    cls = "brand-head brand-head--compact" if compact else "brand-head"
    st.markdown(
        f"<div class='{cls}'>"
        f"<div class='brand-lockup'>{logo_svg(30 if not compact else 24)}"
        f"<span class='brand-name'>書類作成エージェント</span>"
        f"{year_badge_html()}</div>"
        f"<p class='brand-disclaimer'>{DISCLAIMER_TEXT}</p>"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_sidebar_brand() -> None:
    """サイドバー上部のブランドロックアップ（濃紺の面に載るので白抜きロゴ）。"""
    st.markdown(
        f"<div class='sb-brand'>{logo_svg(22, on_dark=True)}"
        f"<span class='sb-brand-name'>書類作成エージェント</span></div>"
        f"<div class='sb-year'><span class='year-badge sb-badge'>{YEAR_LABEL}</span></div>",
        unsafe_allow_html=True,
    )


def render_sidebar_user(name: str) -> None:
    """サイドバーのユーザー行（イニシャルのアバター＋表示名）。"""
    initial = html.escape((name or "?").strip()[:1])
    st.markdown(
        f"<div class='sb-user'><span class='sb-avatar'>{initial}</span>"
        f"<span>{html.escape(name)}</span></div>",
        unsafe_allow_html=True,
    )


def form_notice(domain_config: dict, form_name: str) -> str:
    """様式ごとの注意書きを domain_config.json から引く。

    法改正で様式が切り替わる時期をまたぐ場合など、どちらを使うべきかの
    判断が利用者に委ねられる場面で使う。設定が無いドメインでは何も出ない。
    """
    for entry in domain_config.get("form_notices", []):
        if form_name in entry.get("forms", []):
            return entry.get("text", "")
    return ""


def render_form_notice(text: str) -> None:
    if text:
        st.markdown(
            f"<div class='form-notice'>{html.escape(text)}</div>",
            unsafe_allow_html=True,
        )


def section_label(text: str) -> None:
    """サイドバー等の小見出し（全角大文字風のセクションラベル）。"""
    st.markdown(f"<div class='sb-section'>{html.escape(text)}</div>", unsafe_allow_html=True)


# =============================================================
# 様式名・項目名の整形
# 様式名はファイル名がそのまま入っているため、そのまま見出しにすると
# 「様式第9号の2_特別条項付き協定届.pdf」のように拡張子とアンダースコアが
# 露出して作りかけに見える。様式番号と名称に分けて扱う。
# =============================================================
_EXT_RE     = re.compile(r"\.(pdf|docx?|xlsx?|xlsm|csv)$", re.IGNORECASE)
# 「共通要領様式第２号」「継続様式第２号」など、様式番号の前に付く語も一緒に拾う
_FORM_NO_RE = re.compile(
    r"^((?:[一-龥]{0,6})?様式第[0-9０-９A-Za-zＡ-Ｚａ-ｚ一二三四五六七八九十\-‐－]+号(?:の[0-9０-９]+)*[①-⑳]*"
    r"|(?:[一-龥]{0,6})?様式[0-9０-９]+"
    r"|第[0-9０-９]+条)"
)


def split_form_title(form_name: str) -> tuple[str, str]:
    """様式名を (様式番号, 名称) に分解する。番号が無ければ ('', 名称)。"""
    base = _EXT_RE.sub("", form_name or "")
    base = base.replace("_", " ").replace("　", " ").strip()
    base = re.sub(r"\s{2,}", " ", base)
    m = _FORM_NO_RE.match(base)
    if m:
        return m.group(1), base[m.end():].strip(" ・-—")
    return "", base


def _norm_key(text: str) -> str:
    """比較用の正規化（記号・空白を落とす）。"""
    return re.sub(r"[\s　()（）・_\-.,、。]", "", text or "")


# 「①」「1」などの連番マーカー（グループ名の一部として扱う）
_MARKER_RE = re.compile(r"^[①-⑳0-9０-９IVXivx]+$")

# チップとして出してよい item_id は「条番号」のような構造マーカーだけ。
# 自由記述の item_id（例: 社労士事務所名称）はラベルとほぼ同義で、
# チップにすると1行目の幅を奪ってラベルが語の途中で折り返してしまう。
_CHIP_RE = re.compile(r"^(?:第[0-9０-９]+[条項号]|[①-⑳]+|[A-Za-z]?[0-9０-９]{1,3})$")


def build_item_rows(form_items: list) -> list:
    """右カラム用に (グループ名, 番号チップ, 表示ラベル, item, index) を組み立てる。

    item_id はドメインによって性質が違うため一律にチップ表示してはいけない。
      - 36協定       : item_id と label がほぼ同一 → チップは出さない
      - 両立支援等   : 'A_B' の A がグループ名、B が label と同義 → A を見出しに
      - 就業規則     : item_id='第1条' / label='(目的)' の補完関係 → チップとして出す

    グループは item_id の先頭セグメント（＋連番マーカー）だけを使う。
    全セグメントを使うと 1 項目ごとに見出しが立って逆に読みにくくなる。
    """
    draft = []
    for i, item in enumerate(form_items):
        item_id = (item.get("item_id") or f"項目{i + 1}").strip()
        label   = (item.get("label") or item_id).strip()
        # 就業規則の「(目的)」のように全体が括弧で囲まれている場合だけ外す。
        # 「所定労働時間 (1日) (任意)」の閉じ括弧まで削ってしまわないこと。
        if re.fullmatch(r"[（(][^（()）]*[)）]", label):
            label = label[1:-1].strip()
        parts   = [p for p in item_id.split("_") if p]

        head = parts[:1]
        if len(parts) > 2 and _MARKER_RE.match(parts[1]):
            head.append(parts[1])
        group = " ".join(head) if len(parts) > len(head) else ""
        mid   = parts[len(head):-1] if group else []
        last  = parts[-1] if parts else item_id

        # チップは構造マーカー（第1条 など）のときだけ。かつラベルと重複しないこと。
        n_last, n_label = _norm_key(last), _norm_key(label)
        chip = last if (_CHIP_RE.match(last) and n_last not in n_label) else ""

        # 中間セグメントはラベルに含まれていなければ前置きして文脈を戻す
        prefix = " ".join(p for p in mid if _norm_key(p) not in n_label)
        text   = f"{prefix} {label}".strip() if prefix else label

        draft.append([group, chip, text, item, i])

    # 1 項目しか属さないグループは見出しを立てない（見出しの粒度をそろえる）
    counts = {}
    for row in draft:
        counts[row[0]] = counts.get(row[0], 0) + 1
    for row in draft:
        if row[0] and counts[row[0]] < 2:
            row[0] = ""
    return [tuple(r) for r in draft]

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
    st.error("GEMINI_API_KEY が設定されていません。Streamlit Cloud の Settings → Secrets に設定してください。")
    st.stop()

client = Client(api_key=api_key)

# DB テーブルをアプリ起動時に1回だけ初期化（st.cache_resource でキャッシュ）
# スキーマ版数を引数に取るのは、db.py 側のスキーマを変えたときに確実に
# 再実行させるため。引数が無いと、この関数自身のコードが変わらない限り
# キャッシュが効き続け、プロセスを使い回したままデプロイされた場合に
# マイグレーションがスキップされる。
@st.cache_resource
def _init_db(schema_version: int):
    create_tables()

_init_db(SCHEMA_VERSION)


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


# 制度セレクトボックスの表示順（ここに書いた順に先頭へ並ぶ）
# 未記載のドメインはこの後ろにフォルダ名順で自動的に並ぶため、
# 新しいドメインを追加してもこのリストの変更は必須ではない。
DOMAIN_DISPLAY_ORDER = [
    "36協定",
    "就業規則",
    "労働条件通知書",
]


def _domain_sort_key(entry: str):
    """DOMAIN_DISPLAY_ORDER に載っているものを先に、残りはフォルダ名順で並べる"""
    if entry in DOMAIN_DISPLAY_ORDER:
        return (0, DOMAIN_DISPLAY_ORDER.index(entry), "")
    return (1, 0, entry)


def scan_domains() -> dict:
    """domains/ フォルダをスキャンして {domain_key: display_name} の辞書を返す。
    必須JSON (domain_config / form_structures / basic_rules / pdf_chunks) が揃っているドメインのみ返す。
    未完成のドメインを除外することで FileNotFoundError を防ぐ。
    並び順は DOMAIN_DISPLAY_ORDER に従う（未記載はフォルダ名順で後ろに続く）。"""
    base_dir    = os.path.dirname(os.path.abspath(__file__))
    domains_dir = os.path.join(base_dir, "domains")
    required = ("domain_config.json", "form_structures.json", "basic_rules.json", "pdf_chunks.json")
    result = {}
    if not os.path.isdir(domains_dir):
        return result
    for entry in sorted(os.listdir(domains_dir), key=_domain_sort_key):
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
    today = date.today()
    reiwa_year = today.year - 2018
    today_str = f"{today.year}年{today.month}月{today.day}日（令和{reiwa_year}年{today.month}月{today.day}日）"
    return f"""
あなたは『{selected_grant}』専門の助成金申請サポートAIです。
公式資料に基づいた専門的な知識をもとに、ユーザーが申請書を正確に完成できるよう伴走支援してください。
なお、あなたはAIであるため、専門家（社会保険労務士等）としての法的責任は負えません。回答はあくまでサポート情報としてご活用ください。

【本日の日付】{today_str}
※ 現在の年月日は必ず上記を基準にしてください。あなたの学習データ上の年ではなく、上記の日付が「今日」です。
※ 「今年」「来年」「今年度」等の相対表現や、日付の過去・未来の判定は、すべて上記の本日の日付を基準に解釈すること。

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
MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

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
        st.error(f"エラーが発生しました: {last_error}")
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
        from db import delete_expired_jti
        scheduler.add_job(lambda: delete_old_conversations(days=90), "cron", hour=2, minute=0)
        # 期限切れのSSOトークンIDを掃除する（保持し続ける意味がないため）
        scheduler.add_job(delete_expired_jti, "cron", hour=2, minute=10)
        scheduler.start()
    except Exception:
        pass  # スケジューラー起動失敗はアプリ動作に影響させない

_start_scheduler()

# =============================================================
# アイコン（線画SVGをCSSマスクとして流し込む）
# ボタンのラベルにはHTMLを入れられないため、st.button(key=...) が生成する
# .st-key-<key> クラスを足掛かりに ::before でアイコンを描画する。
# mask + currentColor なのでホバー時の文字色変化にアイコンも追従する。
# =============================================================
_ICON_PATHS = {
    "logout":   "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
    "admin":    "M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6",
    "back":     "M19 12H5M12 19l-7-7 7-7",
    "plus":     "M12 5v14M5 12h14",
    "image":    "M3 3h18v18H3zM8.5 10a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0ZM21 15l-5-5L5 21",
    "review":   "M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z",
    "document": "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8",
}


def _icon_mask(name: str) -> str:
    """線画SVGを data URI 化して mask-image に渡せる形にする。"""
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
        "stroke='%23000' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
        f"<path d='{_ICON_PATHS[name]}'/></svg>"
    )
    return "data:image/svg+xml," + quote(svg, safe="")


# ボタンの key → アイコン名
_BUTTON_ICONS = {
    "setup_logout":  "logout",
    "chat_logout":   "logout",
    "setup_admin":   "admin",
    "chat_admin":    "admin",
    "chat_back":     "back",
    "admin_back":    "back",
    "chat_template": "image",
    "chat_new":      "plus",
    "review_run":    "review",
}

_ICON_CSS = "".join(
    f'.st-key-{key} .stButton button p::before{{'
    f'content:"";display:inline-block;width:14px;height:14px;margin-right:8px;'
    f'vertical-align:-2px;background:currentColor;'
    f'-webkit-mask:url("{_icon_mask(icon)}") center/contain no-repeat;'
    f'mask:url("{_icon_mask(icon)}") center/contain no-repeat;}}'
    for key, icon in _BUTTON_ICONS.items()
)

# =============================================================
# グローバルCSS（全画面共通）
# 配色は :root のCSS変数だけがPython側トークンと接続している。
# 個別画面で色を直書きせず、必ず var(--…) を経由すること。
# =============================================================
_ROOT_VARS = f""":root{{
    --ink:{INK}; --ink-sub:{INK_SUB}; --ink-muted:{INK_MUTED};
    --line:{LINE}; --line-soft:{LINE_SOFT};
    --surface:{SURFACE}; --surface-2:{SURFACE_2}; --canvas:{CANVAS};
    --navy:{NAVY}; --navy-dark:{NAVY_DARK}; --navy-tint:{NAVY_TINT}; --navy-line:{NAVY_LINE};
    --danger:{DANGER};
    --sb-bg:{SB_BG}; --sb-fg:{SB_FG}; --sb-muted:{SB_MUTED};
    --sb-line:rgba(255,255,255,.11); --sb-hover:rgba(255,255,255,.08); --sb-active:rgba(255,255,255,.14);
    --radius:10px; --radius-sm:8px;
}}"""

_CSS_BODY = """
/* ───────── ベース ───────── */
html, body, .stApp, button, input, textarea, select,
[class*="st-"], [data-testid="stMarkdownContainer"] {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 "Hiragino Kaku Gothic ProN", "Hiragino Sans",
                 "Yu Gothic UI", "Yu Gothic", Meiryo, sans-serif;
    -webkit-font-smoothing: antialiased;
}
/* 背景に淡いトーンを敷き、コンテンツを白いカードとして浮かせる。
   全面が白のままだと要素の境界が消えて「のっぺり」する。 */
.stApp { background: var(--canvas); color: var(--ink); font-size: 15px; }
[data-testid="stMainBlockContainer"], .block-container {
    padding-top: 1.75rem; padding-bottom: 2.25rem; max-width: 1240px;
}
/* Streamlit は縦ブロック・要素コンテナに「実測した px 幅」を焼き込む。
   その結果 layout="wide" でも本文が中央寄せ既定の 704px のまま固定され、
   さらに CSS で親を狭めても子が焼き込み幅のままはみ出す。両方まとめて打ち消す。
   （これが無いと 1600px の画面でも本文は 704px で左右が大きく余る） */
section[data-testid="stMain"] [data-testid="stVerticalBlock"],
section[data-testid="stMain"] [data-testid="stElementContainer"] { width: 100% !important; }
/* 本文の既定値。:where() で詳細度を 0 にして、後段の自作クラス
   （.empty-sub / .ctx-meta / .field-note 等）が必ず勝つようにする。 */
:where([data-testid="stMarkdownContainer"]) :where(p) {
    font-size: .94rem; line-height: 1.8; color: var(--ink-sub);
}
/* ボタンのラベルも markdown コンテナなので、上の本文色を引き継ぐと
   ネイビーのボタン上に濃いグレーの文字が乗って読めなくなる。
   ラベルは必ずボタン自身の色を継ぐこと。 */
.stButton button p, .stFormSubmitButton button p,
.stButton button [data-testid="stMarkdownContainer"],
.stFormSubmitButton button [data-testid="stMarkdownContainer"] {
    color: inherit !important;
}
/* 自作見出しは Streamlit 既定の h1〜h3 スタイルより詳細度を上げる */
[data-testid="stMarkdownContainer"] h2.ctx-title,
[data-testid="stMarkdownContainer"] h2.admin-title {
    font-size: 1.45rem !important; font-weight: 700; color: var(--ink);
    margin: 0; padding: 0; line-height: 1.4; letter-spacing: .01em;
}
[data-testid="stMarkdownContainer"] h2.admin-title { margin-bottom: .2rem; }
hr, [data-testid="stDivider"] hr { border-color: var(--line); margin: 1.5rem 0; }
a { color: var(--navy); }
[data-testid="stCaptionContainer"] p { font-size: .8rem !important; color: var(--ink-muted) !important; }

/* ───────── Streamlit標準クロームを隠す ───────── */
@media (min-width: 769px) { header[data-testid="stHeader"] { display: none !important; } }
footer, #MainMenu,
[data-testid="stDecoration"], [data-testid="stDeployButton"],
[data-testid="stToolbarActions"],
.viewerBadge_container__1QSob, .styles_viewerBadge__CvC9N { display: none !important; }

/* ───────── ブランドロックアップ ───────── */
.brand-head { margin: 0 0 1.75rem; }
.brand-head--compact { margin-bottom: 1.25rem; }
.brand-lockup { display: flex; align-items: center; justify-content: center; gap: 10px; }
.brand-name {
    font-size: 1.5rem; font-weight: 700; color: var(--ink);
    letter-spacing: .01em; line-height: 1.2;
}
.brand-head--compact .brand-name { font-size: 1.15rem; }
.year-badge {
    display: inline-block; font-size: .74rem; font-weight: 600;
    padding: 3px 10px; border-radius: 999px; letter-spacing: .03em;
    white-space: nowrap; line-height: 1.5;
}
/* Streamlit 既定の [data-testid="stMarkdownContainer"] p より詳細度が低いので、
   p 要素として描画される自作クラスはサイズ・色を !important で確定させる。 */
.brand-disclaimer {
    margin: .8rem auto 0 !important; max-width: 44rem; text-align: center;
    color: var(--ink-muted) !important; font-size: .82rem !important; line-height: 1.75 !important;
}

/* ───────── 見出し・セクション ───────── */
.step-head { display: flex; align-items: baseline; gap: 10px; margin: 1.9rem 0 .7rem; }
.step-num {
    font-size: .74rem; font-weight: 700; color: var(--navy);
    background: var(--navy-tint); border-radius: 5px;
    padding: 2px 8px; letter-spacing: .06em;
}
.step-title { font-size: 1.08rem; font-weight: 700; color: var(--ink); }
.admin-title { font-size: 1.4rem; font-weight: 700; color: var(--ink); margin: 0 0 .2rem; }
.field-note {
    margin: .55rem 0 0 !important; color: var(--ink-muted) !important;
    font-size: .84rem !important; line-height: 1.7 !important;
}
/* 様式ごとの注意書き（適用時期など、利用者の判断が要る事項） */
.form-notice {
    margin: .7rem 0 0; padding: .8rem 1rem;
    background: var(--navy-tint); border: 1px solid var(--navy-line);
    border-left: 3px solid var(--navy); border-radius: 8px;
    color: var(--ink-sub); font-size: .84rem; line-height: 1.8;
}

/* ───────── ボタン ───────── */
.stButton button, .stFormSubmitButton button, [data-testid="stBaseButton-primary"] {
    border-radius: var(--radius-sm) !important;
    font-weight: 600 !important; font-size: .88rem !important;
    padding: .55rem 1.1rem !important; box-shadow: none !important;
    transition: background .12s ease, border-color .12s ease, color .12s ease;
}
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {
    background: var(--navy) !important; border: 1px solid var(--navy) !important; color: #fff !important;
}
.stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind="primary"]:hover {
    background: var(--navy-dark) !important; border-color: var(--navy-dark) !important; color: #fff !important;
}
.stButton button[kind="secondary"] {
    background: var(--surface) !important; border: 1px solid var(--line) !important; color: var(--ink) !important;
}
.stButton button[kind="secondary"]:hover {
    border-color: var(--navy) !important; color: var(--navy) !important;
}
.stButton button:focus-visible, .stFormSubmitButton button:focus-visible {
    outline: none !important; box-shadow: 0 0 0 3px rgba(31,58,95,.16) !important;
}

/* ───────── サイドバー（濃紺の面） ───────── */
[data-testid="stSidebar"] { background: var(--sb-bg); border-right: none; }
/* 折りたたみボタン用に確保された余白の上に、さらに padding を足さない。
   足すとロゴの上に大きな空白ができる。 */
[data-testid="stSidebarHeader"] { padding-top: .55rem !important; padding-bottom: 0 !important; }
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] { padding-top: .25rem !important; }
[data-testid="stSidebar"] hr { margin: 1.05rem 0; border-color: var(--sb-line); }
/* 折りたたみ矢印なども白系に寄せる */
[data-testid="stSidebar"] svg[data-testid="stIconMaterial"],
[data-testid="stSidebarCollapseButton"] svg { color: var(--sb-muted) !important; fill: var(--sb-muted) !important; }

.sb-brand { display: flex; align-items: center; gap: 9px; }
.sb-brand-name { font-size: .98rem; font-weight: 700; color: #FFFFFF; line-height: 1.3; }
.sb-year { margin: 7px 0 2px 31px; }
.sb-badge { background: rgba(255,255,255,.13) !important; color: #C7D5E6 !important; }
/* ユーザー行はイニシャルのアバターを添えて「行」として成立させる */
.sb-user {
    display: flex; align-items: center; gap: 9px; margin: 1.1rem 0 .6rem;
    color: var(--sb-fg); font-size: .88rem; font-weight: 600;
}
.sb-avatar {
    width: 26px; height: 26px; flex: 0 0 auto; border-radius: 7px;
    background: rgba(255,255,255,.15); color: #fff;
    display: inline-flex; align-items: center; justify-content: center;
    font-size: .78rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0;
}
.sb-section {
    margin: .4rem 0 .55rem; font-size: .72rem; font-weight: 700;
    color: var(--sb-muted); letter-spacing: .1em;
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p { color: var(--sb-muted) !important; }

/* サイドバーの通常ボタンは静かなナビゲーション行として扱う */
[data-testid="stSidebar"] .stButton button[kind="secondary"] {
    justify-content: flex-start !important; text-align: left !important;
    background: transparent !important; border-color: transparent !important;
    color: var(--sb-fg) !important; font-weight: 500 !important;
    font-size: .84rem !important; padding: .5rem .7rem !important; line-height: 1.55;
}
[data-testid="stSidebar"] .stButton button[kind="secondary"]:hover {
    background: var(--sb-hover) !important; border-color: transparent !important; color: #FFFFFF !important;
}
/* 表示中の会話（disabled）は現在地として強調する */
[data-testid="stSidebar"] .stButton button[kind="secondary"]:disabled {
    background: var(--sb-active) !important; color: #FFFFFF !important;
    border: none !important; border-left: 2px solid #FFFFFF !important;
    opacity: 1 !important; font-weight: 600 !important;
}
/* 濃紺の面ではネイビーのボタンが沈むので、CTA は白ボタンにする */
[data-testid="stSidebar"] .stButton button[kind="primary"] {
    background: #FFFFFF !important; color: var(--navy) !important; border-color: #FFFFFF !important;
}
[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {
    background: #DDE5EF !important; border-color: #DDE5EF !important; color: var(--navy) !important;
}
/* 長い会話タイトルは2行で打ち切る（3行に折り返して箱が並ぶのを防ぐ） */
[data-testid="stSidebar"] .stButton button p {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; text-overflow: ellipsis; margin: 0;
}
/* 会話履歴は行ごとに薄い罫線を入れる（無いと同じ塊が続いて平坦に見える） */
[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.stButton) + [data-testid="stElementContainer"]:has(.stButton) {
    border-top: 1px solid rgba(255,255,255,.06);
}

/* ───────── 入力系 ───────── */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div:first-child {
    border-radius: var(--radius-sm) !important;
    border: 1px solid var(--line) !important;
    background: var(--surface) !important;
}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:first-child:focus-within {
    border-color: var(--navy) !important; box-shadow: 0 0 0 3px rgba(31,58,95,.10) !important;
}
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
    color: var(--ink) !important; font-size: .875rem !important;
}
[data-testid="stWidgetLabel"] p { font-size: .8rem !important; font-weight: 600; color: var(--ink-sub); }

/* 管理画面のユーザー一覧の操作ボタン。列が狭いので、日本語ラベルが
   1文字ずつ縦に折り返らないよう、余白を詰めて折り返しを禁止する。 */
[class*="st-key-cno_btn_"] .stButton button,
[class*="st-key-pw_btn_"] .stButton button,
[class*="st-key-toggle_"] .stButton button,
[class*="st-key-del_"] .stButton button {
    padding: .5rem .35rem !important; font-size: .8rem !important; white-space: nowrap;
}

/* 初期設定のフォームは白いカードにして中央に置く。
   左寄せのままだと中央寄せのヘッダーと軸がずれて、右側が非対称な空白になる。 */
.st-key-setup_form {
    max-width: 720px; margin: 0 auto;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 14px; padding: 1.6rem 1.9rem 1.9rem;
}
.st-key-setup_form .step-head:first-of-type { margin-top: .2rem; }
/* STEP 間の区切り。項目が続くと境目が無くて平坦に見えるため。 */
.form-sep { height: 1px; background: var(--line); margin: 1.6rem 0 .2rem; }

/* SSO 失敗時の案内。濃紺のログイン画面に載るため白系で組む。 */
.sso-notice {
    max-width: 34rem; margin: 0 auto 1rem; padding: .9rem 1.1rem;
    background: rgba(255,255,255,.10); border: 1px solid rgba(255,255,255,.22);
    border-left: 3px solid #FFFFFF; border-radius: 8px;
    color: #FFFFFF; font-size: .86rem; line-height: 1.8;
}
.sso-back { max-width: 34rem; margin: 0 auto 1.2rem; text-align: center; }
.sso-back a {
    display: inline-block; padding: .55rem 1.4rem; border-radius: 8px;
    background: #FFFFFF; color: var(--navy) !important;
    font-size: .86rem; font-weight: 600; text-decoration: none;
}
.sso-back a:hover { background: #DDE5EF; }
.sso-hint {
    max-width: 34rem; margin: 0 auto 1.2rem !important; text-align: center;
    color: #C7D5E6 !important; font-size: .84rem !important; line-height: 1.8 !important;
}

/* ログインフォームをカードとして見せる */
[data-testid="stForm"] {
    border: 1px solid var(--line) !important; border-radius: 14px !important;
    padding: 1.75rem !important; background: var(--surface) !important;
}

/* ───────── チャット ───────── */
[data-testid="stChatMessageAvatarUser"], [data-testid="stChatMessageAvatarAssistant"] { display: none !important; }
[data-testid="stChatMessage"] {
    flex-direction: column; align-items: stretch; gap: 0;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 12px; padding: .95rem 1.15rem; margin-bottom: .7rem;
}
[data-testid="stChatMessage"]::before {
    font-size: .68rem; font-weight: 700; letter-spacing: .1em;
    color: var(--ink-muted); margin-bottom: .45rem;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"])::before { content: "エージェント"; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: var(--navy-tint); border-color: var(--navy-line);
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])::before {
    content: "あなた"; color: var(--navy);
}
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] { font-size: .9rem; line-height: 1.85; }

/* ───────── コンテキストバー（チャット上部） ───────── */
.ctx-bar {
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
    padding: 1.1rem 1.3rem 1rem; margin-bottom: 1rem;
}
.ctx-no {
    display: inline-block; margin-bottom: .5rem;
    font-size: .74rem; font-weight: 700; letter-spacing: .02em;
    color: var(--navy); background: var(--navy-tint);
    border: 1px solid var(--navy-line); border-radius: 5px; padding: 2px 8px;
}
.ctx-title {
    font-size: 1.45rem; font-weight: 700; color: var(--ink);
    margin: 0; line-height: 1.4; letter-spacing: .01em;
}
.ctx-meta {
    display: flex; align-items: center; flex-wrap: wrap; gap: 0 10px;
    margin-top: .6rem; font-size: .8rem; line-height: 1.7; color: var(--ink-muted);
}
.ctx-domain { color: var(--ink-sub); font-weight: 600; }
.ctx-sep { width: 1px; height: 11px; background: var(--line); display: inline-block; }

/* ───────── 会話ゼロ件の状態 ───────── */
.empty-state {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 12px; padding: 1.4rem 1.5rem; margin-bottom: .9rem;
}
.empty-title { font-size: 1rem; font-weight: 700; color: var(--ink); margin-bottom: .35rem; }
.empty-sub {
    margin: 0 !important; font-size: .87rem !important;
    line-height: 1.8 !important; color: var(--ink-sub) !important;
}
/* 例示の質問は控えめな候補として並べる */
.st-key-starter_0 .stButton button, .st-key-starter_1 .stButton button,
.st-key-starter_2 .stButton button {
    justify-content: flex-start !important; text-align: left !important;
    font-weight: 500 !important; font-size: .85rem !important;
    color: var(--ink-sub) !important; border-color: var(--line) !important;
    background: var(--surface) !important; padding: .6rem .85rem !important;
}
.st-key-starter_0 .stButton button:hover, .st-key-starter_1 .stButton button:hover,
.st-key-starter_2 .stButton button:hover {
    background: var(--navy-tint) !important; border-color: var(--navy-line) !important;
    color: var(--navy) !important;
}

/* ───────── コンポーザー（入力欄） ───────── */
.st-key-composer {
    border: 1px solid var(--line); border-radius: 12px;
    padding: .85rem .85rem .8rem; background: var(--surface); margin-top: 1.25rem;
}
.st-key-composer:focus-within { border-color: var(--navy-line); box-shadow: 0 0 0 3px rgba(31,58,95,.07); }
/* 枠は外側のコンポーザーが持つので、内側のテキストエリアからは外す */
.st-key-composer [data-baseweb="textarea"] {
    border-color: transparent !important; box-shadow: none !important;
}
.st-key-composer [data-testid="stTextArea"] textarea { font-size: .92rem !important; line-height: 1.75; }

/* ───────── 右カラム（記入項目） ───────── */
[data-testid="stColumn"]:has(.right-col-header) > div:first-child {
    position: sticky; top: 20px; max-height: calc(100vh - 44px);
    overflow-y: auto; padding: 1.05rem 1rem 1.2rem;
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
}
/* ヘッダーと一覧の間に区切りを入れる */
.right-col-sub + div, .right-col-header + .right-col-sub { position: relative; }
/* 項目は行ごとに罫線で区切る（無いと同じ塊が続いて平坦に見える） */
[data-testid="stColumn"]:has(.right-col-header) [data-testid="stElementContainer"]:has(.stButton)
+ [data-testid="stElementContainer"]:has(.stButton) {
    border-top: 1px solid var(--line-soft);
}
.right-col-header {
    font-size: .82rem; font-weight: 700; color: var(--ink); margin: 0 0 .25rem;
}
.right-col-sub {
    font-size: .78rem !important; color: var(--ink-muted) !important;
    margin: 0 0 .9rem !important; line-height: 1.65 !important;
}
/* item_id の接頭辞から起こしたグループ見出し */
.item-group {
    font-size: .7rem; font-weight: 700; letter-spacing: .06em; color: var(--ink-muted);
    margin: 1rem 0 .35rem; padding-bottom: .3rem; border-bottom: 1px solid var(--line);
}
[data-testid="stColumn"]:has(.right-col-header) .stButton button[kind="secondary"] {
    justify-content: flex-start !important; text-align: left !important;
    font-weight: 500 !important; font-size: .82rem !important;
    padding: .45rem .6rem !important; line-height: 1.6;
    background: transparent !important; border-color: transparent !important;
    color: var(--ink-sub) !important;
}
[data-testid="stColumn"]:has(.right-col-header) .stButton button[kind="secondary"]:hover {
    border-color: var(--navy-line) !important; background: var(--surface) !important;
    color: var(--navy) !important;
}
/* 条番号など、ラベルを補完する item_id だけチップとして描画する */
[data-testid="stColumn"]:has(.right-col-header) .stButton button code {
    background: var(--surface); color: var(--ink-muted); border: 1px solid var(--line);
    border-radius: 4px; padding: 0 5px; font-size: .7rem; font-weight: 600;
    margin-right: 6px; white-space: nowrap;
}
/* 3行までは折り返して見せる（1行省略だと語の途中で切れて読めない）。
   ラベルが文章そのものの項目もあるため、それ以上は打ち切る。 */
[data-testid="stColumn"]:has(.right-col-header) .stButton button {
    min-height: 42px; align-items: center;
}
[data-testid="stColumn"]:has(.right-col-header) .stButton button p {
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    white-space: normal; overflow: hidden; margin: 0;
    /* word-break:break-word は日本語で語中改行を誘発するので使わない。
       長い英数字だけ折り返せれば十分。 */
    word-break: normal; overflow-wrap: break-word; line-break: strict;
}

/* ───────── 面（expander / alert） ───────── */
[data-testid="stExpander"] {
    border: 1px solid var(--line) !important; border-radius: var(--radius) !important;
    background: var(--surface) !important; box-shadow: none !important;
}
[data-testid="stExpander"] summary { font-size: .86rem !important; font-weight: 600 !important; color: var(--ink-sub); }
[data-testid="stExpander"] summary:hover { color: var(--navy); }
/* 濃紺サイドバー上の添削モードパネル。
   summary に色を指定しても中の p が本文色を持っていて濃紺に埋もれるため、
   p と markdown コンテナまで明示的に継がせること。 */
[data-testid="stSidebar"] [data-testid="stExpander"] {
    background: rgba(255,255,255,.08) !important;
    border-color: rgba(255,255,255,.20) !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary,
[data-testid="stSidebar"] [data-testid="stExpander"] summary p,
[data-testid="stSidebar"] [data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] {
    color: #FFFFFF !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary svg { fill: #FFFFFF !important; color: #FFFFFF !important; }
[data-testid="stSidebar"] [data-testid="stExpander"]:hover { background: rgba(255,255,255,.12) !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background: rgba(255,255,255,.05) !important; border-color: var(--sb-line) !important;
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * { color: var(--sb-muted) !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
    background: transparent !important; border-color: var(--sb-line) !important; color: var(--sb-fg) !important;
}

[data-testid="stAlertContainer"] {
    border-radius: var(--radius) !important; border: 1px solid var(--line) !important;
    box-shadow: none !important; font-size: .82rem;
}
[data-testid="stFileUploaderDropzone"] {
    background: var(--surface-2) !important; border: 1px dashed var(--line) !important;
    border-radius: var(--radius-sm) !important;
}
"""

st.markdown(
    "<style>" + _ROOT_VARS + _CSS_BODY + _ICON_CSS + "</style>",
    unsafe_allow_html=True,
)

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
    "pending_prompt":      "",
    "input_key":           0,
    "last_error":          "",
    "sso_error":           "",
    "sso_return_url":      "",
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# =============================================================
# SSO（他システムからの署名付きトークンによるログイン）
# =============================================================
# 未ログインのときだけ処理する。ログイン成立後は再実行されても
# ここを通らないため、同じトークンが二重に処理されることはない。
if not st.session_state.authenticated:
    _sso_token = st.query_params.get("t")
    if _sso_token:
        import sso as _sso

        _sso_user, _sso_err, _sso_ret = _sso.authenticate(_sso_token)
        if _sso_user:
            st.session_state.authenticated = True
            st.session_state.user_id      = _sso_user["id"]
            st.session_state.display_name = _sso_user["display_name"]
            st.session_state.is_admin     = bool(_sso_user["is_admin"])
            st.session_state.app_state    = "setup"
            st.session_state.sso_error    = ""
        else:
            st.session_state.sso_error = _sso_err
            st.session_state.sso_return_url = _sso_ret
        # トークンをURLから消す。ブラウザ履歴や再読み込みで再送されないようにする。
        st.query_params.clear()
        st.rerun()


# =============================================================
# ログイン画面
# =============================================================
if st.session_state.app_state == "login":
    # ログインだけは濃紺を全面に敷き、白いカードを縦中央に置く。
    # 淡い下地のまま狭いカードを上寄せにすると、周囲の空白が「余り」に見える。
    st.markdown(
        """
        <style>
        .stApp { background: var(--sb-bg) !important; }
        [data-testid="stMainBlockContainer"] {
            min-height: 100vh; display: flex; flex-direction: column; justify-content: center;
            padding-top: 1.5rem !important; padding-bottom: 1.5rem !important;
        }
        .brand-name { color: #FFFFFF !important; }
        .brand-disclaimer { color: #A9B6C7 !important; }
        .year-badge { background: rgba(255,255,255,.13) !important; color: #C7D5E6 !important; }
        [data-testid="stForm"] { box-shadow: 0 18px 48px rgba(0,0,0,.22) !important; border-color: transparent !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    render_brand_header()

    # ── SSO が失敗したときの案内 ──
    # 期限切れは、アプリがスリープから復帰するのに時間がかかった場合に起きる。
    # そのときは発行側へ戻せば新しいトークンが発行され、
    # 起動済みのアプリに対して今度は成功する。
    if st.session_state.sso_error:
        import sso as _sso

        _msgs = {
            _sso.E_EXPIRED: (
                "アプリの起動に時間がかかったため、ログイン用の有効期限が切れました。"
                "お手数ですが、もう一度お試しください。"
            ),
            _sso.E_REPLAYED: (
                "このログイン用リンクは既に使用済みです。"
                "お手数ですが、もう一度お試しください。"
            ),
            _sso.E_NO_ACCOUNT: (
                "書類作成AIエージェントは、ご契約いただいた企業様向けのサービスです。"
                "ご利用をご希望の場合は、担当者までお問い合わせください。"
            ),
            _sso.E_NOT_ALLOWED: (
                "このアカウントではご利用いただけません。担当者までお問い合わせください。"
            ),
            _sso.E_NOT_CONFIGURED: (
                "外部システムからのログインは現在ご利用いただけません。"
                "下記のIDとパスワードでログインしてください。"
            ),
        }
        _msg = _msgs.get(
            st.session_state.sso_error,
            "ログインできませんでした。下記のIDとパスワードでログインしてください。",
        )
        st.markdown(
            f"<div class='sso-notice'>{html.escape(_msg)}</div>", unsafe_allow_html=True
        )

        # 再試行で解決する種類の失敗にだけ、やり直しの導線を出す。
        # 契約が無い場合は戻しても同じ結果になるため出さない。
        if st.session_state.sso_error in (_sso.E_EXPIRED, _sso.E_REPLAYED):
            _back = st.session_state.sso_return_url
            if _back:
                # 署名済みの戻り先が分かっている場合はボタンで戻す。
                st.markdown(
                    f"<div class='sso-back'><a href='{html.escape(_back)}' target='_self'>"
                    "もう一度ログインする</a></div>",
                    unsafe_allow_html=True,
                )
            else:
                # 戻り先が分からない場合は、元のタブへ戻ってもらう。
                # 発行側は新しいタブで開くため、元のタブは残っている。
                st.markdown(
                    "<p class='sso-hint'>元のタブに戻り、もう一度"
                    "「AIエージェント」を押してください。</p>",
                    unsafe_allow_html=True,
                )
        st.session_state.sso_error = ""
        st.session_state.sso_return_url = ""

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
        render_sidebar_brand()
        render_sidebar_user(st.session_state.display_name)
        if st.button("ログアウト", use_container_width=True, key="setup_logout"):
            logout()
            st.rerun()
        if st.session_state.is_admin:
            if st.button("管理画面へ", use_container_width=True, key="setup_admin"):
                st.session_state.app_state = "admin"
                st.rerun()
        st.divider()

        # 過去の会話一覧
        section_label("過去の会話")
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

    render_brand_header()
    st.divider()

    if not available_domains:
        st.error("domains/ フォルダにドメインが見つかりません。セットアップを確認してください。")
        st.stop()

    # フォームは本文幅いっぱいに伸ばさず、読みやすい幅に収める
    # （伸ばすとセレクトボックスとボタンが 1000px 超になって間延びする）
    with st.container(key="setup_form"):
        st.markdown(
            "<div class='step-head'><span class='step-num'>STEP 1</span>"
            "<span class='step-title'>制度を選択</span></div>",
            unsafe_allow_html=True,
        )
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

        st.markdown(
            "<div class='form-sep'></div>"
            "<div class='step-head'><span class='step-num'>STEP 2</span>"
            "<span class='step-title'>相談・添削したい様式を選択</span></div>",
            unsafe_allow_html=True,
        )
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
        # 選択した様式に注意書きがあれば、その場で提示する
        # （改正をまたぐ時期にどちらの様式を使うかは利用者の判断になるため）
        render_form_notice(form_notice(_sel_cfg, selected_form))
        st.markdown(
            "<p class='field-note'>様式を特定すると、AIの回答精度と添削の正確さが向上します。</p>",
            unsafe_allow_html=True,
        )
        st.write("")
        _start = st.button("相談を開始する", use_container_width=True, type="primary", key="setup_start")

    if _start:
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
        render_sidebar_brand()

        # ── ユーザー情報・ログアウト ──
        render_sidebar_user(st.session_state.display_name)
        if st.button("ログアウト", use_container_width=True, key="chat_logout"):
            logout()
            st.rerun()
        if st.session_state.is_admin:
            if st.button("管理画面へ", use_container_width=True, key="chat_admin"):
                st.session_state.app_state = "admin"
                st.rerun()

        st.divider()

        # ── 添削モード ──
        with st.expander("添削モード"):
            st.caption("申請書類をアップロードして添削します。")
            uploaded_file = st.file_uploader(
                "申請書類", type=["pdf", "docx", "xlsx", "xls", "xlsm", "csv"], label_visibility="collapsed",
            )
            if uploaded_file:
                st.caption(uploaded_file.name)
                if st.button("添削を実行", type="primary", use_container_width=True, key="review_run"):
                    with st.spinner("添削中..."):
                        st.session_state.review_result = review_document(
                            uploaded_file, st.session_state.selected_form,
                            form_map, rules_and_cases,
                        )
                    st.rerun()

        # ── 制度の選択画面に戻る（確認ダイアログ付き） ──
        if st.button("制度の選択画面に戻る", use_container_width=True, key="chat_back"):
            confirm_reset_dialog()

        # ── 様式を画像で表示する ──
        template_path = get_template_path(st.session_state.selected_form)
        if template_path:
            if st.button("様式を画像で表示", use_container_width=True, key="chat_template"):
                show_template_dialog(template_path)

        st.divider()

        # ── 過去の会話スレッド一覧 ──
        section_label("過去の会話")
        if st.button("新しい会話を始める", use_container_width=True, type="primary", key="chat_new"):
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
            # 表示中のスレッドは disabled 状態のスタイル（左のネイビー罫線）で示す
            _label = _conv["title"]
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

    # 右カラムの有無でレイアウトを切り替え。
    # 3:1 だと右カラムが 216px しか取れず項目名が折り返してガタつくため 2.4:1 にする。
    if form_items:
        col_main, col_right = st.columns([2.4, 1], gap="large")
    else:
        col_main = st.container()
        col_right = None

    # ── メインカラム ──────────────────────────────────────────
    with col_main:

        # ── コンテキストバー（様式番号／様式名／制度名・免責）──
        # 様式名はファイル名そのままなので、番号と名称に分けて整形する。
        # ※ 免責は AI の出力任せにせず常に表示する
        _form_no, _form_name = split_form_title(st.session_state.selected_form)
        st.markdown(
            "<div class='ctx-bar'>"
            + (f"<span class='ctx-no'>{html.escape(_form_no)}</span>" if _form_no else "")
            + f"<h2 class='ctx-title'>{html.escape(_form_name)}</h2>"
            + "<div class='ctx-meta'>"
            + f"<span class='ctx-domain'>{html.escape(st.session_state.selected_grant)}</span>"
            + f"<span class='ctx-sep'></span><span>{DISCLAIMER_TEXT}</span>"
            + "</div></div>",
            unsafe_allow_html=True,
        )

        # 添削レポート（あれば表示）
        if st.session_state.review_result:
            with st.expander("添削レポート", expanded=True):
                st.markdown(st.session_state.review_result)
                if st.button("チャット履歴に追加"):
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": f"【添削レポート】\n\n{st.session_state.review_result}",
                    })
                    st.session_state.review_result = ""
                    st.rerun()

        # ── 前回のエラー表示 ──────────────────────────────────
        if st.session_state.last_error:
            st.error(f"前回のエラー: {st.session_state.last_error}")
            st.session_state.last_error = ""

        # ── チャット履歴の表示 ────────────────────────────────
        if st.session_state.messages:
            for msg in st.session_state.messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
        elif st.session_state.pending_item is None and not st.session_state.pending_prompt:
            # 会話ゼロ件のときに白紙を見せない。最初の一手を提示する。
            st.markdown(
                "<div class='empty-state'>"
                "<div class='empty-title'>この様式について相談を始めましょう</div>"
                "<p class='empty-sub'>下の入力欄から自由に質問できます。"
                + ("右の記入項目を選ぶと、その欄の質問を自動で送信します。" if form_items else "")
                + "</p></div>",
                unsafe_allow_html=True,
            )
            _starters = [
                "この様式の記入手順を最初から教えてください",
                "提出先・提出期限を教えてください",
                "よくある不備・差し戻しの理由を教えてください",
            ]
            for _si, _s in enumerate(_starters):
                if st.button(_s, key=f"starter_{_si}", use_container_width=True):
                    st.session_state.pending_prompt = _s
                    st.rerun()

        # ── 項目ボタン／例示ボタンからの自動送信処理 ──────────
        _auto_prompt = ""
        if st.session_state.pending_item is not None:
            item = st.session_state.pending_item
            st.session_state.pending_item = None
            item_id = item.get("item_id", "")
            label   = item.get("label", "")
            _auto_prompt = f"{item_id}「{label}」について教えてください"
        elif st.session_state.pending_prompt:
            _auto_prompt = st.session_state.pending_prompt
            st.session_state.pending_prompt = ""

        if _auto_prompt:
            st.session_state.messages.append({"role": "user", "content": _auto_prompt})
            # DB にユーザーメッセージを保存
            conv_id = st.session_state.get("current_conv_id")
            if conv_id:
                add_message(conv_id, "user", _auto_prompt)
            with st.chat_message("user"):
                st.markdown(_auto_prompt)

            success = send_and_stream(_auto_prompt)
            if success:
                st.rerun()

        # ── 入力欄（コンポーザー）────────────────────────────
        with st.container(key="composer"):
            user_input = st.text_area(
                "入力欄",
                placeholder="例：離職率の計算方法は？ / ③(1)欄には何を書く？",
                height=112,
                label_visibility="collapsed",
                key=f"user_input_{st.session_state.input_key}",
            )
            _cl, _cr = st.columns([3, 1])
            with _cr:
                submit = st.button("送信", use_container_width=True, type="primary", key="composer_send")

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

    # ── 右カラム（記入項目・固定風） ─────────────────────────
    if col_right is not None:
        with col_right:
            # .right-col-header はスタイルのフックも兼ねる（グローバルCSS側で
            # [data-testid="stColumn"]:has(.right-col-header) として右カラムを特定する）
            st.markdown(
                "<div class='right-col-header'>記入項目</div>"
                "<p class='right-col-sub'>選ぶと、その欄についての質問を送信します。</p>",
                unsafe_allow_html=True,
            )

            _prev_group = None
            for _group, _chip, _label, item, i in build_item_rows(form_items):
                # グループが変わったところにだけ見出しを差し込む
                if _group != _prev_group:
                    if _group:
                        st.markdown(
                            f"<div class='item-group'>{html.escape(_group)}</div>",
                            unsafe_allow_html=True,
                        )
                    _prev_group = _group

                # チップは item_id がラベルを補完する場合だけ付く（就業規則の条番号など）
                # 切り詰めは CSS 側の2行クランプに任せる（ここで削ると語の途中で切れる）。
                # 極端に長いラベルだけ保険で丸める。
                _text = truncate_half_width(_label, 120)
                btn_label = f"`{_chip}`　{_text}" if _chip else _text

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
