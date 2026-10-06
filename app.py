# =============================================================
# 移転のお知らせ（旧アドレス https://jyoseikin-navi-r08.streamlit.app/ 用）
# 切り替えのとき、このファイルの中身で R8 リポジトリの app.py を置き換えて main に上げる。
# データベースにもAIにも一切つながない（旧アドレスでの書き込みを止めるため）。
# =============================================================
import html

import streamlit as st

YEAR_LABEL = "令和8年度版"
NEW_URL = "https://tk2-115-58023.vs.sakura.ne.jp/r8/"

st.set_page_config(
    page_title=f"移転のお知らせ｜書類作成AIエージェント（{YEAR_LABEL}）",
    page_icon="🛡️",
    layout="centered",
)

url = html.escape(NEW_URL)
st.markdown(f"""
<style>
header[data-testid="stHeader"], [data-testid="stToolbar"], #MainMenu, footer {{ display: none !important; }}
.stApp {{ background: linear-gradient(180deg, #172A44 0%, #1F3A5F 100%); }}
.block-container {{ padding-top: 3.5rem; padding-bottom: 3rem; max-width: 640px; }}
.mv-head {{ color: #fff; text-align: center; margin-bottom: 1.4rem; }}
.mv-head .name {{ font-size: 1.05rem; font-weight: 700; letter-spacing: .04em; }}
.mv-head .badge {{ display: inline-block; margin-left: .4rem; padding: .1rem .55rem; border-radius: 999px;
                   background: rgba(255,255,255,.14); font-size: .78rem; font-weight: 700; vertical-align: 2px; }}
.mv-card {{ background: #fff; border-radius: 14px; padding: 1.8rem 1.6rem; box-shadow: 0 10px 30px rgba(0,0,0,.18); color: #1A2230; }}
.mv-card h1 {{ font-size: 1.35rem; line-height: 1.5; margin: 0 0 .9rem; padding: 0; color: #1A2230; }}
.mv-card p {{ font-size: .98rem; line-height: 1.85; margin: 0 0 .9rem; }}
.mv-url {{ display: block; margin: 1.1rem 0 .6rem; padding: .85rem 1rem; border: 1px solid #D5DCE6; border-radius: 10px;
           background: #F7F8FA; font-size: 1rem; font-weight: 700; word-break: break-all; color: #1F3A5F !important;
           text-decoration: underline; }}
.mv-btn {{ display: block; text-align: center; margin: 1rem 0 1.2rem; padding: .85rem 1rem; border-radius: 10px;
           background: #1F3A5F; color: #fff !important; font-weight: 700; font-size: 1.02rem; text-decoration: none !important; }}
.mv-note {{ font-size: .9rem !important; color: #4A5568; border-top: 1px solid #E2E8F0; padding-top: .9rem; margin-bottom: 0 !important; }}
@media (max-width: 480px) {{
  .block-container {{ padding-top: 2rem; }}
  .mv-card {{ padding: 1.4rem 1.1rem; }}
  .mv-card h1 {{ font-size: 1.2rem; }}
}}
</style>
<div class="mv-head"><span class="name">書類作成エージェント</span><span class="badge">{YEAR_LABEL}</span></div>
<div class="mv-card">
  <h1>書類作成AIエージェントは、新しいアドレスに移転しました</h1>
  <p>いつもご利用いただきありがとうございます。<br>
     このアドレスでのご利用は終了しました。今後は、次の新しいアドレスからご利用ください。</p>
  <a class="mv-url" href="{url}" target="_blank" rel="noopener">{url}</a>
  <a class="mv-btn" href="{url}" target="_blank" rel="noopener">新しいアドレスを開く</a>
  <p><b>ログインIDとパスワードは、これまでと同じものをそのままお使いいただけます。</b><br>
     これまでの相談履歴も、新しいアドレスで引き続きご覧いただけます。</p>
  <p class="mv-note">お手数ですが、ブックマーク（お気に入り）に登録している場合は、新しいアドレスに変更してください。</p>
</div>
""", unsafe_allow_html=True)
