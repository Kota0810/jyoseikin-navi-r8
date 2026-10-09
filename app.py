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
    update_conversation_title, update_conversation_form,
    get_conversations_by_user, get_messages_by_conversation, get_conversation,
)
from auth import login, logout, require_login, require_admin
import sys
import psycopg2

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


# =============================================================
# 様式の呼び方・相談中の様式の切り替え・太字の表示（2026-10-07 追加）
# =============================================================
# 様式はファイル名で持っているが、AI にファイル名をそのまま渡すと、回答の中で
# 「様式第a-1号_別紙1_…_令和８年度４月８日以降.pdf」のように呼んでしまう。
# AI と画面の文中では、見出しと同じ「様式第a-1号 別紙1（…の概要票）」の形で呼ぶ。
_YEAR_SUFFIX_RE = re.compile(r"\s*[（(]?令和[0-9０-９]+年度.*$")
_ANNEX_HEAD_RE = re.compile(r"^[（(]?(別紙[0-9０-９]*)[）)]?\s*(.*)$")


def _form_display(form_name: str) -> str:
    """文中で使う様式の呼び方。ファイル名（拡張子・「令和○年度…以降」）は使わない。"""
    if not form_name or form_name == "全般（様式を特定しない）":
        return form_name
    no, name = split_form_title(form_name)
    name = _YEAR_SUFFIX_RE.sub("", name).strip()
    m = _ANNEX_HEAD_RE.match(name)
    if no and m:  # 「別紙N」は番号の側に寄せる
        no, name = f"{no} {m.group(1)}", m.group(2).strip()
    if no and name:
        return f"{no}（{name}）"
    return no or name


# 相談の途中で様式を切り替えた目印。会話の記録に残し、画面では区切りの行として出す。
# AI にもこの行が渡るので、どこから様式が変わったかを区別できる。
FORM_SWITCH_MARK = "【様式の切り替え】"
# 添削を実行した目印。添削の結果は、実行した位置（会話の流れの一番下）に残す。
REVIEW_MARK = "【添削の実行】"
REVIEW_REPORT_HEAD = "【添削レポート】"


def _is_switch_note(text: str) -> bool:
    return (text or "").startswith(FORM_SWITCH_MARK)


def _is_divider(text: str) -> bool:
    """会話の中で区切りの行として表示するもの（様式の切り替え・添削の実行）。"""
    return (text or "").startswith((FORM_SWITCH_MARK, REVIEW_MARK))


def _make_conv_title(form_name: str, grant: str, course_name: str) -> str:
    """過去の会話一覧の題名。左の欄は狭く途中で切れるので、様式名を先に出す（様式名／コース名）。
    コースの無い制度は制度名、様式を選んでいない相談は「全般」。"""
    head = "全般" if (not form_name or form_name == "全般（様式を特定しない）") else _form_display(form_name)
    return "／".join(p for p in (head, course_name or grant) if p)


def _switch_form(new_form: str) -> None:
    """相談はそのまま、様式だけを切り替える（AI に渡す項目・右の記入項目・添削の基準が変わる）。"""
    old = st.session_state.get("selected_form", "")
    if not new_form or new_form == old:
        return
    st.session_state.selected_form = new_form
    st.session_state.pending_item = None
    st.session_state.review_result = ""
    conv_id = st.session_state.get("current_conv_id")
    # まだ何も話していない相談なら、区切りの行は出さずに様式だけ変える
    if any(not _is_switch_note(m.get("content", "")) for m in st.session_state.get("messages", [])):
        note = f"{FORM_SWITCH_MARK}ここから様式を「{_form_display(new_form)}」に切り替えました"
        st.session_state.messages.append({"role": "assistant", "content": note})
        if conv_id:
            add_message(conv_id, "assistant", note)
    if conv_id:
        update_conversation_form(conv_id, new_form, _make_conv_title(
            new_form, st.session_state.get("selected_grant", ""), st.session_state.get("selected_course_name", "")))


def _on_form_switch(widget_key: str) -> None:
    _switch_form(st.session_state.get(widget_key, ""))


def _render_form_switcher(form_map: dict, domain_config: dict, where: str = "top") -> None:
    """「切り替える」ボタン。押すと、同じ制度・同じコースの様式と「全般」の一覧が出る。
    様式名の横（top）と、入力欄のすぐ上の帯（bottom）の2か所に置く。"""
    order = domain_config.get("form_order", [])
    forms = sorted(form_map.keys(), key=lambda f: order.index(f) if f in order else len(order))
    options = ["全般（様式を特定しない）"] + forms
    current = st.session_state.get("selected_form", "")
    if current not in options:
        options.insert(0, current)
    key = f"form_switch_{where}_{st.session_state.get('current_conv_id')}_{current}"
    label = "⇄ 様式を切り替える" if where == "top" else "⇄ 切り替える"
    with st.popover(label, use_container_width=True):
        st.caption("同じコースの様式から選び直します。この相談のまま続けられます。")
        st.radio(
            "様式", options, index=options.index(current),
            format_func=lambda f: _form_display(f) + ("（いまの様式）" if f == current else ""),
            key=key, label_visibility="collapsed", on_change=_on_form_switch, args=(key,),
        )
        st.caption("AIの案内・右の記入項目・添削の基準が、選んだ様式に切り替わります。")


# 太字の「**」は、日本語のかぎかっこ等の隣に置くと太字として扱われず、記号のまま出る。
# 表示するときだけ、「**」の内側に幅のない文字を挟んで太字として扱われるようにする
# （記録は AI の回答のまま）。HTML としては扱わないので、回答の中身が画面の部品になることはない。
_BOLD_PAIR_RE = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
# 太字だけの行（見出し代わり。「**① 氏名**」「**記入の考え方：**」など）の次の行は、
# 改行1つだとつながって表示される（「① 氏名助成金申請の…」）。空行を入れて段落を分ける。
_BOLD_LINE_RE = re.compile(r"(?m)^([ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?\*\*[^\n]+?\*\*[:：]?)[ \t]*\n(?=[ \t]*\S)")


def _md(text: str) -> str:
    text = _BOLD_LINE_RE.sub(lambda m: m.group(1) + "\n\n", text or "")
    return _BOLD_PAIR_RE.sub(lambda m: "**\u200b" + m.group(1) + "\u200b**", text)


def build_item_rows(form_items: list) -> list:
    """右カラム用に (グループ名, 番号チップ, 表示ラベル, item, index) を組み立てる。

    表示の名前は、質問文・AIに渡す資料と同じ見出し（_item_headings）を使う（2026-10-09 担当者決定：
    番号・「_」の置き換え・添え書きを3つでそろえる）。項目IDの内部の名前からはグループを作らない。
    グループの見出しにするのは、様式に印刷された呼び名（「様式第3号①」）と「No.1」のような
    行の番号だけで、そのときは「グループ名＋表示の名前」が見出しと同じになる。
    番号チップは使わない（番号は名前の一部として出す）。
    """
    heads = _item_headings(form_items)
    group_re = re.compile(rf"^({_FORM_SEG}|No\.?[0-9０-９]+)\s+(\S.*)$")
    rows = []
    for i, (item, head) in enumerate(zip(form_items, heads)):
        m = group_re.match(head)
        rows.append([m.group(1), "", m.group(2), item, i] if m else ["", "", head, item, i])
    # 1 項目しか属さないグループは見出しを立てず、見出しをそのまま出す
    counts = {}
    for row in rows:
        counts[row[0]] = counts.get(row[0], 0) + 1
    for row in rows:
        if row[0] and counts[row[0]] < 2:
            row[0], row[2] = "", heads[row[4]]
    return [tuple(r) for r in rows]

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
    """ドメインの知識JSONの更新時刻を返す（キャッシュ無効化用）。

    domain_config.json も見ること。コース区分を入れてから、この
    ファイルは表示名だけでなく「どの様式・どの資料をそのコースで
    使うか」を決めるようになった。form_structures.json だけを見て
    いると、コースを直しても画面に反映されない。
    """
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains", domain_key)
    stamps = []
    for name in ("form_structures.json", "domain_config.json",
                 "basic_rules.json", "pdf_chunks.json"):
        try:
            stamps.append(int(os.path.getmtime(os.path.join(base_dir, name))))
        except Exception:
            pass
    return str(max(stamps)) if stamps else "0"


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


def _restore_course(conv: dict) -> None:
    """保存済みの会話を開くとき、コースの選択も元に戻す。

    course 列が空（コース区分の無い制度、または列を足す前の古い会話）なら
    何もしない。その場合は制度全体が対象になり、これまでと同じ挙動になる。
    """
    key = (conv or {}).get("course") or ""
    st.session_state.selected_course = key
    if not key:
        st.session_state.selected_course_name = ""
        return
    try:
        _, _, _, cfg = load_knowledge(conv["domain_key"], mtime=_domain_mtime(conv["domain_key"]))
        st.session_state.selected_course_name = find_course(cfg, key).get("name", key)
    except Exception:
        st.session_state.selected_course_name = key


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
# コース（制度の下の区分）
# =============================================================
# 助成金によっては、ひとつの制度の下にコースが並び、様式も支給要領も別になる。
# 例: 人材開発支援助成金 ＝ 人材育成支援コース / 建設労働者技能実習コース / …
# 分けずに全部見せると、別コースの様式や要領が混ざって回答が濁るため、
# domain_config.json に courses があるドメインだけ、選択を1段深くする。
# courses が無いドメイン（業務改善助成金など）はこれまでどおり2段のまま。
def domain_courses(cfg: dict) -> list:
    return cfg.get("courses") or []


def find_course(cfg: dict, course_key: str) -> dict:
    return next((c for c in domain_courses(cfg) if c.get("key") == course_key), {})


def filter_forms_by_course(form_map: dict, cfg: dict, course_key: str) -> dict:
    """そのコースで使う様式だけに絞る。コース未指定・未設定なら素通し。"""
    course = find_course(cfg, course_key)
    want = set(course.get("forms") or [])
    if not want:
        return form_map
    return {k: v for k, v in form_map.items() if k in want}


def excluded_sources(cfg: dict, course_key: str) -> set:
    """このコースでは見せない資料（＝他コース専用の資料）の出典名の集合。

    「このコースの資料だけ残す」ではなく「他コースの資料だけ落とす」に
    している。どのコースにも割り当てていない資料——制度共通の手引き、
    共通Q&A、パンフレット、それに知識抽出のときに出典名が崩れた
    レコード——を取りこぼさないため。コース側に sources を1つも
    書いていない制度（様式だけコース分けする制度）では空集合を返し、
    知識は一切絞らない。
    """
    courses = domain_courses(cfg)
    if not any(c.get("sources") for c in courses):
        return set()
    # 選ばれているコースがこの制度のものでないときは何も絞らない。
    # ここで素通しにしておかないと「自分のぶんは無い・他コースのぶんは
    # 全部除外」となり、資料が丸ごと消える。
    if not find_course(cfg, course_key):
        return set()
    own = set(find_course(cfg, course_key).get("sources") or [])
    others = set()
    for c in courses:
        if c.get("key") != course_key:
            others |= set(c.get("sources") or [])
    return others - own


def drop_sources(records: list, exclude: set) -> list:
    """他コース専用の資料から作ったルール・チャンクを外す。"""
    if not exclude:
        return records
    return [r for r in records if r.get("source") not in exclude]


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
# 案内の範囲（2026-10-06 追加）
# =============================================================
# AI には選んだ様式1つ分の項目データしか渡していない。それなのに AI が自分から
# 別の様式の記入案内へ進み、実物にない欄（法人番号・所在地・生年月日）を作って
# 案内した。「全般」の相談でも欄を作っていた。資料にないことを補わない・選んだ
# 様式だけを扱う・元号の分からない日付は確認する、を指示に足す。
# 会話がすでに別の様式に流れていると指示だけでは引きずられるため、毎回の質問の
# 末尾にも短い前提を付ける（利用者には見えず、記録にも残さない）。
GENERAL_FORM = "全般（様式を特定しない）"
# 様式を選び直す場所（様式名の横と、入力欄のすぐ上の帯の2か所）。AI の案内文で使う。
SWITCH_GUIDE = ("様式名の横の「⇄ 様式を切り替える」か、入力欄のすぐ上の「⇄ 切り替える」から選び直してください。"
                "この相談のまま続けられます")
# ほかの様式の欄を尋ねられたときの答え方（ほかの様式のデータは渡していないので断定させない）
OTHER_FORM_RULE = f"""  - ほかの様式にどの欄があるか（例：「法人番号は様式第a-1号に書く」）は、ほかの様式のデータを渡していないので
    断定しないこと。尋ねられたら「ほかの様式にあるかどうかは、その様式に切り替えるとご案内できます。
    {SWITCH_GUIDE}」と答えること。"""


def _scope_rules(selected_form: str) -> str:
    disp = _form_display(selected_form)
    if selected_form == GENERAL_FORM:
        scope = f"""■ 様式を特定していない相談（今回はこれに当たる）
  - この相談では様式が選ばれていないため、欄ごとの記入案内はしないこと。
    制度の内容・要件・決まりの説明にとどめること。
  - 記入のしかたを聞かれたら、「記入のご相談は、{SWITCH_GUIDE}。
    様式を選ぶと、欄ごとにご案内できます」と案内すること。
    「資料に記載がない」とは言わないこと（様式を選べば、その様式の登録データで案内できるため）。
  - 会話の中に「{FORM_SWITCH_MARK}」で始まる行があるときは、それより前のやりとりは前の様式についてのもの。
    いまは様式が選ばれていないので、前の様式の欄の案内を続けないこと。
{OTHER_FORM_RULE}"""
    else:
        scope = f"""■ この相談で扱う様式は「{disp}」だけ
  - 記入欄の案内は、【対象様式データ】に載っている欄だけを対象にすること。
    載っていない欄を作って案内してはならない。
  - この様式の記入が終わっても、あなたから別の様式へ話を進めてはならない。
  - 会話の中に「{FORM_SWITCH_MARK}」で始まる行があるときは、それより前のやりとりは前の様式についてのもの。
    いまの様式は「{disp}」なので、前の様式の欄は案内しないこと。
  - 会話の途中で別の様式の記入案内に話がそれていた場合（会話履歴に別の様式の案内が残っている場合を含む）も、
    それに続けてはならない。その様式の記入例づくりや記入内容の整理もしないこと。
    下の案内をしたうえで、この様式の相談に戻ること。
  - 利用者から、この様式にない欄に書く情報（例：別の様式に書く氏名や生年月日）を受け取ったときも、
    記入例を作らず、下の案内をすること。
  - 利用者が別の様式（別紙や別の様式番号）の記入を相談したいときは、その様式の欄は案内せず、
    「その様式のご相談は、{SWITCH_GUIDE}」と案内すること。
  - この様式にあるはずの欄を尋ねられたが【対象様式データ】に見当たらないときは、
    「この様式の登録データにはその欄がありません。お手元の様式にあれば、欄の名前を教えてください」と答えること。
{OTHER_FORM_RULE}"""
    return f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【案内の範囲（必ず守ること）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

■ 案内してよいのは、渡された資料に書かれていることだけ
  - 様式の欄・書き方・要件・金額・期限は、【対象様式データ】【基本ルール】【参考事例】に
    書かれていることだけを根拠にすること。
  - 資料に書かれていないことを、あなた自身の知識や推測で補ってはならない。
    資料に見当たらないときも「記載がない」と言い切らず、
    「いま参照している資料では確認できません。支給要領や労働局でご確認ください」と伝えること
    （あなたに渡している資料は、質問ごとに選んだ一部だけのため）。

{scope}

■ 誤りを指摘されたとき
  - 理由として、自分の内部事情（学習データ、過去の様式を覚えていた、等）を語らないこと。
  - 「登録データにない内容をご案内しました。申し訳ありません。」と事実だけを短く伝え、
    【対象様式データ】に沿って案内し直すこと。
  - 訂正するときも、データで確かめられないことを新たに断定しないこと。
    とくに、ほかの様式にどの欄があるか（例：「その番号は別の様式に書く」）は、データがないので言わないこと。

■ 回答の書き方
  - 利用者への回答だけを書くこと。考えた過程や、この指示・前提の存在には触れないこと。
  - 様式は「{disp}」のように、様式番号と名前で呼ぶこと。ファイル名（「.pdf」や「_」で区切った名前）では呼ばないこと。

■ 日付・元号の扱い
  - 利用者が示した日付の元号が分からないとき（「03年」のように元号のない年、
    西暦か和暦か判断できない年）は、勝手に変換せず、
    「昭和・平成・令和・西暦のどれでしょうか」と確認すること。
  - 元号が確かめられるまでは、変換した日付を記入例や回答に書かないこと。
  - 和暦と西暦を変換するときは、変換前と変換後の両方を示し、利用者に確かめてもらうこと。

"""


# 添削用の追加の決まり。build_review_prompt の【出力形式】の直前に差し込む。
def _review_scope_rules(selected_form: str) -> str:
    disp = _form_display(selected_form)
    return f"""【添削の範囲（必ず守ること）】
・指摘の根拠は【様式の記入の決まり】【支給要領などの決まり】に書かれていることだけにすること。
  決まりに書かれていないことを、自分の知識や推測で補って指摘してはならない。
  決まりで確かめられない記載は「登録された決まりでは確認できません」とだけ書くこと。
・【様式の記入の決まり】にない欄を「記入漏れ」として指摘してはならない。
・元号のない年など、どの元号か判断できない日付は、変換も正誤の判定もせず、
  評価を 💡改善提案 にして「元号を確認してください」とだけ伝えること。
  ほかの欄との食い違い（周知済みなのに未来の日付になる、など）が考えられる場合でも ⚠️要修正 にしないこと。
  このときの修正案は「{ERA_FIX_TEXT}」の1文だけにすること。
  具体的な元号や日付の例（「令和8年…」「西暦2008年…」など）や、どの元号と解釈したらどうなるかの説明は書かないこと
  （出力形式の「修正案は書き換え後の文面を示す」よりも、こちらを優先する）。
・報告の中で、自分の内部事情（学習データ等）に触れないこと。
・報告では自己紹介をしないこと（「添削員として」「社会保険労務士として」などと書かない）。
  「様式基準」「ルール基準」「基準データ」などの内部の呼び名は使わず、
  「様式の記入の決まり」「支給要領などの決まり」と書くこと。
・様式は「{disp}」のように、様式番号と名前で呼ぶこと。ファイル名では呼ばないこと。

"""


# 元号の分からない日付に使う決まった修正案（_review_scope_rules で AI に指定している1文）。
ERA_FIX_TEXT = "元号（昭和・平成・令和）または西暦のどれかを確認し、付けて記入してください。"


# 元号が分からないことを理由にした項目か（理由の文）と、修正案に元号・西暦つきの日付の例があるか
_ERA_UNCLEAR_RE = re.compile(
    r"元号(が|の|は|を|か)?(不明|分から|わから|分かりませ|わかりませ|な[いくし]|記載が?な|記載されてい?な|書かれてい?な|付いてい?な|ついてい?な|表記が?な)"
    r"|元号[^。]{0,12}(判別でき|特定でき|判断でき|分から|わから|分かりませ|わかりませ|不明)")
_DATED_RE = re.compile(r"(令和|平成|昭和)\s*[0-9０-９元]+\s*年|(19|20)[0-9]{2}\s*年|西暦\s*[0-9]")


def _plain(s: str) -> str:
    return re.sub(r"[\s*「」『』\"]", "", s or "")


# 添削の結果の見た目をそろえるための目印。
# 「■ ① 事業所名 評価：… 理由：…」のように1行に詰まっていても、評価・理由・修正案の前で行を分ける
# （「その理由：」のような文中の語では分けないよう、前に空白か区切りがあるときだけ）。
_INLINE_LABEL_RE = re.compile(r"[ \t　｜|]+(?=\**\s*(?:評価|理由|修正案)\s*\**\s*[:：])")
_LABEL_LINE_RE = re.compile(r"^[\s>#\-・*]*?\**\s*(評価|理由|修正案)\s*\**\s*[:：]\s*\**\s*(.*?)\s*$")


def _split_packed_labels(text: str) -> str:
    """1行に詰まった「見出し 評価：… 理由：…」を、評価・理由・修正案の前で行に分ける。
    前に見出しや文があるときだけ分ける（「- 評価：」のような箇条書きの記号だけなら分けない）。"""
    out = []
    for line in text.split("\n"):
        while True:
            m = next((m for m in _INLINE_LABEL_RE.finditer(line)
                      if re.sub(r"[\s#*>\-・■□◆◇●○]", "", line[:m.start()])), None)
            if not m:
                break
            out.append(line[:m.start()])
            line = line[m.end():]
        out.append(line)
    return "\n".join(out)


def _heading_text(line: str) -> str:
    """見出しの行（「### ① 事業所名」「■ ① 事業所名」「**① 事業所名**」など）から、飾りを外した文字だけを取り出す。
    見出しらしくない行（長い文・表・区切り線など）なら空文字を返す。"""
    s = line.strip()
    if not s or s.startswith(("|", "---")) or s.endswith("。") or _LABEL_LINE_RE.match(s):
        return ""
    s = re.sub(r"^[#\s■□◆◇●○・\-]+", "", s).replace("**", "").strip()
    return s if 0 < len(s) <= 60 else ""


def _layout_review(lines: list) -> list:
    """欄ごとに「見出し」「評価」「理由」「修正案」が行を分けて出るようにそろえる。
    見た目は案A＋C（2026-10-08 担当者決定）：見出しは一段大きい見出し（##### → 左に色の帯。CSS の
    .rv-head 相当は h5 の設定）、「評価」「理由」「修正案」は太字にせず色で区別する（:blue[…]）。"""
    out = []
    for line in lines:
        m = _LABEL_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        key, rest = m.group(1), m.group(2)
        if rest.count("**") % 2 == 1:
            rest = rest.replace("**", "", 1).strip()
        if key == "評価":
            while out and not out[-1].strip():
                out.pop()
            if out and (h := _heading_text(out[-1])):
                out[-1] = f"##### {h}"
                if len(out) > 1 and out[-2].strip():
                    out.insert(len(out) - 1, "")
            out.append("")
        else:
            while out and not out[-1].strip():
                out.pop()
            if out:
                out[-1] = out[-1].rstrip() + "  "
        out.append(f":blue[{key}：] {rest}")
    return out


def _tidy_review(report: str) -> str:
    """添削の結果を、決まりどおりの形に整える（AI が指示を守りきれないことがあるため）。
    ・書き出しの1段落だけの前置き（「〜について添削しました」など）を外す。
    ・元号の分からない日付の項目で、修正案に元号つきの日付の例（「令和8年…」など）が
      書かれていたら、決まった1文（ERA_FIX_TEXT）に置き換える。
    ・修正案が決まった1文の項目は、評価を 💡改善提案 にそろえる
      （元号の分からない日付は確認をお願いするだけで、誤りとは扱わない）。
    ・欄ごとに、見出し・評価・理由・修正案が行を分けて出るようにそろえる（_layout_review）。"""
    text = report or ""
    head, sep, rest = text.lstrip().partition("\n\n")
    if (sep and "評価" not in head and "評価" in rest and len(head) <= 150
            and re.search(r"添削|報告", head) and head.rstrip().endswith("。")
            and not head.lstrip().startswith(("#", "**"))):
        text = rest.lstrip("\n")
        if text.startswith("---"):
            text = text[3:].lstrip("\n")
    # 決まった言い回しの括弧だけを外す（2026-10-08 担当者決定。ほかの言い回しは変えない）
    text = text.replace("【様式の記入の決まり】", "様式の記入の決まり").replace("【支給要領などの決まり】", "支給要領など")
    text = _split_packed_labels(text)
    lines = text.split("\n")
    era_fix = _plain(ERA_FIX_TEXT)
    grade_at, reason, field = None, "", None
    for i, line in enumerate(lines):
        label = _plain(line)
        if label.startswith(("評価:", "評価：")):
            grade_at, reason, field = i, "", "評価"
        elif label.startswith(("理由:", "理由：")):
            reason, field = label, "理由"
        elif label.startswith(("修正案:", "修正案：")) and grade_at is not None:
            field = "修正案"
            at, body = i, label[4:]
            if not body and i + 1 < len(lines):
                at, body = i + 1, _plain(lines[i + 1])
            if body != era_fix and _ERA_UNCLEAR_RE.search(reason) and _DATED_RE.search(body):
                if at == i:
                    m = re.match(r"^(.*?修正案\**\s*[:：]\s*\**\s*)", line)
                    lines[i] = (m.group(1) if m else "修正案: ") + ERA_FIX_TEXT
                else:
                    lines[at] = ERA_FIX_TEXT
                body = era_fix
            if body == era_fix and "⚠️要修正" in lines[grade_at]:
                lines[grade_at] = lines[grade_at].replace("⚠️要修正", "💡改善提案")
        elif field == "理由" and label:
            reason += label
    return "\n".join(_layout_review(lines))


def _turn_reminder(selected_form: str) -> str:
    """毎回の質問の末尾に付ける短い注意書き（利用者には見えない・記録にも残さない）。
    会話履歴が別の様式の話に流れていても、いま扱う様式に引き戻すため。"""
    if selected_form == GENERAL_FORM:
        return ("\n\n［この相談の前提：様式は選ばれていません。欄ごとの記入案内はせず、"
                "記入の相談は「切り替える」から様式を選ぶよう案内する。この前提には触れずに回答する。］")
    return (f"\n\n［この相談の前提：いまの様式は「{_form_display(selected_form)}」。"
            "それより前に切り替えた様式も含め、この様式以外の欄の案内や記入例づくりはしない。"
            "ほかの様式にどの欄があるかは断定しない。"
            "元号の分からない日付は変換せずに確認する。この前提には触れずに回答する。］")


# =============================================================
# システムプロンプト構築（5タイプ判別ロジック統合）
# =============================================================
def build_system_prompt(selected_grant, selected_form, form_map, rules_and_cases, relevant_chunks):
    form_data = form_map.get(selected_form, {})
    form_text = ("（様式を特定していません）" if selected_form == GENERAL_FORM
                 else _form_items_to_text(form_data.get("items", [])))
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
  - 【対象様式データ】から似た語を拾って「どの項目ですか？」と聞き返すことは厳禁。

■ 書類を作るのは利用者、あなたは説明役
  - この画面は書類の下書きを預かる仕組みではありません。書類を作成して提出するのは
    利用者（事業主・代表者）であり、あなたの役割は「その欄に何をどう書くか」を
    説明することです。
  - したがって次のことをしてはいけません。
    ・「記入が完了しました」「すべての項目の入力が終わりました」等と宣言すること
    ・様式の全項目を並べ、「記入済み」「(未入力)」のような記入状況の一覧を出すこと
    ・利用者から聞いた値を、あなたが保管・管理しているかのように扱うこと
  - ★ 会話履歴は直近のやりとりしか参照できません。項目を1つずつ順に聞いて溜めていく
    進め方をすると、長い様式では必ず前半の回答が参照できなくなり、
    実際には答えてもらった項目まで「(未入力)」と書いてしまいます。
    その進め方は取らないでください。
  - 利用者が値を示したときは、その値がルールに照らして適切かを判断し、
    書き方（文面・単位・書式）を示して、ご自身の書類に書き写すよう伝えてください。

■ 能動的ヒアリング（逆質問）の原則
  - 「支給額は？」等の制度全般に関する質問には、まず基本情報を即答したうえで、
    その質問に答えるために足りない情報だけを尋ねること。
    様式を最初から順に埋めるためのヒアリングはしないこと。

■ 5タイプ判別と回答スタイル
  ▶ タイプ1【チェック型】→ ルールのみ。事例引用厳禁。
  ▶ タイプ2【自由記述型】→ 参考事例を引用して記入見本を作成。
  ▶ タイプ3【数値・計算型】→ 計算式明示。ヒアリング後に具体的計算結果を提示。
  ▶ タイプ4【日付・期間型】→ 期限警告を最優先。
  ▶ タイプ5【選択・フラグ型】→ 定義の違いを解説し選択基準を提示。

■ 書き方の決まり
  - すべて日本語で書くこと。
  - 【対象様式データ】【基本ルール】の記載をそのまま貼り付けてはならない。
    波括弧・角括弧・引用符を使ったデータ形式や、英字の項目名を
    回答に出してはならない。根拠は必ず自分の言葉で日本語に言い換えること。
  - 出典を示す場合は、資料名のみを「（出典: ○○.pdf）」の形で添えること。

{_scope_rules(selected_form)}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【対象様式データ】（様式: {_form_display(selected_form)}）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{form_text}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【基本ルール・数値定義（各種公式資料より抽出）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{_rules_to_text(rules_and_cases)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【参考事例・申請記入例（自由記述項目への回答時に優先活用）】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{relevant_chunks if relevant_chunks else "（関連する参考事例なし）"}
"""


# =============================================================
# 添削用システムプロンプト構築
# =============================================================
# ナレッジを JSON のまま渡すと、AI が根拠を示すときにレコードを
# そのまま貼り付け、レポートに category / term / definition などの
# 英語キー名が現れる。キー名自体を見せないよう、渡す前に日本語の
# 箇条書きへ整形する。
#
# header_recipient のような英単語だけの item_id を持つドメインがある。
# 見出しに出すとレポートにそのまま英語が現れるので伏せる。ただし
# 「第1条」「1-⑥」「③(1)」「3-4」のような番号・記号のIDは、書類上の
# どこを指すかを示す手がかりなので残す（英語の原因にもならない）。
_SLUG_ID_RE = re.compile(r"^(?=[A-Za-z0-9_.\-]+$).*[A-Za-z]")


# ── 項目の見出し（AIに渡す資料・右の記入項目から送る質問文で使う）──
# 項目ID（item_id）には、様式に印刷された番号（①、2(1)、11.1.(1).ソ、第3条 など）と、
# データを作るときに付けた内部の名前（「事業主_電話番号」「別添_row3_労働者氏名」など）が混ざっている。
# 番号は利用者もAIも欄を見分ける手がかりになるので見出しに残し、内部の名前は見出しに出さない
# （2026-10-08 担当者決定）。同じ欄の名前が1つの様式に複数あるときだけ、内部の名前の中から
# 利用者にも分かる部分（「3行目」「事業主」など）を添えて区別する。データ（form_structures）は変えない。
_NUM_TOKEN = (r"(?:第[0-9０-９一二三四五六七八九十百]+[条号項章節]"
              r"|No\.?[0-9０-９]+"
              r"|[（(][0-9０-９A-Za-zア-ン①-⑳一二三四五六七八九十]{1,3}[）)]"
              r"|[0-9０-９]{1,3}"
              r"|[①-⑳㉑-㊿]"
              r"|[A-Za-z](?![A-Za-z])"
              r"|[ア-ン](?![ァ-ヶー一-龥ぁ-ん]))")
_NUM_SEG = rf"{_NUM_TOKEN}(?:[.．]?{_NUM_TOKEN})*"
# 1つのファイルに複数の様式がまとまっているもの（様式第3号①〜④ など）は、
# 様式の呼び名も印刷された名前なので、番号と同じく見出しに残す。
_FORM_SEG = r"様式第?[0-9０-９]+号[①-⑳0-9０-９]*"
_NUM_PREFIX_RE = re.compile(
    rf"^(?:{_FORM_SEG}(?:[_\-－ 　]+{_NUM_SEG})*|{_NUM_SEG}(?:[_\-－ 　]+{_NUM_SEG})*)"
    r"(?=$|[_\-－ 　]|[^A-Za-z0-9０-９])")
_ROW_RE = re.compile(r"^row([0-9]+)$", re.I)


# 欄の名前だけでは意味が分からないもの（日付の枠・「その他」・単位だけ など）
_GENERIC_LABEL_RE = re.compile(r"^[\s　年月日時分()（）:：・/／\-－~〜]*$"
                               r"|^(有|無|有・無|はい|いいえ|○|〇|□|その他|備考|金額|円|人|日|時間|数|計|合計|小計)$")


def _item_number(item_id: str) -> str:
    """項目IDの先頭にある、様式に印刷された番号の部分（なければ空）。区切りは空白にそろえる。"""
    m = _NUM_PREFIX_RE.match(item_id or "")
    return re.sub(r"[_　 ]+", " ", m.group(0)).strip(" -－") if m else ""


def _item_qualifier(item_id: str, label: str, number: str) -> str:
    """同じ名前の欄を見分けるための添え書き。内部の名前から、利用者にも分かる部分だけを取り出す。"""
    m0 = _NUM_PREFIX_RE.match(item_id or "")
    rest = (item_id or "")[len(m0.group(0)) if m0 else 0:]
    out = []
    for tok in re.split(r"[_\-－]+", rest):
        tok = tok.strip(" 　")
        if not tok or tok == label or (len(tok) >= 2 and tok in label):
            continue
        m = _ROW_RE.match(tok)
        f = re.fullmatch(r"Form([0-9]+)", tok, re.I)
        if m:
            out.append(f"{m.group(1)}行目")
        elif f:
            out.append(f"様式第{f.group(1)}号")
        elif re.fullmatch(r"[A-Za-z0-9.]+", tok) and not re.fullmatch(r"[0-9]+|No\.?[0-9]+|[A-Za-z]|[A-Z]{2,6}", tok):
            continue                      # 英小文字の内部の名前（applicant など）は出さない。OJT・FAX などの略語は残す
        else:
            out.append(tok)
    return " ".join(out)[:40]


def _item_headings(items: list) -> list:
    """様式の各項目の見出し（items と同じ順番）。番号＋欄の名前。内部の名前は出さない。"""
    heads = []
    for it in items:
        item_id = str(it.get("item_id", "")).strip()
        # 欄の名前に入っている「_」（データの書き方）は、見出しに出すときだけ空白にする（2026-10-08 担当者決定）
        label = re.sub(r"\s*_+\s*", " ", str(it.get("label", ""))).strip()
        number = "" if _SLUG_ID_RE.match(item_id) else _item_number(item_id)
        if number and label:
            last = number.split(" ")[-1]
            if label.startswith(number):
                head = label
            elif label.startswith(last):          # 「② イ」＋「イ 常用労働者」→「② イ 常用労働者」
                head = f"{number[:-len(last)].strip()} {label}".strip()
            else:
                head = f"{number} {label}"
        else:
            head = label or number or item_id
        heads.append(head)
    # 欄の名前だけでは何の欄か分からないもの（「その他」「年月日」「備考」「円」など）は、
    # 同じ名前がなくても添え書きを付ける（内部の名前がその手がかりだったため）
    for i, it in enumerate(items):
        label = re.sub(r"\s*_+\s*", " ", str(it.get("label", ""))).strip()
        if _GENERIC_LABEL_RE.match(label):
            iid = str(it.get("item_id", "")).strip()
            q = _item_qualifier(iid, label, _item_number(iid))
            if q:
                heads[i] = f"{heads[i]}（{q}）"
    # 同じ見出しが2つ以上あるときだけ、添え書きで見分ける
    for h in {h for h in heads if heads.count(h) > 1}:
        idx = [i for i, x in enumerate(heads) if x == h]
        quals = {}
        for i in idx:
            it = items[i]
            q = _item_qualifier(str(it.get("item_id", "")).strip(), re.sub(r"\s*_+\s*", " ", str(it.get("label", ""))).strip(),
                                _item_number(str(it.get("item_id", "")).strip()))
            quals[i] = q
        for n, i in enumerate(idx, 1):
            q = quals[i]
            if not q or list(quals.values()).count(q) > 1:
                q = f"{q} {n}つ目".strip() if q else f"{n}つ目"
            heads[i] = f"{h}（{q}）"
    return heads


def _form_items_to_text(items: list) -> str:
    # 見出しは _item_headings で作る（様式の番号＋欄の名前。内部の名前は出さない。
    # 同じ名前の欄は「3行目」「事業主」などを添えて見分ける）。
    lines = []
    for it, head in zip(items, _item_headings(items)):
        lines.append(f"■ {head or '（項目名なし）'}")
        if it.get("instruction"):
            lines.append(f"  記入の考え方: {it['instruction']}")
        if it.get("logic_check"):
            lines.append(f"  確認すべき点: {it['logic_check']}")
        lines.append("")
    return "\n".join(lines).rstrip() or "（様式基準なし）"


def _rules_to_text(rules: list) -> str:
    # domain は選択中のドメインで固定なので落とす。applies_to（支給申請・
    # 計画届など）は、様式と段階の対応表が未定義のドメインでは絞り込みが
    # 効かず全件が渡るため、落とすとルールの適用場面が判断できなくなる。
    lines = []
    for r in rules:
        term = str(r.get("term", "")).strip() or "（項目名なし）"
        lines.append(f"■ {term}")
        if r.get("category"):
            lines.append(f"  分類: {r['category']}")
        if r.get("definition"):
            lines.append(f"  内容: {r['definition']}")
        applies = [str(a) for a in (r.get("applies_to") or []) if str(a).strip()]
        if applies:
            lines.append(f"  適用場面: {'、'.join(applies)}")
        if r.get("source"):
            lines.append(f"  出典: {r['source']}")
        lines.append("")
    return "\n".join(lines).rstrip() or "（ルール基準なし）"


def build_review_prompt(selected_form, form_map, rules_and_cases):
    form_items = form_map.get(selected_form, {}).get("items", [])
    today = date.today()
    reiwa_year = today.year - 2018
    today_str = f"{today.year}年{today.month}月{today.day}日（令和{reiwa_year}年{today.month}月{today.day}日）"
    return f"""
あなたは、助成金の申請書類を提出前に点検する添削の担当です。
アップロードされた書類の各欄を、【様式の記入の決まり】と【支給要領などの決まり】に書かれた条件の一つひとつと照らし合わせ、見落としのないよう厳密に添削してください。
条件（通し番号の付け方・記入のしかた・選び方・日付や数の決まりなど）は、細かい点でも満たしているかを確かめ、満たしていなければ指摘してください。書かれていることが決まりに沿っているように見えても、決まりの条件を一つずつ確かめてから判断してください。
理由には、どの決まりのどの条件と照らしたかと、書類の記載がどうなっているかを、具体的に書いてください。

【本日の日付】{today_str}
※ 日付の過去・未来の判定は必ず上記の本日の日付を基準にしてください。

【添削手順】
STEP1: 書類の各項目を識別し、【様式の記入の決まり】の該当する項目と照合する。
STEP2: 各記載内容が、その項目の「記入の考え方」に沿っているか確認する。
STEP3: 数値・日付・計算値が【支給要領などの決まり】と矛盾していないか確認する。
STEP4: 結果を ⚠️要修正 / 💡改善提案 / ✅問題なし の3段階で報告。

【選択肢がグループになっている項目の見かた】
・「ア、イ、ウのいずれか一つ選択必須」のように、複数の項目が同じ選び方の
  ルールを持っていることがある。様式の上では1つの設問で、選択肢が並んで
  いるだけなので、グループ全体で1つの判断をすること。
・★ グループのどれか1つが選ばれていれば、選ばれなかった残りは正しい状態
  である。残りを ⚠️要修正 にしてはならない。「アが選ばれているため、
  イ・ウが未選択なのは適切」とまとめ、見出しも1つにして1回だけ報告する。
  1つ選べば足りる設問で、選ばれなかった選択肢を欠落として指摘すると、
  同じ設問の中で「問題なし」と「要修正」が同時に並ぶ矛盾した報告になる。
・⚠️要修正 とするのは、グループのどれも選ばれていない場合か、
  一つだけ選ぶ決まりなのに2つ以上選ばれている場合だけ。
・「ウを選んだ場合は括弧内に記載」のような、選んだときだけ効く条件は、
  その選択肢が選ばれているときだけ確認すること。

{_review_scope_rules(selected_form)}【出力形式】
・前置き（「〜として添削しました」「〜に基づき添削いたしました」など）は書かず、最初の項目の見出しから書き始めること。
・書類に記載されている順に、項目ごとの見出しを立てて報告すること。
・各項目は、次の形で、見出し・評価・理由・修正案をそれぞれ別の行に書くこと（1行にまとめない）。

    **① 事業所名**
    評価：⚠️要修正 / 💡改善提案 / ✅問題なし のいずれか
    理由：照らした決まりの条件と、書類の記載がどうなっているかを具体的に説明する
    修正案：要修正・改善提案の場合のみ、書き換え後の文面をそのまま示す

・理由は、問題がない欄でも「記載されており、決まりに沿っています」のような決まり文句で済ませず、
  どの条件を確かめて、書類のどの記載がそれを満たしているかを書くこと。

・すべて日本語で書くこと。
・【様式の記入の決まり】【支給要領などの決まり】の記載をそのまま貼り付けてはならない。
  波括弧・角括弧・引用符を使ったデータ形式や、英字の項目名を
  レポートに出してはならない。根拠は必ず自分の言葉で日本語に言い換えること。
・基準の出典を示す場合は、資料名のみを「（出典: ○○.pdf）」の形で添えること。

【様式の記入の決まり】（{_form_display(selected_form)}）
{_form_items_to_text(form_items)}

【支給要領などの決まり】（支給要領）
{_rules_to_text(rules_and_cases)}
"""


# =============================================================
# Word 文書の本文取り出し
# =============================================================
def docx_to_text(doc) -> str:
    """本文を、書かれている順に取り出す。

    ★ doc.paragraphs は表の中の段落を含まない。
      申請様式は記入欄がほぼ全部表の中にあるため、段落だけを渡すと
      表紙と注意書きしか見えない。実測で、雇用管理制度等整備計画書は
      全体の 12% しか渡っていなかった。

    表はセルを「｜」で区切った1行にする。行と列の関係が保たれ、
    どの見出しの欄に何が書いてあるかが読み取れる。
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    out = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            t = Paragraph(child, doc).text.strip()
            if t:
                out.append(t)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells = []
                for c in row.cells:
                    v = " ".join(x.text.strip() for x in c.paragraphs if x.text.strip())
                    cells.append(v)
                # 結合セルは同じ内容が繰り返し返るので、重複を落として1行にする
                if any(cells):
                    out.append(" ｜ ".join(dict.fromkeys(cells)))
    return "\n".join(out)


def count_docx_shapes(raw: bytes) -> int:
    """図形（○印・矢印など）の数を数える。

    python-docx は図形を扱えないので、docx の中身（XML）を直接見る。
    手で付けた○は v:oval、Word の図形は wps:wsp として入っている。
    """
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception:
        return 0
    return xml.count("<v:oval") + xml.count("<v:shape") + xml.count("<wps:wsp")


# =============================================================
# ファイル添削処理（PDF / DOCX / XLSX）
# =============================================================
# =============================================================
# 添削の前に、書類の様式番号と選んだ様式を照らし合わせる（2026-10-07 追加）
# =============================================================
# AI に「別の様式なら止めて」と頼むだけでは、止まったり止まらなかったりした。
# 様式番号の照合は機械的にできるので、AI に送る前にプログラムで行う。
#   ・書類の冒頭に様式番号があり、選んだ様式と違う → AI に送らず、選び直しを案内して止める
#   ・書類の冒頭に様式番号がない → 止めずに添削し、冒頭に一言添える
#   ・選んだ様式に様式番号がない（就業規則など） → これまでどおり添削する
# 「共通要領様式第１号」のように前に語が付く様式もあるため、比べるのは「様式第…号」の部分だけ。
# 実物の様式には「様式第10 号」（番号と号の間に空白）や「建活様式第５号－２」（「の２」を「－２」と書く）もある。
_DOC_FORM_NO_RE = re.compile(r"様式第\s*[0-9A-Za-z\-]+?\s*号(?:\s*(?:の|-)\s*\d+)*(?:[\s_]*別紙\s*\d+)?")
FORM_UNKNOWN_NOTE = "書類から様式名を確認できなかったため、選ばれている様式として添削しました。"


def _doc_form_nos(text: str) -> list:
    """文中に出てくる「様式第…号（の N）（別紙 N）」を出てくる順に。全角・空白・書き方をそろえて返す。
    括弧は区切りとして残す（「別紙７）（2025.4改正）」を「別紙72025」と読まないため）。"""
    norm = re.sub(r"[‐－−―]", "-", unicodedata.normalize("NFKC", text or ""))
    out = []
    for m in _DOC_FORM_NO_RE.finditer(norm):
        s = re.sub(r"[\s_]", "", m.group(0))
        s = re.sub(r"号-(\d+)", r"号の\1", s)
        out.append(s)
    return out


def _doc_form_no(text: str) -> str:
    """最初に出てくる様式番号。なければ空文字。"""
    nos = _doc_form_nos(text)
    return nos[0] if nos else ""


def _doc_head_text(uploaded_file, limit: int = 400) -> str:
    """書類の冒頭の文字（様式番号を探すため）。読めなければ空文字。読んだあとは先頭に戻す。"""
    name = uploaded_file.name.lower()
    raw = uploaded_file.read()
    uploaded_file.seek(0)
    try:
        if name.endswith(".pdf"):
            try:
                import pymupdf as fitz  # 新しい名前
            except ImportError:
                import fitz
            with fitz.open(stream=raw, filetype="pdf") as pdf:
                return pdf[0].get_text()[:limit] if len(pdf) else ""
        if name.endswith(".docx"):
            from docx import Document
            return docx_to_text(Document(io.BytesIO(raw)))[:limit]
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            import pandas as pd
            df = pd.read_excel(io.BytesIO(raw), header=None, dtype=str, nrows=10).fillna("")
            return " ".join(" ".join(r) for r in df.values.tolist())[:limit]
        if name.endswith(".csv"):
            for enc in ("utf-8-sig", "shift_jis"):
                try:
                    return raw.decode(enc)[:limit]
                except UnicodeDecodeError:
                    continue
    except Exception:
        return ""
    return ""


def review_document(uploaded_file, selected_form, form_map, rules_and_cases):
    want = _doc_form_no(selected_form)
    nos = _doc_form_nos(_doc_head_text(uploaded_file)) if want else []
    got = nos[0] if nos else ""
    # 止めるのは、冒頭に様式番号があり、そのどれもが選んだ様式と違うときだけ
    # （冒頭に注意書きとして別の様式番号が出てくることがあるため）
    if want and nos and want not in nos:
        return (f"アップロードされた書類は「{got}」のようです。選ばれている様式（{want}）とは違うため、"
                "添削できません。画面で正しい様式を選び直してから、もう一度添削してください。")
    result = _review_document_ai(uploaded_file, selected_form, form_map, rules_and_cases)

    def _again():
        uploaded_file.seek(0)
        return _review_document_ai(uploaded_file, selected_form, form_map, rules_and_cases)
    result = _guard_answer(result or "", _again, "添削")
    if result and not result.startswith("❌") and result != LEAK_FALLBACK:
        result = _tidy_review(result)
    if want and not got and result and not result.startswith("❌") and result != LEAK_FALLBACK:
        result = f"{FORM_UNKNOWN_NOTE}\n\n{result}"
    return result


def _review_document_ai(uploaded_file, selected_form, form_map, rules_and_cases):
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
        except ImportError:
            return "❌ `pip install python-docx` が必要です。"
        raw  = uploaded_file.read()
        doc  = Document(io.BytesIO(raw))
        text = docx_to_text(doc)
        # 図形（○印など）は文字として取り出せない。あることだけ伝え、
        # 位置の判定はさせない。黙っていると当て推量で「○が付いている」と
        # 書いてしまう。
        shapes = count_docx_shapes(raw)
        notice = ""
        if shapes:
            notice = (
                f"\n\n【この文書の読み取りについて】\n"
                f"この Word 文書には、文字ではない図形が {shapes} 個あります。"
                "様式の選択肢に手で付けた○印は、この図形であることがほとんどです。"
                "図形は文字として取り出せないため、下の本文には含まれていません。\n"
                "・どの選択肢に○が付いているかは判定できません。推測で「○が付いている」"
                "「選択済み」と書かないでください。\n"
                "・選択欄については『○印は文字として読み取れないため確認できません。"
                "PDF形式で保存し直して添削すると判定できます』と書いてください。\n"
            )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"以下のWord文書を添削してください：{notice}\n\n{text}",
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
        # 切り替えの区切りの行は AI 側の行として残る。同じ話し手が続く・先頭が AI 側になるのを避ける
        if not contents and role == "model":
            continue
        if contents and contents[-1].role == role:
            contents[-1].parts.append(types.Part(text=m["content"]))
            continue
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=current_prompt)]))
    return contents


# =============================================================
# AI応答処理（共通関数化）
# =============================================================
# =============================================================
# AI が考えた過程を回答に書いてしまったときの備え（2026-10-07 追加）
# =============================================================
# まれに（試験で数十回に1回）、AI が「思考プロセス」「_thought」などから始めて、
# 考えた過程をそのまま回答に書くことがある。利用者に見せないために:
#   1. 回答の書き出しがそうなっていたら、流れてくる途中でも画面には出さない
#   2. もう一度だけ作り直してもらう（たいていは2回目で普通の回答になる）
#   3. それでもだめなら、「回答生成」などの区切りより後ろだけを残す
#   4. それも取れなければ、もう一度送ってもらうよう案内する
# 取り除いたことは利用者には見せず、件数を数えられるよう記録（標準エラー）に1行だけ残す
#（中身は残さない）。数え方: journalctl -u jyoseikin@r8 | grep -c "\[thought-guard\]"
# 判定は「書き出し」だけを見る。普通の回答の途中に「思考」という言葉が出ても対象にしない。
_LEAK_HEAD_RE = re.compile(
    r"^[\s#*_>\-]*(?:思考プロセス|思考過程|考えた過程|_?thoughts?\b|thinking\b)", re.IGNORECASE)
_LEAK_PROBE_CHARS = 30   # 書き出しをこの文字数まで見てから画面に出し始める
_LEAK_MARKERS = ("**回答生成**", "回答生成", "上記に基づき、回答を生成する。", "この回答で良さそうだ。",
                 "【回答】", "**回答**")
LEAK_FALLBACK = "申し訳ありません。回答をうまく作成できませんでした。お手数ですが、もう一度お送りください。"


def _is_leak(text: str) -> bool:
    return bool(_LEAK_HEAD_RE.match(text or ""))


def _extract_answer(text: str) -> str:
    """考えた過程のあとに続く本来の回答だけを取り出す。取り出せなければ空文字。"""
    cands = []
    for m in _LEAK_MARKERS:
        i = text.rfind(m)
        if i >= 0:
            cands.append(text[i + len(m):])
    # 区切り線（---）は考えた過程と回答の境目に使われることがある。回答の中でも使われるので、最初の線より後ろを取る
    parts = re.split(r"\n\s*---+\s*\n", text, maxsplit=1)
    if len(parts) > 1:
        cands.append(parts[1])
    for c in cands:
        c = c.strip().lstrip("-*:： \n").strip()
        if len(c) >= 15 and not _is_leak(c):
            return c
    return ""


def _log_thought_guard(where: str, action: str, chars: int) -> None:
    print(f"[thought-guard] {where} action={action} chars={chars}", file=sys.stderr, flush=True)


def _guard_answer(text: str, regenerate, where: str) -> str:
    """回答が考えた過程から始まっていたら、作り直し→区切りより後ろ→案内、の順に差し替える。"""
    if not _is_leak(text):
        return text
    try:
        again = regenerate() or ""
    except Exception:
        again = ""
    if again and not _is_leak(again):
        _log_thought_guard(where, "regenerated", len(text))
        return again
    picked = _extract_answer(again or text) or _extract_answer(text)
    if picked:
        _log_thought_guard(where, "extracted", len(text))
        return picked
    _log_thought_guard(where, "fallback", len(text))
    return LEAK_FALLBACK


def _show_streaming(placeholder, full: str) -> None:
    """流れてくる途中の表示。書き出しを確かめるまでは出さず、考えた過程なら「作成中」とだけ出す。"""
    if _is_leak(full):
        placeholder.markdown("回答を作成しています…")
    elif len(full) >= _LEAK_PROBE_CHARS:
        placeholder.markdown(_md(full) + "▌")


def _regenerate_chat(model_name, contents, system_prompt) -> str:
    resp = client.models.generate_content(
        model=model_name, contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt))
    parts = (resp.candidates[0].content.parts or []) if resp.candidates and resp.candidates[0].content else []
    return "".join(p.text for p in parts if p.text and not getattr(p, "thought", False))


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
    # 質問の末尾に前提を付けるのは AI に送る分だけ（画面と記録は利用者の入力のまま）
    gemini_contents = build_gemini_contents(
        st.session_state.messages, prompt + _turn_reminder(st.session_state.selected_form))

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
                            _show_streaming(placeholder, full)
                full = _guard_answer(
                    full, lambda: _regenerate_chat(model_name, gemini_contents, system_prompt), "相談")
                placeholder.markdown(_md(full) or "（回答を生成できませんでした）")
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
        # 日本時間で動かす（未指定だとサーバーの時計＝多くは世界標準時になり、日本時間の11時に動いてしまう）
        scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
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
/* 添削の結果の欄の見出し（_layout_review が ##### で書く。案A：一回り大きく、左に色の帯）。
   相談の回答で AI が使うことのある ### や #### には効かせないよう、h5 だけにする */
[data-testid="stChatMessage"] h5 { font-size: 1.0rem !important; font-weight: 700 !important; color: var(--ink) !important;
    border-left: 4px solid var(--navy); background: var(--navy-tint); padding: .3rem .6rem !important;
    margin: 1.1rem 0 .45rem !important; border-radius: 0 6px 6px 0; }
[data-testid="stChatMessage"] h5 [data-testid="stHeaderActionElements"], [data-testid="stChatMessage"] h5 a { display: none !important; }

/* ───────── コンテキストバー（チャット上部） ───────── */
.ctx-bar {
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
    padding: 1.1rem 1.3rem 1rem; margin-bottom: 1rem;
}
.st-key-ctx_bar {
    background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
    padding: 1.1rem 1.3rem 1rem; margin-bottom: 1rem; gap: .2rem;
}
.st-key-ctx_bar .ctx-meta { margin-top: .2rem; }
.st-key-ctx_bar [data-testid="stPopover"] button {
    border: 1px solid var(--navy-line); color: var(--navy); background: var(--navy-tint);
    font-weight: 700; font-size: .85rem;
}
.st-key-form_strip {
    background: var(--navy-tint); border: 1px solid var(--navy-line); border-radius: 10px;
    padding: .35rem .8rem; margin: .4rem 0 .5rem;
}
.form-strip { font-size: .95rem; color: var(--ink-sub); line-height: 1.5; }
.form-strip b { color: var(--navy); }
.st-key-form_strip [data-testid="stPopover"] button {
    min-height: 0; padding: .25rem .7rem; font-size: .88rem; font-weight: 700;
    color: var(--navy); background: #fff; border: 1px solid var(--navy-line);
}
.form-switch-note {
    display: flex; align-items: center; gap: 12px; margin: 1.1rem 0;
    color: var(--navy); font-size: .82rem; font-weight: 700;
}
.form-switch-note::before, .form-switch-note::after {
    content: ""; flex: 1; border-top: 1px dashed var(--navy-line);
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
/* 添削パネルの注意書き。濃紺の上なので、本文色のままだと沈む。 */
.upload-note {
    color: #C7D5E6 !important; font-size: .74rem; line-height: 1.65;
    margin: 2px 0 10px; padding: 9px 11px;
    background: rgba(255,255,255,.06); border-left: 3px solid #7FA9DC;
    border-radius: 6px;
}
.upload-note b { color: #FFFFFF !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background: rgba(255,255,255,.05) !important; border-color: var(--sb-line) !important;
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] * { color: var(--sb-muted) !important; }
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
    background: transparent !important; border-color: var(--sb-line) !important; color: var(--sb-fg) !important;
}

/* 添削中の表示。
   既定のスピナーは濃紺サイドバーの奥で小さく回るだけで、処理が進んでいるのか
   固まったのか分からない。添削は書類の量によっては1分近くかかるので、
   画面の中央に出して手を止めてよいことが分かるようにする。 */
.busy-veil {
    /* サイドバー(999991)・ヘッダー(999990)より上に出す */
    position: fixed; inset: 0; z-index: 999999;
    display: flex; align-items: center; justify-content: center;
    background: rgba(18,22,29,.58);
    backdrop-filter: blur(2px);
}
.busy-card {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: 0 18px 48px -18px rgba(18,22,29,.55);
    padding: 26px 34px; text-align: center; min-width: 264px;
}
.busy-dots { display: flex; gap: 7px; justify-content: center; margin-bottom: 14px; }
.busy-dots i {
    display: block; width: 9px; height: 9px; border-radius: 50%;
    background: var(--navy);
    animation: busy-bounce 1.05s infinite ease-in-out both;
}
.busy-dots i:nth-child(2) { animation-delay: .14s; }
.busy-dots i:nth-child(3) { animation-delay: .28s; }
@keyframes busy-bounce {
    0%, 80%, 100% { transform: scale(.55); opacity: .45; }
    40%           { transform: scale(1);   opacity: 1; }
}
.busy-title { font-size: 1.02rem; font-weight: 700; color: var(--ink); }
.busy-sub { margin-top: 7px; font-size: .82rem; color: var(--ink-muted); line-height: 1.75; }
@media (prefers-reduced-motion: reduce) {
    .busy-dots i { animation: none; opacity: .85; }
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

# コースが選ばれていれば、様式も知識もそのコースのぶんだけに絞る。
# ここで絞っておけば、以降の質問・添削・右カラムはすべて自動的にコース単位になる。
_course_key = st.session_state.get("selected_course", "")
if _domain_key and _course_key:
    _drop           = excluded_sources(domain_config, _course_key)
    form_map        = filter_forms_by_course(form_map, domain_config, _course_key)
    rules_and_cases = drop_sources(rules_and_cases, _drop)
    pdf_chunks      = drop_sources(pdf_chunks, _drop)

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
    "selected_course":     "",   # コース区分のある制度だけ使う（無い制度は空）
    "selected_course_name": "",
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
# トークンを処理するのは未ログインのときだけ。ログイン成立後に再実行されても
# ここを通らないため、同じトークンが二重に処理されることはない。
#
# ただし【URLからの除去は認証状態に関わらず必ず行う】。
# 処理をスキップしたときに消し忘れると、トークンがURLに残ったままになり、
# ブラウザの戻る操作で何度もその状態に戻れてしまう。さらにその状態で
# ログアウトすると、残ったトークンが再処理されて「既に使用済みです」と
# 表示され、通常のログイン画面に戻れなくなる。
_sso_token = st.query_params.get("t")
if _sso_token:
    if not st.session_state.authenticated:
        import sso as _sso

        try:
            _sso_user, _sso_err, _sso_ret = _sso.authenticate(_sso_token)
        except psycopg2.Error as e:
            # トレースバックを利用者に見せない。本当の理由はサーバーのログに残す。
            print(f"[sso] db_error {type(e).__name__}: {str(e).strip()[:200]}", file=sys.stderr, flush=True)
            _sso_user, _sso_err, _sso_ret = None, "db_error", ""
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

        /* ログイン失敗の表示。
           既定のアラートは淡い下地に載せる前提の配色なので、
           ここだけ全面が濃紺だと地に沈んで、何が書いてあるか読めない。
           カードと同じ白い面に赤い縦線を立てて、本文は濃い赤にする。 */
        [data-testid="stAlertContainer"] {
            background: #FFFFFF !important;
            border: 1px solid #E7C3C5 !important;
            border-left: 5px solid var(--danger) !important;
            color: var(--danger) !important;
            font-size: .88rem !important;
            font-weight: 600;
            box-shadow: 0 10px 26px -14px rgba(0,0,0,.4) !important;
        }
        [data-testid="stAlertContainer"] * { color: var(--danger) !important; }
        /* 既定のアイコンは淡くて見えないので、色を合わせる */
        [data-testid="stAlertContainer"] svg { fill: var(--danger) !important; }
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
            "db_error": (
                "現在、一時的にログインできません。"
                "少し時間をおいてから、もう一度お試しください。"
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
            # DB に一時的につながらないときに、Streamlit 既定のトレースバック
            # （ファイルパス入り）が利用者に出ないようにする。
            # ★ config.toml の showErrorDetails は Streamlit Cloud 側の設定に上書きされ、効かない。
            try:
                user = login(input_username, input_password)
                db_down = False
            except psycopg2.Error as e:
                print(f"[login] db_error {type(e).__name__}: {str(e).strip()[:200]}", file=sys.stderr, flush=True)
                user, db_down = None, True
            if db_down:
                st.error("現在、一時的にログインできません。少し時間をおいてから、もう一度お試しください。")
            elif user:
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
                    _restore_course(_conv)
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

        # ── コース選択（コース区分のある制度だけ挟む）──
        # 制度を選んだ時点では、その制度の全コースの様式が混ざっている。
        # ここで絞らないと、たとえば人材開発支援助成金で教育訓練休暇の様式と
        # 人材育成支援コースの様式が同じ一覧に並んでしまう。
        _courses      = domain_courses(_sel_cfg)
        _sel_course   = ""
        _sel_course_nm = ""
        _step = 2
        if _courses:
            st.markdown(
                "<div class='form-sep'></div>"
                f"<div class='step-head'><span class='step-num'>STEP {_step}</span>"
                "<span class='step-title'>コースを選択</span></div>",
                unsafe_allow_html=True,
            )
            _course_keys   = [c.get("key", "") for c in _courses]
            _course_labels = [c.get("name", c.get("key", "")) for c in _courses]
            _prev_course   = st.session_state.get("selected_course", "")
            _c_idx = _course_keys.index(_prev_course) if _prev_course in _course_keys else 0
            _c_sel = st.selectbox(
                "コース",
                range(len(_course_keys)),
                format_func=lambda i: _course_labels[i],
                index=_c_idx,
                label_visibility="collapsed",
            )
            _sel_course    = _course_keys[_c_sel]
            _sel_course_nm = _course_labels[_c_sel]
            # 選んだコースの様式だけに絞ってから STEP 3 に渡す
            _fm = filter_forms_by_course(_fm, _sel_cfg, _sel_course)
            _step = 3

        st.markdown(
            "<div class='form-sep'></div>"
            f"<div class='step-head'><span class='step-num'>STEP {_step}</span>"
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
        # 題名は「様式名／コース名」（コースが無ければ制度名）。左の欄で途中が切れても様式名が見えるように先に置く
        _new_title = _make_conv_title(selected_form, _sel_domain_label, _sel_course_nm)
        # DB に新規スレッドを作成
        conv_id = create_conversation(
            st.session_state.user_id,
            _sel_domain_key,
            selected_form,
            title=_new_title,
            course=_sel_course,
        )
        st.session_state.app_state           = "chat"
        st.session_state.selected_domain_key = _sel_domain_key
        st.session_state.selected_grant      = _sel_domain_label
        st.session_state.selected_course     = _sel_course
        st.session_state.selected_course_name = _sel_course_nm
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

    # 添削中の表示を出すための置き場。
    # ★ 本文側に作ること。サイドバーの中に作ると、サイドバーに transform が
    #   掛かっているため position: fixed がサイドバー基準になり、画面全体を
    #   覆えない（幅0になる）。書き込むのはサイドバーの中からで構わない。
    busy_slot = st.empty()

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
            # ○やチェックは Word だと図形として入っていて文字にならないため、
            # 添削では読み取れない。PDF は書類そのものをAIに渡すので読める。
            # 上げる前に気づけるよう、ここに出しておく。
            st.markdown(
                "<p class='upload-note'><b>PDFを推奨します。</b>○やチェックを付けた欄は、Word・Excelのままだと読み取れません（印が文字ではなく図形として入っているため）。Word で「PDFとして保存」してからアップロードすると、印まで見て添削できます。</p>",
                unsafe_allow_html=True,
            )
            uploaded_file = st.file_uploader(
                "申請書類", type=["pdf", "docx", "xlsx", "xls", "xlsm", "csv"], label_visibility="collapsed",
            )
            if uploaded_file:
                st.caption(uploaded_file.name)
                if st.button("添削を実行", type="primary", use_container_width=True, key="review_run"):
                    # 待ち時間が長いので、サイドバーのスピナーではなく
                    # 画面全体に出す。書き出しは処理を始める前に送る。
                    busy_slot.markdown(
                        "<div class='busy-veil'><div class='busy-card'>"
                        "<div class='busy-dots'><i></i><i></i><i></i></div>"
                        "<div class='busy-title'>添削しています</div>"
                        "<div class='busy-sub'>書類の量によっては1分ほどかかります。<br>"
                        "このまま開いたままお待ちください。</div>"
                        "</div></div>",
                        unsafe_allow_html=True,
                    )
                    _report = review_document(
                        uploaded_file, st.session_state.selected_form,
                        form_map, rules_and_cases,
                    )
                    # 添削の結果は画面の上ではなく、会話の流れの一番下（実行した位置）に出す。
                    # 区切りの行とレポートを会話の記録に残すので、開き直しても同じ位置に出る。
                    _note = f"{REVIEW_MARK}添削を実行しました（{uploaded_file.name}）"
                    _body = f"{REVIEW_REPORT_HEAD}\n\n{_report}"
                    st.session_state.messages += [{"role": "assistant", "content": _note},
                                                  {"role": "assistant", "content": _body}]
                    _cid = st.session_state.get("current_conv_id")
                    if _cid:
                        add_message(_cid, "assistant", _note)
                        add_message(_cid, "assistant", _body)
                        touch_conversation(_cid)
                    st.session_state.review_result = ""
                    busy_slot.empty()
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
                _restore_course(_conv)
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
        # 様式名の横に「切り替える」を置く（押すと同じコースの様式と「全般」の一覧が出る）。
        # 枠は .st-key-ctx_bar で .ctx-bar と同じ見た目にしている。
        with st.container(key="ctx_bar"):
            _c_title, _c_switch = st.columns([3.2, 1.25], vertical_alignment="center")
            with _c_title:
                st.markdown(
                    (f"<span class='ctx-no'>{html.escape(_form_no)}</span>" if _form_no else "")
                    + f"<h2 class='ctx-title'>{html.escape(_form_name)}</h2>",
                    unsafe_allow_html=True,
                )
            with _c_switch:
                _render_form_switcher(form_map, domain_config)
            st.markdown(
                "<div class='ctx-meta'>"
                + f"<span class='ctx-domain'>{html.escape(st.session_state.selected_grant)}</span>"
                # コースまで絞っている場合は、いまどのコースを見ているかを常に出す。
                # 制度名だけだと、別コースの内容だと思い込んだまま進む恐れがある。
                + (f"<span class='ctx-sep'></span>"
                   f"<span class='ctx-domain'>{html.escape(st.session_state.selected_course_name)}</span>"
                   if st.session_state.get("selected_course_name") else "")
                + f"<span class='ctx-sep'></span><span>{DISCLAIMER_TEXT}</span>"
                + "</div>",
                unsafe_allow_html=True,
            )

        # ── 前回のエラー表示 ──────────────────────────────────
        if st.session_state.last_error:
            st.error(f"前回のエラー: {st.session_state.last_error}")
            st.session_state.last_error = ""

        # ── チャット履歴の表示 ────────────────────────────────
        if st.session_state.messages:
            for msg in st.session_state.messages:
                if _is_divider(msg["content"]):
                    _mark = FORM_SWITCH_MARK if _is_switch_note(msg["content"]) else REVIEW_MARK
                    st.markdown(
                        f"<div class='form-switch-note'><span>"
                        f"{html.escape(msg['content'][len(_mark):])}</span></div>",
                        unsafe_allow_html=True,
                    )
                    continue
                with st.chat_message(msg["role"]):
                    st.markdown(_md(msg["content"]))
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
            # 質問文も、AIに渡す資料と同じ見出しで書く（内部の名前を出さない）
            _form_items = form_map.get(st.session_state.selected_form, {}).get("items", [])
            _heads = _item_headings(_form_items)
            _head = next((h for it, h in zip(_form_items, _heads)
                          if it.get("item_id") == item.get("item_id") and it.get("label") == item.get("label")),
                         None) or _item_headings([item])[0]
            _auto_prompt = f"「{_head}」について教えてください"
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

        # ── 入力欄のすぐ上の帯（いまの様式＋切り替える）──────
        # 会話が長くなっても、上まで戻らずに様式を確かめて切り替えられるようにする
        with st.container(key="form_strip"):
            _fs_l, _fs_r = st.columns([4, 1.1], vertical_alignment="center")
            with _fs_l:
                st.markdown(
                    "<div class='form-strip'>いまの様式："
                    f"<b>{html.escape(_form_display(st.session_state.selected_form))}</b></div>",
                    unsafe_allow_html=True,
                )
            with _fs_r:
                _render_form_switcher(form_map, domain_config, where="bottom")

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

                # 表示の名前は質問文・AIに渡す資料と同じ見出し（build_item_rows）。チップは今は使わない。
                # 切り詰めは CSS 側の3行クランプに任せる（ここで削ると語の途中で切れる）。
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
