"""
admin.py  –  管理画面 UI（ユーザー管理・会話履歴閲覧・利用統計）
"""
import streamlit as st
from auth import require_admin, hash_password
import re
import unicodedata

from version import BUILD_LABEL
from db import (
    get_all_users, create_user, update_password, set_user_active, delete_user,
    update_customer_no, bulk_update_customer_no,
    get_customer_no_duplicates, get_customer_no_summary,
    get_conversation_counts_by_year,
    get_all_user_stats,
    get_all_conversations_by_user, get_messages_by_conversation,
)


def _year_label(app_year: str) -> str:
    return {"R7": "令和7年度版", "R8": "令和8年度版"}.get(app_year or "R7", app_year or "R7")


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
            if not new_username or not new_password:
                st.error("ログインIDとパスワードは必須です。")
            elif new_customer_no and not CUSTOMER_NO_RE.fullmatch(new_customer_no):
                st.error("顧客番号は「C」＋数字9桁で入力してください（例：C000000000）。")
            else:
                try:
                    create_user(new_username, new_display or new_username,
                                hash_password(new_password), new_is_admin,
                                customer_no=new_customer_no)
                    st.success(f"ユーザー「{new_username}」を追加しました。")
                    st.rerun()
                except Exception as e:
                    if _is_duplicate_error(e):
                        st.error(
                            f"顧客番号「{new_customer_no}」は既に他のアカウントで使われています。"
                            "顧客番号は1社につき1つです。"
                        )
                    else:
                        st.error(f"追加失敗：{e}")

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
                            st.success(f"{n} 件の顧客番号を登録しました。")
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
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    # 会社名・IDで検索
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

    for user in users:
        uid = user["id"]
        with st.container(border=True):
            # ボタンが4つあるため、右側の取り分を広めにする
            # （狭いと日本語ラベルが1文字ずつ縦に折り返される）
            c1, c2, c3, c4 = st.columns([4, 1.2, 1.2, 4.6])
            # 顧客番号は管理画面にだけ出す（利用者側の画面には一切表示しない）
            _cno = user.get("customer_no") or ""
            c1.markdown(
                f"**{user['display_name']}**  \n`{user['username']}`"
                + (f"  \n`{_cno}`" if _cno else "  \n:orange[顧客番号 未設定]")
            )
            c2.write("管理者" if user["is_admin"] else "一般")
            c3.write("有効" if user["is_active"] else "無効")

            with c4:
                btn_col0, btn_col1, btn_col2, btn_col3 = st.columns(4)

                # 顧客番号の修正（一括取込の結果を個別に直せるようにしておく）
                with btn_col0:
                    if st.button("顧客番号", key=f"cno_btn_{uid}", use_container_width=True):
                        st.session_state[f"cno_edit_{uid}"] = True

                # パスワード変更
                with btn_col1:
                    if st.button("PW変更", key=f"pw_btn_{uid}", use_container_width=True):
                        st.session_state[f"pw_edit_{uid}"] = True

                # 有効/無効切替
                with btn_col2:
                    toggle_label = "無効化" if user["is_active"] else "有効化"
                    if st.button(toggle_label, key=f"toggle_{uid}", use_container_width=True):
                        set_user_active(uid, not bool(user["is_active"]))
                        st.rerun()

                # 削除
                with btn_col3:
                    if st.button("削除", key=f"del_{uid}", use_container_width=True,
                                 type="primary" if False else "secondary"):
                        st.session_state[f"del_confirm_{uid}"] = True

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
                        st.error("顧客番号は「C」＋数字9桁で入力してください（例：C000000000）。")
                    else:
                        try:
                            update_customer_no(uid, edit_cno)
                        except Exception as e:
                            if _is_duplicate_error(e):
                                st.error(
                                    f"顧客番号「{edit_cno}」は既に他のアカウントで使われています。"
                                    "顧客番号は1社につき1つです。"
                                )
                            else:
                                st.error(f"更新に失敗しました：{e}")
                        else:
                            st.session_state.pop(f"cno_edit_{uid}", None)
                            st.success("顧客番号を更新しました。")
                            st.rerun()

            # パスワード変更フォーム（展開時）
            if st.session_state.get(f"pw_edit_{uid}"):
                with st.form(f"pw_form_{uid}"):
                    new_pw = st.text_input("新しいパスワード", type="password")
                    pw_ok  = st.form_submit_button("変更する")
                if pw_ok:
                    if new_pw:
                        update_password(uid, hash_password(new_pw))
                        st.session_state.pop(f"pw_edit_{uid}", None)
                        st.success("パスワードを変更しました。")
                        st.rerun()
                    else:
                        st.error("パスワードを入力してください。")

            # 削除確認（展開時）
            if st.session_state.get(f"del_confirm_{uid}"):
                st.warning(f"「{user['display_name']}」を削除します。会話履歴も全て削除されます。本当によろしいですか？")
                d1, d2 = st.columns(2)
                with d1:
                    if st.button("削除する", key=f"del_yes_{uid}", type="primary", use_container_width=True):
                        delete_user(uid)
                        st.session_state.pop(f"del_confirm_{uid}", None)
                        st.success("削除しました。")
                        st.rerun()
                with d2:
                    if st.button("キャンセル", key=f"del_no_{uid}", use_container_width=True):
                        st.session_state.pop(f"del_confirm_{uid}", None)
                        st.rerun()


# =============================================================
# タブ2: 会話履歴閲覧
# =============================================================
def _render_conversation_viewer():
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    user_by_id = {u["id"]: u for u in users}

    # ── 会社名・IDで検索 ──
    search = st.text_input(
        "会社名・ログインIDで検索",
        key="conv_search",
        placeholder="社名やIDの一部を入力（空欄で全員表示）",
    )
    if search:
        s = search.lower()
        filtered = [
            u for u in users
            if s in u["display_name"].lower() or s in u["username"].lower()
        ]
    else:
        filtered = users

    if not filtered:
        st.warning("該当するユーザーが見つかりません。検索条件を変えてください。")
        return

    option_ids = [u["id"] for u in filtered]

    # ── 利用統計からのジャンプ先ユーザーを選択状態にする（ウィジェット生成前）──
    jump_id = st.session_state.pop("conv_target_user_id", None)
    if jump_id is not None and jump_id in option_ids:
        st.session_state["conv_user_select"] = jump_id
    # 保存済みの選択が現在の候補に無ければリセット（検索で絞られた場合など）
    if st.session_state.get("conv_user_select") not in option_ids:
        st.session_state.pop("conv_user_select", None)

    selected_id = st.selectbox(
        "ユーザーを選択",
        options=option_ids,
        format_func=lambda uid: f"{user_by_id[uid]['display_name']} ({user_by_id[uid]['username']})",
        key="conv_user_select",
    )
    selected_user = user_by_id[selected_id]

    convs = get_all_conversations_by_user(selected_user["id"], limit=100)
    if not convs:
        st.info("このユーザーの会話履歴はありません。")
        return

    selected_conv = st.selectbox(
        "会話を選択",
        options=convs,
        format_func=lambda c: f"[{_year_label(c.get('app_year'))}] {c['title']}　（{c['updated_at'][:10]}）",
    )

    if not selected_conv:
        return

    st.caption(
        f"年度: {_year_label(selected_conv.get('app_year'))}　"
        f"制度: {selected_conv['domain_key']}　様式: {selected_conv['form_name']}"
    )
    st.divider()

    messages = get_messages_by_conversation(selected_conv["id"])
    if not messages:
        st.info("メッセージがありません。")
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
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
    SORT_OPTIONS = {
        "登録日順（デフォルト）": None,
        "表示名順":             "display_name",
        "最終ログイン順":       "last_login_at",
        "会話数順":             "total_conversations",
        "メッセージ数順":       "total_messages",
    }
    c1, c2 = st.columns([3, 1])
    with c1:
        sort_label = st.selectbox("並び替え", list(SORT_OPTIONS.keys()), key="stats_sort_key")
    with c2:
        descending = st.toggle("降順", value=True, key="stats_sort_desc")

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
        "is_active":           "有効",
        "last_login_at":       "最終ログイン",
        "total_conversations": "会話数",
        "total_messages":      "メッセージ数",
    })
    df["有効"] = df["有効"].map({1: "有効", 0: "無効"})
    df = df.drop(columns=["id"], errors="ignore")
    df = df[["表示名", "ログインID", "有効", "最終ログイン", "会話数", "メッセージ数"]]

    st.caption("行を選択すると、その会社の会話履歴へ移動できます。")
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
