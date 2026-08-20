"""
db.py  –  PostgreSQL CRUD 全般（Supabase対応）
テーブル: users / conversations / messages
"""
import os
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()  # ローカル開発用 .env を読み込む

# 環境変数から接続文字列を取得（ローカルは .env、Streamlit Cloud は Secrets）
DATABASE_URL = os.getenv("DATABASE_URL", "")

# アプリ年度識別子（R7=令和7年度版, R8=令和8年度版）
# 同一DBを複数年度版アプリで共有するときの会話分離キー。未設定時はR7扱い。
APP_YEAR = os.getenv("APP_YEAR", "R7")

JST = timezone(timedelta(hours=9))

# コネクションプール（アプリ起動時に1回だけ作成・再利用）
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """コネクションプールを取得（なければ作成）"""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def _now() -> str:
    """日本時間の現在時刻を文字列で返す"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_conn():
    """コネクションプールから接続を取得するコンテキストマネージャー"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # 切断されていた場合は再接続
        if conn.closed:
            pool.putconn(conn, close=True)
            conn = pool.getconn()
        yield conn
        conn.commit()
    except psycopg2.OperationalError:
        # 接続エラー時はプールをリセットして再試行
        conn.rollback()
        pool.putconn(conn, close=True)
        global _pool
        _pool = None
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


# =============================================================
# テーブル作成（アプリ起動時に1回呼び出す）
# =============================================================
# create_tables() は app.py 側で @st.cache_resource 越しに呼ばれる。
# キャッシュはデコレートした関数自身のコードでしか無効化されないため、
# ここのスキーマだけ変えてもプロセスが生き残っているとマイグレーションが
# 実行されない。スキーマを変更したら必ずこの値を +1 すること。
SCHEMA_VERSION = 4


def create_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT    NOT NULL UNIQUE,
                display_name  TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                customer_no   TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    NOT NULL,
                last_login_at TEXT
            )
            """)
            # 既存DBへの追加（テーブルが既にある環境向けのマイグレーション）
            cur.execute(
                """ALTER TABLE users
                   ADD COLUMN IF NOT EXISTS customer_no TEXT NOT NULL DEFAULT ''"""
            )
            cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title      TEXT    NOT NULL DEFAULT '無題の会話',
                domain_key TEXT    NOT NULL DEFAULT '',
                form_name  TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT    NOT NULL CHECK(role IN ('user', 'assistant')),
                content         TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            )
            """)
            # 既存DBへの後方互換マイグレーション: app_year カラムを追加（既存行は 'R7' 扱い）
            cur.execute("""
                ALTER TABLE conversations
                ADD COLUMN IF NOT EXISTS app_year TEXT NOT NULL DEFAULT 'R7'
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_user     ON conversations(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_updated  ON conversations(updated_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_app_year ON conversations(app_year)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv      ON messages(conversation_id)")
            # SSO で使用済みのトークンID。同じトークンの再利用を防ぐ。
            # jti を主キーにすることで、INSERT の衝突＝使用済みと判定できる。
            cur.execute("""
            CREATE TABLE IF NOT EXISTS sso_used_jti (
                jti        TEXT NOT NULL PRIMARY KEY,
                used_at    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sso_jti_exp ON sso_used_jti(expires_at)"
            )
            # 顧客番号の一意制約。SSO はこの番号でログイン先を特定するため、
            # 重複していると特定できない。
            # ただし未設定は空文字で保持しており、PostgreSQL では空文字も
            # 通常の値として扱われるため、単純な UNIQUE では未設定どうしが
            # 衝突してしまう。空文字を除いた部分インデックスにする。
            cur.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_customer_no
                       ON users (customer_no) WHERE customer_no <> ''"""
            )


# =============================================================
# ユーザー関連
# =============================================================
def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, display_name: str, password_hash: str, is_admin: bool = False,
                customer_no: str = "") -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, display_name, password_hash, is_admin, customer_no, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (username, display_name, password_hash, int(is_admin), customer_no, _now()),
            )
            return cur.fetchone()["id"]


def update_customer_no(user_id: int, customer_no: str) -> None:
    """顧客番号を更新する（管理画面からの個別修正・一括取込の両方で使う）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET customer_no = %s WHERE id = %s",
                (customer_no, user_id),
            )


def bulk_update_customer_no(pairs: list[tuple[int, str]]) -> int:
    """(user_id, customer_no) をまとめて更新する。1トランザクションで実行し、
    途中で失敗した場合は全件ロールバックされる。"""
    if not pairs:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE users SET customer_no = %s WHERE id = %s",
                [(no, uid) for uid, no in pairs],
            )
            return len(pairs)


def update_password(user_id: int, new_hash: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (new_hash, user_id),
            )


def set_user_active(user_id: int, is_active: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s",
                (int(is_active), user_id),
            )


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


def update_last_login(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = %s WHERE id = %s",
                (_now(), user_id),
            )


# =============================================================
# SSO（他システムからのトークンによるログイン）
# =============================================================
def get_user_by_customer_no(customer_no: str) -> dict | None:
    """顧客番号からアカウントを引く。customer_no には部分一意インデックスが
    張られているため、該当は最大1件。"""
    if not customer_no:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE customer_no = %s", (customer_no,))
            row = cur.fetchone()
    return dict(row) if row else None


def consume_jti(jti: str, expires_at: str) -> bool:
    """トークンIDを使用済みとして記録する。既に使われていれば False を返す。

    ★ この関数は必ず単独で呼び、単独でコミットさせること。
      アカウント検索など後続処理と同じトランザクションに入れると、
      後続で例外が出たときに記録ごとロールバックされ、
      「弾いたトークンが再利用できる」状態になる。
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sso_used_jti (jti, used_at, expires_at) VALUES (%s, %s, %s)",
                    (jti, _now(), expires_at),
                )
        return True
    except psycopg2.IntegrityError:
        # 主キー衝突 = 既に使用済み
        return False


def delete_expired_jti() -> int:
    """有効期限を過ぎたトークンIDを削除する（日次バッチから呼ぶ）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sso_used_jti WHERE expires_at < %s", (_now(),))
            return cur.rowcount


# =============================================================
# 運用確認用の集計
# =============================================================
def get_customer_no_duplicates() -> list[dict]:
    """同じ顧客番号を持つアカウントを返す。

    SSO は顧客番号でアカウントを特定するため、重複があるとログイン先を
    決められない。customer_no に UNIQUE 制約を張る前の確認に使う。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT customer_no,
                          COUNT(*) AS cnt,
                          STRING_AGG(display_name || '（' || username || '）', ' / '
                                     ORDER BY id) AS accounts
                     FROM users
                    WHERE customer_no <> ''
                    GROUP BY customer_no
                   HAVING COUNT(*) > 1
                    ORDER BY customer_no"""
            )
            return [dict(r) for r in cur.fetchall()]


def get_customer_no_summary() -> dict:
    """顧客番号の設定状況（全体 / 設定済み / 未設定）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)                                  AS total,
                          COUNT(*) FILTER (WHERE customer_no <> '') AS with_no,
                          COUNT(*) FILTER (WHERE customer_no =  '') AS without_no
                     FROM users"""
            )
            return dict(cur.fetchone())


def get_conversation_counts_by_year() -> list[dict]:
    """年度別の会話数・メッセージ数・最終利用日。

    旧年度版がどの程度使われているかを見るため（SSO の遷移先を
    現行年度に固定してよいかの判断材料）。
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.app_year,
                          COUNT(DISTINCT c.id) AS conversations,
                          COUNT(m.id)          AS messages,
                          MAX(c.updated_at)    AS last_used
                     FROM conversations c
                     LEFT JOIN messages m ON m.conversation_id = c.id
                    GROUP BY c.app_year
                    ORDER BY c.app_year"""
            )
            return [dict(r) for r in cur.fetchall()]


def get_all_user_stats() -> list[dict]:
    """全ユーザーの利用統計（管理画面用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    u.id,
                    u.username,
                    u.display_name,
                    u.is_active,
                    u.last_login_at,
                    COUNT(DISTINCT c.id)  AS total_conversations,
                    COUNT(m.id)           AS total_messages
                FROM users u
                LEFT JOIN conversations c ON c.user_id = u.id
                LEFT JOIN messages m      ON m.conversation_id = c.id
                GROUP BY u.id
                ORDER BY u.created_at DESC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# =============================================================
# 会話スレッド関連
# =============================================================
def create_conversation(user_id: int, domain_key: str, form_name: str, title: str = "無題の会話") -> int:
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversations (user_id, domain_key, form_name, title, created_at, updated_at, app_year)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (user_id, domain_key, form_name, title, now, now, APP_YEAR),
            )
            return cur.fetchone()["id"]


def get_conversations_by_user(user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """現在のアプリ年度（APP_YEAR）の会話のみを返す。年度違いのルール混入を防ぐため。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM conversations
                   WHERE user_id = %s AND app_year = %s
                   ORDER BY updated_at DESC
                   LIMIT %s OFFSET %s""",
                (user_id, APP_YEAR, limit, offset),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_all_conversations_by_user(user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """全年度の会話を返す（管理画面用）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM conversations
                   WHERE user_id = %s
                   ORDER BY updated_at DESC
                   LIMIT %s OFFSET %s""",
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM conversations WHERE id = %s", (conv_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def update_conversation_title(conv_id: int, title: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s WHERE id = %s",
                (title, conv_id),
            )


def touch_conversation(conv_id: int) -> None:
    """updated_at を現在時刻に更新（スレッド一覧のソート用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (_now(), conv_id),
            )


def delete_old_conversations(days: int = 90) -> int:
    """updated_at が days 日以上前の会話を削除（messages は CASCADE で連鎖削除）"""
    cutoff = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE updated_at < %s", (cutoff,)
            )
            return cur.rowcount


# =============================================================
# メッセージ関連
# =============================================================
def add_message(conv_id: int, role: str, content: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO messages (conversation_id, role, content, created_at)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (conv_id, role, content, _now()),
            )
            return cur.fetchone()["id"]


def get_messages_by_conversation(conv_id: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM messages WHERE conversation_id = %s ORDER BY id ASC",
                (conv_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]
