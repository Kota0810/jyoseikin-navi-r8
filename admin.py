"""
admin.py  –  管理画面 UI（ユーザー管理・会話履歴閲覧・利用統計）
"""
import streamlit as st
from auth import require_admin, hash_password
from db import (
    get_all_users, create_user, update_password, set_user_active, delete_user,
    get_all_user_stats,
    get_all_conversations_by_user, get_messages_by_conversation,
)


def _year_label(app_year: str) -> str:
    return {"R7": "令和7年度版", "R8": "令和8年度版"}.get(app_year or "R7", app_year or "R7")


def render_admin_page():
    """管理画面のメインレンダリング関数（app.py から呼び出す）"""
    require_admin()

    # ── ヘッダー & 戻るボタン ──────────────────────────────────
    col_h1, col_h2 = st.columns([4, 1])
    with col_h1:
        st.markdown("## 🔧 管理画面")
    with col_h2:
        if st.button("← アプリに戻る", use_container_width=True):
            st.session_state.app_state = "setup"
            st.rerun()

    st.caption(f"ログイン中: {st.session_state.display_name}（管理者）")
    st.divider()

    tab1, tab2, tab3 = st.tabs(["👥 ユーザー管理", "💬 会話履歴閲覧", "📊 利用統計"])

    # ==========================================================
    # タブ1: ユーザー管理
    # ==========================================================
    with tab1:
        _render_user_management()

    # ==========================================================
    # タブ2: 会話履歴閲覧
    # ==========================================================
    with tab2:
        _render_conversation_viewer()

    # ==========================================================
    # タブ3: 利用統計
    # ==========================================================
    with tab3:
        _render_usage_stats()


# =============================================================
# タブ1: ユーザー管理
# =============================================================
def _render_user_management():
    # ── 新規ユーザー追加 ──
    with st.expander("＋ 新しいユーザーを追加"):
        with st.form("add_user_form", clear_on_submit=True):
            new_username     = st.text_input("ログインID（英数字）")
            new_display      = st.text_input("表示名")
            new_password     = st.text_input("パスワード", type="password")
            new_is_admin     = st.checkbox("管理者権限を付与")
            add_submitted    = st.form_submit_button("追加", type="primary")

        if add_submitted:
            if not new_username or not new_password:
                st.error("ログインIDとパスワードは必須です。")
            else:
                try:
                    create_user(new_username, new_display or new_username,
                                hash_password(new_password), new_is_admin)
                    st.success(f"✅ ユーザー「{new_username}」を追加しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"追加失敗：{e}")

    st.divider()

    # ── ユーザー一覧 ──
    users = get_all_users()
    if not users:
        st.info("ユーザーが登録されていません。")
        return

    for user in users:
        uid = user["id"]
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 2, 2, 3])
            c1.markdown(f"**{user['display_name']}**  \n`{user['username']}`")
            c2.write("👑 管理者" if user["is_admin"] else "一般")
            c3.write("✅ 有効" if user["is_active"] else "⛔ 無効")

            with c4:
                btn_col1, btn_col2, btn_col3 = st.columns(3)

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

    selected_user = st.selectbox(
        "ユーザーを選択",
        options=users,
        format_func=lambda u: f"{u['display_name']} ({u['username']})",
    )

    if not selected_user:
        return

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

    df = pd.DataFrame(stats)
    df = df.rename(columns={
        "username":            "ログインID",
        "display_name":        "表示名",
        "is_active":           "有効",
        "last_login_at":       "最終ログイン",
        "total_conversations": "会話数",
        "total_messages":      "メッセージ数",
    })
    df["有効"] = df["有効"].map({1: "✅", 0: "⛔"})
    df = df.drop(columns=["id"], errors="ignore")

    st.dataframe(df, use_container_width=True, hide_index=True)
