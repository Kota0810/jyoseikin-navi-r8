"""
auth.py  –  認証ロジック（bcrypt + セッション検証）
"""
import bcrypt
import streamlit as st
from db import get_user_by_username, update_last_login


# =============================================================
# パスワードハッシュ
# =============================================================
def hash_password(plain: str) -> str:
    """平文パスワードを bcrypt ハッシュ化して文字列で返す"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """ログイン時のパスワード照合"""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# =============================================================
# ログイン・ログアウト
# =============================================================
def login(username: str, password: str) -> dict | None:
    """
    認証成功: ユーザー辞書を返す
    失敗（ユーザー不存在・PW不一致・無効化済み）: None を返す
    """
    user = get_user_by_username(username)
    if not user:
        return None
    if not user["is_active"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    update_last_login(user["id"])
    return user


def logout() -> None:
    """session_state の認証情報をすべてクリアしてログイン画面へ"""
    for key in ["authenticated", "user_id", "display_name", "is_admin",
                "current_conv_id", "messages", "app_state",
                "selected_domain_key", "selected_grant", "selected_form",
                "review_result", "pending_item"]:
        if key in st.session_state:
            del st.session_state[key]
    st.session_state["app_state"] = "login"


# =============================================================
# ガード関数
# =============================================================
def require_login() -> None:
    """未認証なら login 状態へリダイレクト（各ページ先頭で呼び出す）"""
    if not st.session_state.get("authenticated"):
        st.session_state["app_state"] = "login"
        st.rerun()


def require_admin() -> None:
    """管理者以外は処理を停止する"""
    require_login()
    if not st.session_state.get("is_admin"):
        st.error("⛔ 管理者権限が必要です。")
        st.stop()
