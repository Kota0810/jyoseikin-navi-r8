"""sso.py の検証ロジックを、実際に鍵ペアを作って通しで確認する。

    python tests/test_sso.py

本番DBにも本番の鍵にも触れない。その場で鍵ペアを生成し、
db と streamlit を差し替えて全分岐を踏む。
SSO のロジックを変更したら必ず実行すること。
"""
import sys, os, uuid, types
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
from cryptography.hazmat.primitives import serialization

# ── 鍵ペアを2組作る（本番用 / 失効済みの試験用）──
def rsa_pair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub

PRIV, PUB = rsa_pair()
OTHER_PRIV, _ = rsa_pair()          # 別人の鍵（偽造の試行）

ISS, AUD, KID = "nss-liboffice-cloud", "jyoseikin-navi", "2026-08-key1"

# ── db を差し替え（本番DBに触れない）──
used = set()
accounts = {
    "C000000001": {"id": 2, "display_name": "テスト株式会社", "is_admin": 0, "is_active": 1},
    "C000000002": {"id": 3, "display_name": "無効化された会社", "is_admin": 0, "is_active": 0},
    "C000000003": {"id": 1, "display_name": "管理者",           "is_admin": 1, "is_active": 1},
}
fake_db = types.ModuleType("db")
fake_db.consume_jti = lambda jti, exp: (jti not in used) and (used.add(jti) or True)
fake_db.get_user_by_customer_no = lambda no: accounts.get(no)
sys.modules["db"] = fake_db

# ── streamlit の secrets を差し替え ──
fake_st = types.ModuleType("streamlit")
fake_st.secrets = {"sso": {
    "issuer": ISS, "audience": AUD, "leeway": 30,
    "return_url": "https://example.invalid/user/funding/ai-agent/sso",
    "public_keys": {KID: PUB},
}}
sys.modules["streamlit"] = fake_st

import sso


def make(sub="C000000001", *, priv=PRIV, kid=KID, iss=ISS, aud=AUD,
         exp_delta=180, jti=None, alg="RS256", drop=None):
    now = datetime.now(tz=timezone.utc)
    payload = {"iss": iss, "aud": aud, "sub": sub,
               "iat": int(now.timestamp()),
               "exp": int((now + timedelta(seconds=exp_delta)).timestamp()),
               "jti": jti or str(uuid.uuid4())}
    if drop:
        payload.pop(drop)
    return jwt.encode(payload, priv, algorithm=alg, headers={"kid": kid})


def check(label, token, want_ok, want_err=None):
    user, err, _ret = sso.authenticate(token)
    ok = (user is not None)
    good = (ok == want_ok) and (want_err is None or err == want_err)
    print(f"  {'OK ' if good else 'NG '} {label:<44} -> "
          f"{'ログイン成功' if ok else '拒否:' + str(err)}")
    return good


print("設定の読み込み:", "有効" if sso.is_enabled() else "無効")
print("戻り先URL     :", sso.return_url())
print()

results = []
results.append(check("正常なトークン", make(), True))
results.append(check("同じトークンの再提示（リプレイ）", (lambda t: (sso.authenticate(t), t)[1])(make()), False, sso.E_REPLAYED))
results.append(check("有効期限切れ（leeway超過 -60秒）", make(exp_delta=-60), False, sso.E_EXPIRED))
results.append(check("leeway内の軽微な期限切れ(-20秒)", make(exp_delta=-20), True))
results.append(check("別の秘密鍵で署名（偽造）", make(priv=OTHER_PRIV), False, sso.E_BAD_TOKEN))
results.append(check("未知のkid（失効した試験用鍵）", make(kid="test-2026-08"), False, sso.E_BAD_TOKEN))
results.append(check("iss が違う", make(iss="attacker"), False, sso.E_BAD_TOKEN))
results.append(check("aud が違う", make(aud="other-app"), False, sso.E_BAD_TOKEN))
results.append(check("sub の形式が不正（C+8桁）", make(sub="C00000001"), False, sso.E_BAD_TOKEN))
results.append(check("jti が無い", make(drop="jti"), False, sso.E_BAD_TOKEN))
results.append(check("exp が無い", make(drop="exp"), False, sso.E_BAD_TOKEN))
results.append(check("未登録の顧客番号", make(sub="C000009999"), False, sso.E_NO_ACCOUNT))
results.append(check("無効化されたアカウント", make(sub="C000000002"), False, sso.E_NOT_ALLOWED))
results.append(check("管理者アカウント（顧客番号あり）", make(sub="C000000003"), False, sso.E_NOT_ALLOWED))
# 署名の「末尾1文字」を変える方法は使わない。RS256 の署名は 2048bit で、
# base64url の最後の1文字は下位2ビットしか意味を持たず、残りのビットは
# デコード時に無視される。そのため文字を変えても署名バイト列が変わらず、
# 検証を通ってしまうことがある（改ざんできていない状態を検査してしまう）。
import base64 as _b64t, json as _jt

def _seg(x):
    return _b64t.urlsafe_b64decode(x + "=" * (-len(x) % 4))

def _mk(b):
    return _b64t.urlsafe_b64encode(b).rstrip(b"=").decode()

# (1) ペイロードの顧客番号を他社にすり替える（本命の攻撃）
_h, _p2, _sg = make().split(".")
_claims = _jt.loads(_seg(_p2))
_claims["sub"] = "C000000003"          # 管理者の顧客番号にすり替える
_swapped = f"{_h}.{_mk(_jt.dumps(_claims).encode())}.{_sg}"
results.append(check("顧客番号を他社にすり替えたトークン", _swapped, False, sso.E_BAD_TOKEN))

# (2) 署名の中間バイトを1つ反転させる（確実に署名バイト列が変わる）
_h3, _p3, _s3 = make().split(".")
_sig_bytes = bytearray(_seg(_s3))
_sig_bytes[len(_sig_bytes) // 2] ^= 0xFF
_bitflip = f"{_h3}.{_p3}.{_mk(bytes(_sig_bytes))}"
results.append(check("署名を1バイト反転させたトークン", _bitflip, False, sso.E_BAD_TOKEN))

# HS256 混入（公開鍵を共通鍵とみなした署名偽造）。
# PyJWT は encode を拒むため、攻撃者と同じように手で組み立てる。
import base64, hmac, hashlib, json as _json

def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")

_now = datetime.now(tz=timezone.utc)
_hdr = _b64(_json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
_pl = _b64(_json.dumps({"iss": ISS, "aud": AUD, "sub": "C000000001",
                        "iat": int(_now.timestamp()),
                        "exp": int((_now + timedelta(seconds=180)).timestamp()),
                        "jti": str(uuid.uuid4())}).encode())
_sig = _b64(hmac.new(PUB.encode(), _hdr + b"." + _pl, hashlib.sha256).digest())
forged = (_hdr + b"." + _pl + b"." + _sig).decode()
results.append(check("HS256 での署名偽造（アルゴリズム混同攻撃）", forged, False, sso.E_BAD_TOKEN))

# alg=none（署名なし）
_hdr2 = _b64(_json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode())
none_tok = (_hdr2 + b"." + _pl + b".").decode()
results.append(check("alg=none（署名なし）", none_tok, False, sso.E_BAD_TOKEN))

# 弾いたトークンの jti が消費されているか（重要）
print()
t = make(sub="C000009999")                     # 未登録 → 拒否されるが jti は消費されるはず
sso.authenticate(t)
u2, e2, _ = sso.authenticate(t)
consumed = (e2 == sso.E_REPLAYED)
print(f"  {'OK ' if consumed else 'NG '} 弾いたトークンも消費済みになっている       -> {e2}")
results.append(consumed)

# ── ret（戻り先URL）の扱い ──
print()
def ret_of(token):
    return sso.authenticate(token)[2]

def make_ret(ret, **kw):
    now = datetime.now(tz=timezone.utc)
    p = {"iss": ISS, "aud": AUD, "sub": "C000000001",
         "iat": int(now.timestamp()),
         "exp": int((now + timedelta(seconds=kw.get("exp_delta", 180))).timestamp()),
         "jti": str(uuid.uuid4()), "ret": ret}
    return jwt.encode(p, PRIV, algorithm="RS256", headers={"kid": KID})

def check_ret(label, got, want):
    good = (got == want)
    print(f"  {'OK ' if good else 'NG '} {label:<44} -> {got or '（空）'}")
    return good

CFG_RET = "https://example.invalid/user/funding/ai-agent/sso"
results.append(check_ret("署名済みの ret が採用される",
                         ret_of(make_ret("https://marugoto.example/sso")),
                         "https://marugoto.example/sso"))
results.append(check_ret("ret が無ければ設定値にフォールバック", ret_of(make()), CFG_RET))
results.append(check_ret("http:// の ret は拒否して設定値に戻す",
                         ret_of(make_ret("http://insecure.example/sso")), CFG_RET))
results.append(check_ret("javascript: の ret は拒否",
                         ret_of(make_ret("javascript:alert(1)")), CFG_RET))
results.append(check_ret("期限切れでも署名済みの ret は取り出せる",
                         ret_of(make_ret("https://lib.example/sso", exp_delta=-60)),
                         "https://lib.example/sso"))

# 未署名（改ざん）トークンの ret が使われないこと
import base64 as _b64m, json as _jm
_h = _b64m.urlsafe_b64encode(_jm.dumps({"alg":"RS256","typ":"JWT","kid":KID}).encode()).rstrip(b"=")
_p = _b64m.urlsafe_b64encode(_jm.dumps({"iss":ISS,"aud":AUD,"sub":"C000000001",
     "iat":int(datetime.now(tz=timezone.utc).timestamp()),
     "exp":int((datetime.now(tz=timezone.utc)+timedelta(seconds=180)).timestamp()),
     "jti":str(uuid.uuid4()),"ret":"https://phishing.example/steal"}).encode()).rstrip(b"=")
tampered = (_h + b"." + _p + b".AAAA").decode()
results.append(check_ret("署名が無効なトークンの ret は使わない",
                         ret_of(tampered), CFG_RET))

print()
print(f"結果: {sum(results)} / {len(results)} 件が期待どおり")
sys.exit(0 if all(results) else 1)
