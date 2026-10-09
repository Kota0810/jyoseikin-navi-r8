"""
admin.py  –  管理画面 UI（ユーザー管理・会話履歴閲覧・利用統計）
"""
import streamlit as st
from auth import require_admin, hash_password
import hashlib
import html
import json
import os
import re
import unicodedata

from display_text import GENERAL_FORM, _form_display, _is_divider, _divider_text, _md

from version import BUILD_LABEL
from db import (
    get_all_users, create_user, update_password, set_user_active, delete_user,
    update_customer_no, bulk_update_customer_no,
    get_customer_no_duplicates, get_customer_no_summary,
    get_conversation_counts_by_year,
    get_all_user_stats,
    get_nonempty_conversations_by_user, get_messages_by_conversation,
)


def _year_label(app_year: str) -> str:
    return {"R7": "令和7年度版", "R8": "令和8年度版"}.get(app_year or "R7", app_year or "R7")


def _or_none(value, empty: str = "記録なし") -> str:
    """日時などが無いとき（None）に、「None」ではなく分かる言葉で出す。"""
    return str(value) if value not in (None, "") else empty


def _key_of(text) -> str:
    """ウィジェットの key に使う短い印（同じ文字なら同じ印）。"""
    return hashlib.md5(str(text or "").encode("utf-8")).hexdigest()[:8]


def _selected_rows(event) -> list:
    """表（st.dataframe）で選ばれている行の番号。"""
    sel = getattr(event, "selection", None)
    return list(sel.rows) if sel and getattr(sel, "rows", None) else []


# ── 操作の結果の知らせ（2026-10-09 担当者決定）──
# 操作のあと画面を作り直す（st.rerun）と、その場で出した「変更しました」は消えてしまう。
# 結果を session_state に残し、次の操作の結果で上書きされるまで出し続ける。
def _flash(kind: str, msg: str, uid=None) -> None:
    """kind は success / error / info。uid はだれに対する操作か（追加・削除のあとは None）。"""
    st.session_state["admin_flash"] = {"kind": kind, "msg": msg, "uid": uid}


def _show_flash(uid=None) -> None:
    """uid を渡すと、その利用者の操作欄の中に出す。渡さないと一覧の上に出す
    （選んでいる利用者についての知らせは、操作欄の中に出すので一覧の上には出さない）。"""
    f = st.session_state.get("admin_flash")
    if not f:
        return
    if uid is not None and f.get("uid") != uid:
        return
    if uid is None and f.get("uid") is not None and f.get("uid") == st.session_state.get("um_selected_uid"):
        return
    {"success": st.success, "error": st.error}.get(f.get("kind"), st.info)(f.get("msg", ""))


def _who(user: dict) -> str:
    return f"「{user.get('display_name')}」（{user.get('username')}）"


def _short(e: Exception) -> str:
    return " ".join(str(e).split())[:120] or type(e).__name__


@st.cache_data(show_spinner=False)
def _domain_labels(domain_key: str) -> tuple:
    """制度の表示名と、コースの名前の対応表。会話の記録にはフォルダ名とコースの記号しか無いため。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domains", domain_key or "", "domain_config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return domain_key or "", {}
    courses = {c.get("key"): (c.get("name") or c.get("key")) for c in (cfg.get("courses") or [])}
    return cfg.get("display_name") or domain_key or "", courses


def _conv_form_label(conv: dict) -> str:
    """会話の様式の呼び方（相談の画面の左の一覧と同じ。ファイル名は出さない）。"""
    form = conv.get("form_name") or ""
    return "全般" if (not form or form == GENERAL_FORM) else _form_display(form)


def _conv_domain_label(conv: dict) -> str:
    """会話の制度・コースの呼び方（制度名／コース名）。"""
    grant, courses = _domain_labels(conv.get("domain_key") or "")
    course = courses.get(conv.get("course") or "", "")
    return "／".join(x for x in (grant, course) if x)


# 管理画面のナビゲーション項目（st.radio で切替。プログラムからの遷移に対応）
NAV_USERS = "ユーザー管理"
NAV_CONVERSATIONS = "会話履歴閲覧"
NAV_STATS = "利用統計"
NAV_OPTIONS = [NAV_USERS, NAV_CONVERSATIONS, NAV_STATS]

# 顧客番号の形式（C + 数字9桁）
CUSTOMER_NO_RE = re.compile(r"C\d{9}")


def _is_duplicate_error(e: Exception) -> bool:
    """顧客番号の一意制約違反かどうか。

    DB 側に部分一意インデックス idx_users_customer_no を張っているため、
    既に使われている番号を設定しようとすると例外になる。
    利用者には生のDBエラーではなく、原因が分かる文言を出す。
    """
    text = str(e)
    return "idx_users_customer_no" in text or "unique" in text.lower()


def render_admin_page():
    """管理画面のメインレンダリング関数（app.py から呼び出す）"""
    require_admin()

    # ── ヘッダー & 戻るボタン ──────────────────────────────────
    col_h1, col_h2 = st.columns([4, 1])
    with col_h1:
        st.markdown("<h2 class='admin-title'>管理画面</h2>", unsafe_allow_html=True)
    with col_h2:
        if st.button("アプリに戻る", use_container_width=True, key="admin_back"):
            st.session_state.app_state = "setup"
            st.rerun()

    # ビルド表記。デプロイが反映されているかを画面から判別するために出す。
    st.caption(
        f"ログイン中: {st.session_state.display_name}（管理者）　｜　ビルド: {BUILD_LABEL}"
    )
    st.divider()

    # ── ナビゲーション ──
    # 他ビュー（利用統計など）からのジャンプ要求があれば、ラジオ生成前に反映する
    if "admin_nav_pending" in st.session_state:
        st.session_state["admin_nav"] = st.session_state.pop("admin_nav_pending")

    nav = st.radio(
        "メニュー",
        NAV_OPTIONS,
        key="admin_nav",
        horizontal=True,
        label_visibility="collapsed",
    )

    if nav == NAV_USERS:
        _render_user_management()
    elif nav == NAV_CONVERSATIONS:
        _render_conversation_viewer()
    else:
        _render_usage_stats()


# =============================================================
# 顧客番号の一括取込（Excel の会社名 → 登録済み表示名 の突合）
# =============================================================
# 「株式会社◯◯（システム検索：株式会社 ◯◯）」のように、Excel 側に
# システム上の表記が併記されている場合があるため、その表記も突合キーに使う。
_SEARCH_HINT_RE = re.compile(r"[（(]\s*システム検索\s*[：:]\s*(.+?)\s*[）)]")


def _norm_company(name: str) -> str:
    """会社名の突合用キー。全角英数を半角化し、空白・記号ゆれを吸収する。"""
    t = unicodedata.normalize("NFKC", name or "")
    t = re.sub(r"[\s　]+", "", t)
    return t.casefold()


def _company_keys(raw_name: str) -> list[str]:
    """1行の会社名から、突合に使うキー候補を返す（完全一致優先の順）。"""
    keys = [raw_name.strip()]
    m = _SEARCH_HINT_RE.search(raw_name)
    if m:
        keys.append(m.group(1).strip())                       # 併記されたシステム上の表記
        keys.append(_SEARCH_HINT_RE.sub("", raw_name).strip())  # 併記部分を除いた表記
    return [k for k in keys if k]


def match_customer_numbers(rows: list[tuple[str, str]], users: list[dict]) -> dict:
    """Excel の (顧客番号, 会社名) と登録ユーザーを突合する。

    戻り値は判定済みの分類。DB へは書き込まない（呼び出し側が確認してから適用する）。
      exact      : 表記が完全に一致
      normalized : 全角半角・空白のゆれを吸収して一致（目視確認の対象）
      ambiguous  : Excel 側に同名の会社が複数あり、顧客番号を決められない
      no_match   : システムに登録があるが Excel に該当なし
      unused     : Excel にあるがシステムに該当なし（件数のみ使う想定）
    """
    exact_map: dict[str, set] = {}
    norm_map: dict[str, set] = {}
    for cno, name in rows:
        for k in _company_keys(name):
            exact_map.setdefault(k, set()).add(cno)
            norm_map.setdefault(_norm_company(k), set()).add(cno)

    result = {"exact": [], "normalized": [], "ambiguous": [], "no_match": [], "unused": 0}
    hit_numbers = set()

    for u in users:
        disp = (u.get("display_name") or "").strip()
        for table, kind in ((exact_map, "exact"), (norm_map, "normalized")):
            key = disp if kind == "exact" else _norm_company(disp)
            nums = table.get(key)
            if not nums:
                continue
            if len(nums) > 1:
                result["ambiguous"].append({"user": u, "candidates": sorted(nums)})
            else:
                cno = next(iter(nums))
                result[kind].append({"user": u, "customer_no": cno})
                hit_numbers.add(cno)
            break
        else:
            result["no_match"].append({"user": u})

    result["unused"] = len({c for c, _ in rows} - hit_numbers)
    return result


# =============================================================
# タブ1: ユーザー管理
# =============================================================
def _render_user_management():
    # ── 新規ユーザー追加 ──
    with st.expander("新しいユーザーを追加"):
        with st.form("add_user_form", clear_on_submit=True):
            new_customer_no  = st.text_input("顧客番号", placeholder="C000000000")
            new_display      = st.text_input("表示名")
            new_username     = st.text_input("ログインID（英数字）")
            new_password     = st.text_input("パスワード", type="password")
            new_is_admin     = st.checkbox("管理者権限を付与")
            add_submitted    = st.form_submit_button("追加", type="primary")

        if add_submitted:
            new_customer_no = (new_customer_no or "").strip()
            new_name = f"「{new_display or new_username}」（{new_username}）" if new_username else "新しいユーザー"
            if not new_username or not new_password:
                _flash("error", f"{new_name}を追加できませんでした。ログインIDとパスワードは必須です。")
            elif new_customer_no and not CUSTOMER_NO_RE.fullmatch(new_customer_no):
                _flash("error", f"{new_name}を追加できませんでした。顧客番号は「C」＋数字9桁で入力してください（例：C000000000）。")
            else:
                try:
                    create_user(new_username, new_display or new_username,
                                hash_password(new_password), new_is_admin,
                                customer_no=new_customer_no)
                    _flash("success", f"{new_name}を追加しました" + ("（管理者）。" if new_is_admin else "。"))
                except Exception as e:
                    if _is_duplicate_error(e):
                        _flash("error", f"{new_name}を追加できませんでした。同じログインIDか顧客番号が、既に他のアカウントで使われています。")
                    else:
                        _flash("error", f"{new_name}を追加できませんでした（{_short(e)}）。")
            st.rerun()

    # ── 顧客番号の一括取込 ──
    with st.expander("顧客番号を一括で取り込む（Excel）"):
        st.caption(
            "「顧客番号」「会社名」の2列を持つExcelを読み込み、"
            "会社名と登録済みの表示名が一致するユーザーに顧客番号を割り当てます。"
            "確認画面を挟むので、この時点ではまだ保存されません。"
        )
        up = st.file_uploader("Excelファイル", type=["xlsx", "xls"], key="cno_import_file")
        overwrite = st.checkbox(
            "既に顧客番号が入っているユーザーも上書きする", value=False, key="cno_overwrite"
        )

        if up is not None:
            try:
                import pandas as pd
                df = pd.read_excel(up, dtype=str).fillna("")
                df.columns = [str(c).strip() for c in df.columns]
                missing = [c for c in ("顧客番号", "会社名") if c not in df.columns]
                if missing:
                    st.error(f"必要な列がありません：{missing}　（現在の列：{list(df.columns)}）")
                else:
                    rows = [
                        (r["顧客番号"].strip(), r["会社名"].strip())
                        for _, r in df.iterrows()
                        if r["顧客番号"].strip() and r["会社名"].strip()
                    ]
                    bad_fmt = sorted({c for c, _ in rows if not CUSTOMER_NO_RE.fullmatch(c)})
                    if bad_fmt:
                        st.warning(
                            f"「C」＋数字9桁の形式でない顧客番号が {len(bad_fmt)} 件あります："
                            + "、".join(bad_fmt[:5]) + ("…" if len(bad_fmt) > 5 else "")
                        )

                    all_users = get_all_users()
                    res = match_customer_numbers(rows, all_users)

                    # 上書きしない設定なら、既に番号が入っているユーザーは対象から外す
                    def _targets(kind):
                        out = []
                        for x in res[kind]:
                            cur = (x["user"].get("customer_no") or "").strip()
                            if cur and not overwrite:
                                continue
                            if cur == x["customer_no"]:
                                continue  # 既に同じ番号なら更新不要
                            out.append(x)
                        return out

                    t_exact, t_norm = _targets("exact"), _targets("normalized")

                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("完全一致", len(res["exact"]))
                    m2.metric("表記ゆれ一致", len(res["normalized"]))
                    m3.metric("要確認", len(res["ambiguous"]))
                    m4.metric("該当なし", len(res["no_match"]))

                    if res["ambiguous"]:
                        st.error(
                            "Excel に同じ会社名が複数あり、顧客番号を決められないユーザーがいます。"
                            "これらは取り込まれません。個別に設定してください。"
                        )
                        st.dataframe(
                            [
                                {"表示名": x["user"]["display_name"],
                                 "候補の顧客番号": " / ".join(x["candidates"])}
                                for x in res["ambiguous"]
                            ],
                            use_container_width=True, hide_index=True,
                        )

                    if res["normalized"]:
                        st.warning(
                            "全角・半角や空白のゆれを吸収して一致させたものです。"
                            "別会社を取り違えていないか確認してください。"
                        )
                        st.dataframe(
                            [
                                {"表示名（システム）": x["user"]["display_name"],
                                 "顧客番号": x["customer_no"]}
                                for x in res["normalized"]
                            ],
                            use_container_width=True, hide_index=True,
                        )

                    if res["no_match"]:
                        # ここは既に expander の中なので、さらに expander を入れ子にできない
                        # （Streamlit の制約）。チェックボックスで開閉する。
                        if st.checkbox(
                            f"Excel に該当が無かった登録ユーザーを表示する（{len(res['no_match'])}件）",
                            key="cno_show_nomatch",
                        ):
                            st.dataframe(
                                [{"表示名": x["user"]["display_name"],
                                  "ログインID": x["user"]["username"]} for x in res["no_match"]],
                                use_container_width=True, hide_index=True,
                            )

                    st.caption(
                        f"Excel の {len(rows)} 行のうち、どの登録ユーザーにも割り当たらなかった顧客番号が "
                        f"{res['unused']} 件あります（未登録の会社と思われます）。"
                    )

                    total = len(t_exact) + len(t_norm)
                    st.divider()
                    if total == 0:
                        st.info("更新対象がありません。")
                    else:
                        st.write(f"**{total} 件**を更新します。")
                        if st.button("この内容で取り込む", type="primary", key="cno_apply"):
                            pairs = [(x["user"]["id"], x["customer_no"]) for x in t_exact + t_norm]
                            n = bulk_update_customer_no(pairs)
                            _flash("success", f"Excel から {n} 件の顧客番号を登録しました。")
                            st.rerun()
            except Exception as e:
                if _is_duplicate_error(e):
                    st.error(
                        "取り込もうとした顧客番号の中に、既に他のアカウントで"
                        "使われているものがあります。取込は行われていません。"
                    )
                else:
                    st.error(f"読み込みに失敗しました：{e}")

    st.divider()

    # ── ユーザー一覧 ──
    # 表1つで一覧し、行を選んだ1人にだけ操作を出す（2026-10-09 担当者決定）。
    # 以前は1人ごとに枠とボタン4つを作っていて、約370人で部品が約6,400個になり、
    # 管理画面を開くたび・検索の文字を打つたびに重かった。
    users = get_all_users()
    all_users = users
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    search = st.text_input(
        "会社名・ログインID・顧客番号で検索",
        key="user_mgmt_search",
        placeholder="社名・ID・顧客番号の一部を入力（空欄で全員表示）",
    )
    if search:
        s = search.lower()
        users = [
            u for u in users
            if s in u["display_name"].lower()
            or s in u["username"].lower()
            or s in (u.get("customer_no") or "").lower()
        ]
        if not users:
            st.warning("該当するユーザーが見つかりません。")
            return
    # 検索を変えたら、選んでいた人の選択を外す（表の行の位置が変わって別の人を指さないように）
    if st.session_state.get("um_last_search") != (search or ""):
        st.session_state["um_last_search"] = search or ""
        st.session_state.pop("um_selected_uid", None)

    import pandas as pd
    _show_flash()
    st.caption(f"{len(users)} 件（登録の新しい順。列の見出しを押すと並べ替えられます）　｜　表の行を選ぶと、下にその利用者の操作が出ます。")
    df = pd.DataFrame([{
        "表示名": u["display_name"],
        "ログインID": u["username"],
        "顧客番号": u.get("customer_no") or "未設定",
        "権限": "管理者" if u["is_admin"] else "一般",
        "状態": "有効" if u["is_active"] else "無効",
        "最終ログイン": _or_none(u.get("last_login_at")),
    } for u in users])
    event = st.dataframe(
        df, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="single-row",
        key=f"um_table_{_key_of(search)}", height=min(420, 38 + 35 * len(df)),
    )
    rows = _selected_rows(event)
    if rows:
        st.session_state["um_selected_uid"] = users[rows[0]]["id"]

    sel = next((u for u in users if u["id"] == st.session_state.get("um_selected_uid")), None)
    if sel:
        _render_user_actions(sel, all_users)


def _guard_reason(user: dict, op: str, active_admins: int) -> str:
    """その操作を止める理由（止めないときは空）。ボタンを押せなくし、押す直前にもう一度確かめる。
    op は "toggle"（無効にする）か "delete"。2026-10-09 担当者決定。"""
    if user["id"] == st.session_state.get("user_id"):
        return "自分のアカウントは無効・削除できません"
    if op == "delete" and user["is_admin"]:
        return "管理者のアカウントは、この画面から削除できません"
    if op == "toggle" and user["is_admin"] and user["is_active"] and active_admins <= 1:
        return "有効な管理者が1人だけのため、無効にできません"
    return ""


def _render_user_actions(user: dict, all_users: list):
    """選んだ1人の操作（顧客番号の変更・パスワードの変更・有効と無効の切り替え・削除）。"""
    uid = user["id"]
    who = _who(user)
    _cno = user.get("customer_no") or ""
    active_admins = sum(1 for u in all_users if u["is_admin"] and u["is_active"])
    toggle_block = _guard_reason(user, "toggle", active_admins) if user["is_active"] else ""
    delete_block = _guard_reason(user, "delete", active_admins)

    with st.container(border=True):
        # 顧客番号は管理画面にだけ出す（利用者側の画面には一切表示しない）
        st.markdown(
            f"**選んだ利用者：{user['display_name']}**　`{user['username']}`　"
            + (f"`{_cno}`" if _cno else ":orange[顧客番号 未設定]")
            + f"　｜　{'管理者' if user['is_admin'] else '一般'}・{'有効' if user['is_active'] else '無効'}"
            + f"・最終ログイン {_or_none(user.get('last_login_at'))}"
        )
        _show_flash(uid)
        b0, b1, b2, b3 = st.columns(4)
        with b0:
            if st.button("顧客番号を変更", key=f"cno_btn_{uid}", use_container_width=True):
                st.session_state[f"cno_edit_{uid}"] = True
        with b1:
            if st.button("パスワードを変更", key=f"pw_btn_{uid}", use_container_width=True):
                st.session_state[f"pw_edit_{uid}"] = True
        with b2:
            toggle_label = "無効にする" if user["is_active"] else "有効にする"
            if st.button(toggle_label, key=f"toggle_{uid}", use_container_width=True, disabled=bool(toggle_block)):
                # 押す直前にもう一度確かめる（ほかの管理者が同時に操作した場合など）
                fresh = {u["id"]: u for u in get_all_users()}
                now_admins = sum(1 for u in fresh.values() if u["is_admin"] and u["is_active"])
                reason = _guard_reason(fresh.get(uid, user), "toggle", now_admins) if user["is_active"] else ""
                if reason:
                    _flash("error", f"{who}を無効にできませんでした。{reason}。", uid)
                else:
                    try:
                        set_user_active(uid, not bool(user["is_active"]))
                        _flash("success", f"{who}を{'無効' if user['is_active'] else '有効'}にしました。", uid)
                    except Exception as e:
                        _flash("error", f"{who}の有効・無効を切り替えられませんでした（{_short(e)}）。", uid)
                st.rerun()
        with b3:
            if st.button("削除", key=f"del_{uid}", use_container_width=True, disabled=bool(delete_block)):
                st.session_state[f"del_confirm_{uid}"] = True
        # 押せない理由を、ボタンのすぐ下に短く出す
        reasons = list(dict.fromkeys(r for r in (toggle_block, delete_block) if r))
        if reasons:
            st.caption("押せない操作があります：" + "／".join(reasons))

        # 顧客番号の編集フォーム（展開時）
        if st.session_state.get(f"cno_edit_{uid}"):
            with st.form(f"cno_form_{uid}"):
                edit_cno = st.text_input(
                    "顧客番号", value=_cno, placeholder="C000000000",
                    help="空欄で保存すると未設定に戻します。",
                )
                cno_ok = st.form_submit_button("保存する")
            if cno_ok:
                edit_cno = (edit_cno or "").strip()
                if edit_cno and not CUSTOMER_NO_RE.fullmatch(edit_cno):
                    _flash("error", f"{who}の顧客番号を変更できませんでした。顧客番号は「C」＋数字9桁で入力してください（例：C000000000）。", uid)
                else:
                    try:
                        update_customer_no(uid, edit_cno)
                    except Exception as e:
                        if _is_duplicate_error(e):
                            _flash("error", f"{who}の顧客番号を変更できませんでした。その番号は既に他のアカウントで使われています（顧客番号は1社につき1つです）。", uid)
                        else:
                            _flash("error", f"{who}の顧客番号を変更できませんでした（{_short(e)}）。", uid)
                    else:
                        st.session_state.pop(f"cno_edit_{uid}", None)
                        _flash("success", f"{who}の顧客番号を" + (f"「{edit_cno}」に変更しました。" if edit_cno else "未設定に戻しました。"), uid)
                st.rerun()

        # パスワード変更フォーム（展開時）
        if st.session_state.get(f"pw_edit_{uid}"):
            with st.form(f"pw_form_{uid}"):
                new_pw = st.text_input("新しいパスワード", type="password")
                pw_ok = st.form_submit_button("変更する")
            if pw_ok:
                if not new_pw:
                    _flash("error", f"{who}のパスワードを変更できませんでした。新しいパスワードを入力してください。", uid)
                else:
                    try:
                        update_password(uid, hash_password(new_pw))
                        st.session_state.pop(f"pw_edit_{uid}", None)
                        _flash("success", f"{who}のパスワードを変更しました。", uid)
                    except Exception as e:
                        _flash("error", f"{who}のパスワードを変更できませんでした（{_short(e)}）。", uid)
                st.rerun()

        # 削除確認（展開時）。取り消せない操作なので、確認のあとにだけ削除する
        if st.session_state.get(f"del_confirm_{uid}"):
            st.warning(f"「{user['display_name']}」を削除します。会話履歴もすべて削除され、元に戻せません。本当によろしいですか？")
            d1, d2 = st.columns(2)
            with d1:
                if st.button("削除する", key=f"del_yes_{uid}", type="primary", use_container_width=True):
                    fresh = {u["id"]: u for u in get_all_users()}
                    reason = _guard_reason(fresh[uid], "delete", 0) if uid in fresh else ""
                    st.session_state.pop(f"del_confirm_{uid}", None)
                    if reason:
                        _flash("error", f"{who}を削除できませんでした。{reason}。", uid)
                    else:
                        try:
                            delete_user(uid)
                            st.session_state.pop("um_selected_uid", None)
                            _flash("success", f"{who}を削除しました（会話履歴も削除しました）。")
                        except Exception as e:
                            _flash("error", f"{who}を削除できませんでした（{_short(e)}）。", uid)
                    st.rerun()
            with d2:
                if st.button("キャンセル", key=f"del_no_{uid}", use_container_width=True):
                    st.session_state.pop(f"del_confirm_{uid}", None)
                    _flash("info", f"{who}の削除をやめました（削除していません）。", uid)
                    st.rerun()
        elif not (st.session_state.get(f"cno_edit_{uid}") or st.session_state.get(f"pw_edit_{uid}")):
            st.caption("顧客番号・パスワードは、押すとこの下に入力欄が出ます。削除は確認のあとに行います。")


# =============================================================
# タブ2: 会話履歴閲覧
# =============================================================
# 会話の一覧（2026-10-09 担当者決定・案A）：横に長い表をやめ、1行に「最後のやり取り／様式（下に制度・コースと年度）
# ／やり取りの件数／開く」を並べる。様式名は折り返し、横には動かない。15件ずつページを送る。
# 開いている会話は行に色を付け、ページを送っても下の表示は消えない（会話の番号を利用者ごとに覚えておく）。
CONV_PAGE_SIZE = 15

_CONV_LIST_CSS = """<style>
[class*="st-key-cvrow_"] { border-bottom: 1px solid var(--line-soft); padding: .35rem .5rem; border-radius: 6px; }
[class*="st-key-cvrow_"] [data-testid="stMarkdownContainer"] p { margin: 0; }
.st-key-cvrow_open { background: var(--navy-tint); border-left: 4px solid var(--navy); }
.cv-dt { font-size: .84rem; white-space: nowrap; color: var(--ink); }
.cv-form { font-size: .86rem; color: var(--ink); overflow-wrap: anywhere; }
.cv-sub { display: block; font-size: .74rem; color: var(--ink-muted); margin-top: .1rem; overflow-wrap: anywhere; }
.cv-n { font-size: .84rem; white-space: nowrap; color: var(--ink-sub); }
.cv-head { font-size: .78rem; color: var(--ink-sub); font-weight: 600; padding: 0 .5rem .2rem; }
</style>"""


def _render_conv_list(convs: list, uid) -> dict | None:
    """会話の一覧を出し、開いている会話（無ければ None）を返す。"""
    open_key, page_key = f"cv_open_{uid}", f"cv_page_{uid}"
    ids = [c["id"] for c in convs]
    if st.session_state.get(open_key) not in ids:
        st.session_state.pop(open_key, None)
    n_pages = max(1, (len(convs) + CONV_PAGE_SIZE - 1) // CONV_PAGE_SIZE)
    page = min(max(0, int(st.session_state.get(page_key, 0))), n_pages - 1)
    start = page * CONV_PAGE_SIZE
    shown = convs[start:start + CONV_PAGE_SIZE]
    open_id = st.session_state.get(open_key)

    st.markdown(_CONV_LIST_CSS, unsafe_allow_html=True)
    st.caption(
        "やり取りが1つもない会話（様式を開いただけのもの）は出していません。新しい順です。"
        f"　全 {len(convs)} 件中 {start + 1}〜{start + len(shown)} 件目"
    )
    if n_pages > 1:
        p1, p2, p3 = st.columns([1, 2, 1], vertical_alignment="center")
        with p1:
            if st.button(f"◀ 前の{CONV_PAGE_SIZE}件", key=f"cv_prev_{uid}", use_container_width=True, disabled=page == 0):
                st.session_state[page_key] = page - 1
                st.rerun()
        p2.markdown(f"<div style='text-align:center;font-size:.82rem;color:var(--ink-sub)'>{page + 1} / {n_pages} ページ</div>",
                    unsafe_allow_html=True)
        with p3:
            if st.button(f"次の{CONV_PAGE_SIZE}件 ▶", key=f"cv_next_{uid}", use_container_width=True, disabled=page >= n_pages - 1):
                st.session_state[page_key] = page + 1
                st.rerun()

    h1, h2, h3, h4 = st.columns([1.6, 6, 1, 1.2])
    h1.markdown("<div class='cv-head'>最後のやり取り</div>", unsafe_allow_html=True)
    h2.markdown("<div class='cv-head'>様式（下に制度・コースと年度）</div>", unsafe_allow_html=True)
    h3.markdown("<div class='cv-head'>やり取り</div>", unsafe_allow_html=True)
    for c in shown:
        is_open = (c["id"] == open_id)
        with st.container(key="cvrow_open" if is_open else f"cvrow_{c['id']}"):
            r1, r2, r3, r4 = st.columns([1.6, 6, 1, 1.2], vertical_alignment="center")
            r1.markdown(f"<span class='cv-dt'>{html.escape((c.get('last_message_at') or '')[:16])}</span>", unsafe_allow_html=True)
            r2.markdown(
                f"<span class='cv-form'>{html.escape(_conv_form_label(c))}</span>"
                f"<span class='cv-sub'>{html.escape(_conv_domain_label(c))}・{html.escape(_year_label(c.get('app_year')))}</span>",
                unsafe_allow_html=True)
            r3.markdown(f"<span class='cv-n'>{c.get('n_messages') or 0}件</span>", unsafe_allow_html=True)
            with r4:
                if st.button("開いている" if is_open else "開く", key=f"cv_open_btn_{c['id']}",
                             type="primary" if is_open else "secondary", use_container_width=True):
                    st.session_state[open_key] = c["id"]
                    st.rerun()

    if open_id is None:
        st.caption("「開く」を押すと、その会話のやり取りがこの下に出ます。")
        return None
    conv = next(c for c in convs if c["id"] == open_id)
    pos = ids.index(open_id)
    if not (start <= pos < start + CONV_PAGE_SIZE):
        st.caption(f"開いている会話は、この一覧の {pos // CONV_PAGE_SIZE + 1} ページ目にあります。")
    return conv


def _render_conversation_viewer():
    # 2026-10-09 作り直し：中身のある会話（質問か添削をした会話）だけを、最後のやり取りが新しい順に出す。
    # 様式はファイル名ではなく相談の画面と同じ呼び方、制度はコース名まで出す。
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return
    stats = {s["id"]: s for s in get_all_user_stats()}
    user_by_id = {u["id"]: u for u in users}

    # ── 会社名・ID・顧客番号で検索 ──
    search = st.text_input(
        "会社名・ログインID・顧客番号で検索",
        key="conv_search",
        placeholder="社名・ID・顧客番号の一部を入力（空欄で全員表示）",
    )
    only_with = st.toggle("会話がある利用者だけを出す", value=True, key="conv_only_with")

    # 利用統計から移ってきたときの行き先（ウィジェットを作る前に受け取る）
    jump_id = st.session_state.pop("conv_target_user_id", None)

    filtered = users
    if search:
        s = search.lower()
        filtered = [
            u for u in filtered
            if s in u["display_name"].lower() or s in u["username"].lower()
            or s in (u.get("customer_no") or "").lower()
        ]
    if only_with:
        filtered = [u for u in filtered if (stats.get(u["id"]) or {}).get("total_conversations")
                    or u["id"] == jump_id]
    # 最近やり取りした人を上に
    filtered = sorted(filtered, key=lambda u: (stats.get(u["id"]) or {}).get("last_message_at") or "", reverse=True)

    if not filtered:
        st.warning("該当するユーザーが見つかりません。検索条件や「会話がある利用者だけを出す」を変えてください。")
        return

    option_ids = [u["id"] for u in filtered]
    if jump_id is not None and jump_id in option_ids:
        st.session_state["conv_user_select"] = jump_id
    # 保存済みの選択が現在の候補に無ければリセット（検索で絞られた場合など）
    if st.session_state.get("conv_user_select") not in option_ids:
        st.session_state.pop("conv_user_select", None)

    def _user_label(uid):
        u, s = user_by_id[uid], stats.get(uid) or {}
        n = s.get("total_conversations") or 0
        tail = f"会話 {n}件・最後のやり取り {(s.get('last_message_at') or '')[:10]}" if n else "会話なし"
        return f"{u['display_name']}（{u['username']}）— {tail}"

    selected_id = st.selectbox("利用者を選択", options=option_ids, format_func=_user_label, key="conv_user_select")

    convs = get_nonempty_conversations_by_user(selected_id)
    if not convs:
        st.info("この利用者には、やり取りのある会話がありません。")
        return

    conv = _render_conv_list(convs, selected_id)
    if not conv:
        return

    st.markdown(
        f"**{_conv_form_label(conv)}**　｜　{_conv_domain_label(conv)}　｜　{_year_label(conv.get('app_year'))}　｜　"
        f"始めた日時 {(conv.get('created_at') or '')[:16]}・最後のやり取り {(conv.get('last_message_at') or '')[:16]}"
        f"・やり取り {conv.get('n_messages') or 0}件"
    )
    st.divider()

    # 相談の画面と同じ表示のしかた（区切りの行は線で、太字は同じ直しをかけて）
    for msg in get_messages_by_conversation(conv["id"]):
        content = msg.get("content") or ""
        if _is_divider(content):
            st.markdown(f"<div class='form-switch-note'>{html.escape(_divider_text(content))}</div>",
                        unsafe_allow_html=True)
            continue
        with st.chat_message(msg["role"]):
            st.markdown(_md(content))
            st.caption(msg["created_at"])


# =============================================================
# タブ3: 利用統計
# =============================================================
def _render_usage_stats():
    try:
        import pandas as pd
    except ImportError:
        st.error("pandas がインストールされていません。`pip install pandas` を実行してください。")
        return

    stats = get_all_user_stats()
    if not stats:
        st.info("データがありません。")
        return

    # ── 並び替えUI ──
    # 「最終会話順」は最後に質問を送った日時の順（2026-10-09 追加）。会話数は、やり取り（質問か添削）のある会話だけを数える
    SORT_OPTIONS = {
        "最終会話順（最後に質問を送った日時）": "last_question_at",
        "登録日順":             None,
        "表示名順":             "display_name",
        "最終ログイン順":       "last_login_at",
        "会話数順":             "total_conversations",
        "メッセージ数順":       "total_messages",
    }
    c1, c2 = st.columns([3, 1])
    with c1:
        sort_label = st.selectbox("並び替え", list(SORT_OPTIONS.keys()), key="stats_sort_key")
    with c2:
        descending = st.toggle("新しい順・多い順", value=True, key="stats_sort_desc")

    sort_field = SORT_OPTIONS[sort_label]
    stats_sorted = list(stats)
    if sort_field in ("total_conversations", "total_messages"):
        stats_sorted.sort(key=lambda s: s.get(sort_field) or 0, reverse=descending)
    elif sort_field:
        stats_sorted.sort(key=lambda s: str(s.get(sort_field) or ""), reverse=descending)

    # 表示順に対応するユーザーIDリスト（行選択→ジャンプ用）
    uids = [s["id"] for s in stats_sorted]

    df = pd.DataFrame(stats_sorted)
    df = df.rename(columns={
        "username":            "ログインID",
        "display_name":        "表示名",
        "is_active":           "状態",
        "last_question_at":    "最後に質問を送った日時",
        "last_login_at":       "最終ログイン",
        "total_conversations": "会話数",
        "total_messages":      "メッセージ数",
    })
    df["状態"] = df["状態"].map({1: "有効", 0: "無効"})
    # 記録が無い欄は「None」ではなく「記録なし」と出す
    for col in ("最後に質問を送った日時", "最終ログイン"):
        df[col] = df[col].map(_or_none)
    df = df.drop(columns=["id"], errors="ignore")
    df = df[["表示名", "ログインID", "状態", "最後に質問を送った日時", "最終ログイン", "会話数", "メッセージ数"]]

    st.caption("行を選択すると、その会社の会話履歴へ移動できます。会話数は、やり取り（質問か添削）のある会話だけの数です。記録がない欄は「記録なし」と出ます。")
    event = st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    sel = getattr(event, "selection", None)
    rows = list(sel.rows) if sel and getattr(sel, "rows", None) else []
    if rows:
        pos = rows[0]
        target_uid = uids[pos]
        target_name = stats_sorted[pos]["display_name"]
        if st.button(
            f"「{target_name}」の会話履歴に移動する",
            type="primary",
            use_container_width=True,
        ):
            st.session_state["admin_nav_pending"] = NAV_CONVERSATIONS
            st.session_state["conv_target_user_id"] = target_uid
            st.rerun()

    _render_system_info()


# =============================================================
# 利用統計タブの末尾: 運用確認用のシステム情報
# =============================================================
def _render_system_info():
    st.divider()
    st.markdown("<h3 class='admin-title'>システム情報</h3>", unsafe_allow_html=True)

    # ── 顧客番号の設定状況 ──
    st.markdown("**顧客番号の設定状況**")
    try:
        summary = get_customer_no_summary()
        c1, c2, c3 = st.columns(3)
        c1.metric("アカウント総数", summary["total"])
        c2.metric("顧客番号あり", summary["with_no"])
        c3.metric("顧客番号なし", summary["without_no"])
    except Exception as e:
        st.error(f"取得に失敗しました：{e}")

    # ── 顧客番号の重複（SSO でアカウントを特定できなくなるため事前に潰す）──
    try:
        dups = get_customer_no_duplicates()
        if dups:
            st.error(
                f"同じ顧客番号のアカウントが {len(dups)} 組あります。"
                "SSO はこの番号でログイン先を決めるため、重複したままでは特定できません。"
                "どちらかを修正してください。"
            )
            st.dataframe(
                [{"顧客番号": d["customer_no"], "件数": d["cnt"], "該当アカウント": d["accounts"]}
                 for d in dups],
                use_container_width=True, hide_index=True,
            )
        else:
            st.success("顧客番号の重複はありません。")
    except Exception as e:
        st.error(f"重複チェックに失敗しました：{e}")

    # ── 年度別の利用状況 ──
    st.markdown("")
    st.markdown("**年度別の利用状況**")
    st.caption("旧年度版がどの程度使われているかの確認用です。")
    try:
        rows = get_conversation_counts_by_year()
        if rows:
            st.dataframe(
                [{"年度": _year_label(r["app_year"]),
                  "会話数": r["conversations"],
                  "メッセージ数": r["messages"],
                  "最終利用日": (r["last_used"] or "")[:10]} for r in rows],
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("会話データがありません。")
    except Exception as e:
        st.error(f"取得に失敗しました：{e}")
