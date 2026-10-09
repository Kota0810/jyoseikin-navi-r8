"""
display_text.py  –  画面に出す文字の整え方（相談の画面と管理画面で共用する）。

Streamlit には触れない、文字を整えるだけの部品。app.py と admin.py の両方から使う
（2026-10-09：管理画面の会話履歴でも、相談の画面と同じ様式名・区切りの行・太字の表示にするため、
app.py から切り出した）。
"""
import re

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

GENERAL_FORM = "全般（様式を特定しない）"


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
    if not form_name or form_name == GENERAL_FORM:
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


def _divider_text(text: str) -> str:
    """区切りの行の、画面に出す文（目印を外したもの）。"""
    for mark in (FORM_SWITCH_MARK, REVIEW_MARK):
        if (text or "").startswith(mark):
            return text[len(mark):]
    return text or ""


def _make_conv_title(form_name: str, grant: str, course_name: str) -> str:
    """過去の会話一覧の題名。左の欄は狭く途中で切れるので、様式名を先に出す（様式名／コース名）。
    コースの無い制度は制度名、様式を選んでいない相談は「全般」。"""
    head = "全般" if (not form_name or form_name == GENERAL_FORM) else _form_display(form_name)
    return "／".join(p for p in (head, course_name or grant) if p)


# 太字の「**」は、日本語のかぎかっこ等の隣に置くと太字として扱われず、記号のまま出る。
# 表示するときだけ、「**」の内側に幅のない文字を挟んで太字として扱われるようにする
# （記録は AI の回答のまま）。HTML としては扱わないので、回答の中身が画面の部品になることはない。
_BOLD_PAIR_RE = re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*")
# 太字だけの行（見出し代わり。「**① 氏名**」「**記入の考え方：**」など）の次の行は、
# 改行1つだとつながって表示される（「① 氏名助成金申請の…」）。空行を入れて段落を分ける。
_BOLD_LINE_RE = re.compile(r"(?m)^([ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?\*\*[^\n]+?\*\*[:：]?)[ \t]*\n(?=[ \t]*\S)")


def _md(text: str) -> str:
    text = _BOLD_LINE_RE.sub(lambda m: m.group(1) + "\n\n", text or "")
    return _BOLD_PAIR_RE.sub(lambda m: "**​" + m.group(1) + "​**", text)
