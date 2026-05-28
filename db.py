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
                created_at    TEXT    NOT NULL,
                last_login_at TEXT
            )
            """)
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


def create_user(username: str, display_name: str, password_hash: str, is_admin: bool = False) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, display_name, password_hash, is_admin, created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (username, display_name, password_hash, int(is_admin), _now()),
            )
            return cur.fetchone()["id"]


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
