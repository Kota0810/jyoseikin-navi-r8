"""
sso.py  –  他システムからの署名付きトークン（JWT）によるログイン。

【方式】一方向SSO。発行側が秘密鍵で署名し、こちらは公開鍵で検証するだけ。
こちらから発行側へは何も送らないため、こちらが侵害されても発行側には波及しない。
保持するのは公開鍵のみで、公開鍵は漏れても偽造には使えない。

【設定】.streamlit/secrets.toml（Streamlit Cloud では Secrets）に置く。
未設定のあいだは SSO が無効になるだけで、通常のログインには影響しない。

    [sso]
    issuer     = "nss-liboffice-cloud"
    audience   = "jyoseikin-navi"
    leeway     = 30                       # 時刻ずれの許容秒数
    return_url = "https://.../user/funding/ai-agent/sso"   # 期限切れ時の戻り先

    [sso.public_keys]                     # kid -> 公開鍵PEM
    "2026-08-key1" = '''-----BEGIN PUBLIC KEY-----
    ...
    -----END PUBLIC KEY-----'''
"""
import os
import re
from datetime import datetime, timezone

import streamlit as st

from db import consume_jti, get_user_by_customer_no

# 顧客番号の形式（C + 数字9桁）。発行側と受け側で同じガードをかける。
CUSTOMER_NO_RE = re.compile(r"C\d{9}")

# 署名アルゴリズムは公開鍵方式のみ許可する。
# HS256（共通鍵）を混ぜると、公開鍵を鍵とみなした署名偽造が成立するため
# 明示的に締める。
ALLOWED_ALGORITHMS = ["RS256", "EdDSA"]

# 失敗理由。画面の文言と対応づける。
E_NOT_CONFIGURED = "not_configured"   # 公開鍵が未設定
E_BAD_TOKEN      = "bad_token"        # 署名・形式・iss/aud が不正
E_EXPIRED        = "expired"          # 有効期限切れ（コールドスタート時に起きうる）
E_REPLAYED       = "replayed"         # 使用済みトークンの再提示
E_NO_ACCOUNT     = "no_account"       # 顧客番号に対応するアカウントが無い
E_NOT_ALLOWED    = "not_allowed"      # 管理者／無効化アカウント


def _config() -> dict:
    """secrets の [sso] を読む。無ければ空の辞書。"""
    try:
        cfg = dict(st.secrets.get("sso", {}))
    except Exception:
        cfg = {}
    if not cfg.get("issuer"):
        cfg["issuer"] = os.getenv("SSO_ISSUER", "")
    if not cfg.get("audience"):
        cfg["audience"] = os.getenv("SSO_AUDIENCE", "")
    return cfg


def public_keys() -> dict:
    """kid -> 公開鍵PEM の対応表。鍵の入れ替えを無停止で行うため複数持てる。"""
    cfg = _config()
    try:
        return {str(k): str(v) for k, v in dict(cfg.get("public_keys", {})).items()}
    except Exception:
        return {}


def is_enabled() -> bool:
    cfg = _config()
    return bool(public_keys() and cfg.get("issuer") and cfg.get("audience"))


def return_url() -> str:
    """期限切れ画面から戻る先（発行側のSSO開始URL）。"""
    return str(_config().get("return_url", "") or "")


def _verify(token: str):
    """署名とクレームを検証する。DBには触れない。

    戻り値: (payload, None) または (None, 失敗理由)
    """
    keys = public_keys()
    cfg = _config()
    if not keys or not cfg.get("issuer") or not cfg.get("audience"):
        return None, E_NOT_CONFIGURED

    try:
        import jwt
    except ImportError:
        return None, E_NOT_CONFIGURED

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:
        return None, E_BAD_TOKEN

    key = keys.get(str(kid))
    if not key:
        # 未知の kid。失効させた試験用鍵などがここに落ちる。
        return None, E_BAD_TOKEN

    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=cfg["audience"],
            issuer=cfg["issuer"],
            leeway=int(cfg.get("leeway", 30)),
            options={"require": ["exp", "iat", "jti", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        return None, E_EXPIRED
    except Exception:
        return None, E_BAD_TOKEN

    if not CUSTOMER_NO_RE.fullmatch(str(payload.get("sub", ""))):
        return None, E_BAD_TOKEN
    return payload, None


def authenticate(token: str):
    """トークンでログインできるか判定する。

    戻り値: (user, None) または (None, 失敗理由)

    検証の順序は発行側と合意済み。とくに jti の消費は
    「署名検証が通った時点」で行い、後続処理の成否とは切り離す。
    アカウントが見つからなくてもトークンは消費済みのままとし、
    弾いたトークンが再利用できないようにする。
    """
    # ① 署名・kid・iss・aud・exp
    payload, err = _verify(token)
    if err:
        return None, err

    # ② jti を消費（db 側で独立したトランザクションとしてコミットされる）
    exp_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    if not consume_jti(str(payload["jti"]), exp_at.strftime("%Y-%m-%d %H:%M:%S")):
        return None, E_REPLAYED

    # ③ 顧客番号でアカウントを検索
    user = get_user_by_customer_no(str(payload["sub"]))
    if not user:
        return None, E_NO_ACCOUNT

    # ④ 管理者・無効化アカウントは拒否する。
    #    管理者は全社の会話履歴を閲覧できるため、発行側が侵害された際の
    #    影響範囲を1社分に留める。運用で顧客番号を設定してしまっても
    #    ここで止まる。
    if user.get("is_admin") or not user.get("is_active"):
        return None, E_NOT_ALLOWED

    return user, None
