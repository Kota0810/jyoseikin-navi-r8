"""
init_db.py  –  初回セットアップ：DBテーブル作成 + 管理者アカウント作成
使い方: python init_db.py
"""
import sys
from db import create_tables, get_user_by_username, create_user
from auth import hash_password


def main():
    print("=" * 40)
    print("  書類作成エージェント 初期設定")
    print("=" * 40)

    # テーブル作成
    create_tables()
    print("✅ データベーステーブルを初期化しました。")

    # 管理者アカウント作成
    print("\n管理者アカウントを作成します。")

    while True:
        username = input("管理者ログインID（英数字）: ").strip()
        if not username:
            print("  → ログインIDを入力してください。")
            continue
        if get_user_by_username(username):
            print(f"  → '{username}' はすでに存在します。別のIDを入力してください。")
            continue
        break

    display_name = input("管理者表示名: ").strip() or username

    print("  ※ 入力した文字が画面に表示されます。設定後はご注意ください。")
    while True:
        password = input("パスワード（8文字以上推奨）: ").strip()
        if len(password) < 1:
            print("  → パスワードを入力してください。")
            continue
        confirm = input("パスワード（確認）: ").strip()
        if password != confirm:
            print("  → パスワードが一致しません。もう一度入力してください。")
            continue
        break

    create_user(username, display_name, hash_password(password), is_admin=True)
    print(f"\n✅ 管理者アカウント '{username}' を作成しました。")
    print("   アプリを起動して、このIDとパスワードでログインしてください。")
    print("=" * 40)


if __name__ == "__main__":
    main()
