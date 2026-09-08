"""
add_rule_knowledge.py
既存の basic_rules.json に「まだ抽出していない資料だけ」を追記する差分更新版。

build_rule_knowledge.py は knowledge/ の全PDFを毎回作り直すため、
資料を1本足すだけでも全部を再抽出することになり、時間もAPI費用も
かかるうえ、抽出のたびに結果が変わって既存の知識が入れ替わってしまう。
こちらは source（＝資料のファイル名）が既に basic_rules.json に
入っている資料を飛ばし、1本終わるごとに保存する。

使い方:
    python tools/add_rule_knowledge.py --domain 人材開発支援
"""
import argparse
import json
import os
import time

import fitz  # PyMuPDF
from google.genai import Client, types
from dotenv import load_dotenv

load_dotenv()
client = Client(api_key=os.getenv("GEMINI_API_KEY"))

# 1回のAPI呼び出しで処理するページ数（大きすぎると出力トークン上限に達する）
PAGE_BATCH_SIZE = 10


def extract_page_range_bytes(pdf_path: str, start: int, end: int) -> bytes:
    """PDFから start〜end-1 ページを抽出して bytes で返す"""
    doc = fitz.open(pdf_path)
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=start, to_page=min(end - 1, len(doc) - 1))
    data = sub.tobytes()
    doc.close()
    sub.close()
    return data


def add_rule_knowledge(domain_key: str):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    domain_dir   = os.path.join(project_root, "domains", domain_key)

    # ============================================================
    # domain_config.json を読み込む
    # ★ 横展開時: applies_to_options は domain_config.json 側で設定
    # ============================================================
    config_path = os.path.join(domain_dir, "domain_config.json")
    if not os.path.isfile(config_path):
        print(f"エラー: '{config_path}' が見つかりません。")
        return
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    applies_to_options = config.get("applies_to_options", ["全般"])

    knowledge_dir = os.path.join(domain_dir, "knowledge")
    if not os.path.exists(knowledge_dir):
        print(f"エラー: '{knowledge_dir}' フォルダが見当たりません。")
        return

    pdf_files = [
        os.path.join(knowledge_dir, f)
        for f in os.listdir(knowledge_dir)
        if f.lower().endswith(".pdf")
    ]
    if not pdf_files:
        print(f"'{knowledge_dir}' 内にPDFが見つかりません。")
        return

    output_path = os.path.join(domain_dir, "basic_rules.json")
    if os.path.isfile(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            rule_master = json.load(f)
        print(f"既存 basic_rules.json を読み込みました（{len(rule_master)} 件）")
    else:
        rule_master = []
        print("basic_rules.json が存在しないため、新規作成します。")

    # 既に抽出済みの資料は source に名前が入っている。
    # ★ 出典名はAIが書き写すので、ゼロ幅スペースなどの表記ゆれが混ざる。
    #    記号を落とした形で突き合わせないと、抽出済みの資料をもう一度
    #    処理してしまい、同じルールが二重に入る。
    def norm(s: str) -> str:
        return "".join(ch for ch in str(s)
                       if ch not in "​‌‍﻿ 　")

    done = {norm(r.get("source", "")) for r in rule_master}
    todo = [p for p in pdf_files if norm(os.path.basename(p)) not in done]
    if not todo:
        print("追加すべき新しい資料はありません。処理を終了します。")
        return
    print(f"新規資料: {len(todo)} 件 / 既存: {len(pdf_files) - len(todo)} 件"
          f" / 合計: {len(pdf_files)} 件")

    for pdf_path in todo:
        pdf_name = os.path.basename(pdf_path)
        print(f"--- [{pdf_name}] からルールを抽出中 ---")

        prompt = f"""
        あなたは制度の厳格な監査官です。
        ファイル『{pdf_name}』を解析し、以下の情報を一切の省略なしに抽出してJSON化してください。

        【抽出対象】
        1. 専門用語の定義
        2. 数値ルール（金額、上限額など）
        3. 計算式
        4. 期間の制限（申請期限、計画期間の最短・最長など）
        5. 対象となる事業主・申請者の具体的な要件
        6. applies_to の判定：このルールがどの申請段階に適用されるかを次のリストから選び、リスト形式で記載してください。
           選択肢: {applies_to_options}

        出力フィールド：
        - category  : 上記1〜5のいずれかに対応する分類名
        - term      : 用語または項目名
        - definition: 定義・ルールの詳細全文
        - source    : "{pdf_name}"（固定値）
        - applies_to: 該当する申請段階のリスト（例: {applies_to_options[:1]}）
        - domain    : "{domain_key}"（固定値）
        """

        response_schema = types.Schema(
            type=types.Type.ARRAY,
            items=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "category":   types.Schema(type=types.Type.STRING),
                    "term":       types.Schema(type=types.Type.STRING),
                    "definition": types.Schema(type=types.Type.STRING),
                    "source":     types.Schema(type=types.Type.STRING),
                    "applies_to": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING)
                    ),
                    "domain":     types.Schema(type=types.Type.STRING),
                },
                required=["category", "term", "definition", "source", "applies_to", "domain"]
            )
        )

        # ページ数を取得してバッチ数を計算
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()
        batch_ranges = list(range(0, total_pages, PAGE_BATCH_SIZE))
        print(f"  全{total_pages}ページを{len(batch_ranges)}バッチに分割して処理します")

        def run_batch(start: int, end: int, depth: int = 0) -> list:
            """start〜end-1 ページを1回で処理する。失敗したらページを半分に割って再試行。

            ★ max_output_tokens を必ず指定すること。既定のままだと、記載の
              詰まった10ページで出力が上限に当たり、JSONが文字列の途中で
              切れて json.loads が落ちる（Unterminated string）。
              そのバッチのルールがまるごと失われるが、例外を握りつぶす作りなので
              ログを見ないと気づけない。

            上限を上げても切れることはあるので、切れたらページ数を半分にして
            もう一度試す。1ページまで割っても駄目なときだけ諦める。
            """
            label = f"p.{start + 1}-{end}"
            pad = "  " + "  " * depth
            try:
                page_bytes = extract_page_range_bytes(pdf_path, start, end)
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[prompt, types.Part.from_bytes(data=page_bytes, mime_type="application/pdf")],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=response_schema,
                        max_output_tokens=40000,
                    )
                )
                rules = json.loads(response.text)
                print(f"{pad}[{label}] {len(rules)} 件抽出")
                time.sleep(2)
                return rules
            except Exception as e:
                if end - start <= 1:
                    print(f"{pad}[{label}] エラー（1ページでも失敗、あきらめます）: {e}")
                    return []
                mid = start + (end - start) // 2
                print(f"{pad}[{label}] エラー: {e}")
                print(f"{pad}[{label}] ページを半分に割って再試行します")
                return run_batch(start, mid, depth + 1) + run_batch(mid, end, depth + 1)

        file_rules = []
        for batch_start in batch_ranges:
            batch_end = min(batch_start + PAGE_BATCH_SIZE, total_pages)
            file_rules.extend(run_batch(batch_start, batch_end))

        rule_master.extend(file_rules)
        # 1本処理するたびに保存する。支給要領は数百ページあり数十分かかるので、
        # 途中で止めても続きから再開できるようにしておく。
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(rule_master, f, ensure_ascii=False, indent=2)
        print(f"成功: {pdf_name} から合計 {len(file_rules)} 個のルールを追加しました。"
              f"（累計 {len(rule_master)} 件）")


    # 絵文字は Windows 日本語コンソール(cp932)でエンコードできず、
    # 全処理が終わった最後の print で UnicodeEncodeError になるため使わない
    print(f"\n完了：『{output_path}』を作成しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="basic_rules.json に新規資料だけを差分追記します")
    parser.add_argument("--domain", required=True, help="domains/ 配下のドメインフォルダ名（例: 雇用管理制度）")
    args = parser.parse_args()
    add_rule_knowledge(args.domain)
