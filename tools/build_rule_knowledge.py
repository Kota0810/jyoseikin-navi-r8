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


def build_rule_knowledge(domain_key: str):
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

    print(f"合計 {len(pdf_files)} 個の資料から基本ルール（辞書データ）を抽出します...")
    rule_master = []

    for pdf_path in pdf_files:
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
        print(f"  → 全{total_pages}ページを{len(batch_ranges)}バッチに分割して処理します")

        file_rules = []
        for batch_start in batch_ranges:
            batch_end  = min(batch_start + PAGE_BATCH_SIZE, total_pages)
            batch_label = f"p.{batch_start + 1}-{batch_end}"
            try:
                page_bytes = extract_page_range_bytes(pdf_path, batch_start, batch_end)
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[prompt, types.Part.from_bytes(data=page_bytes, mime_type="application/pdf")],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=response_schema
                    )
                )
                batch_rules = json.loads(response.text)
                file_rules.extend(batch_rules)
                print(f"  [{batch_label}] {len(batch_rules)} 件抽出")
                time.sleep(2)

            except Exception as e:
                print(f"  [{batch_label}] エラー: {e}")

        rule_master.extend(file_rules)
        print(f"成功: {pdf_name} から合計 {len(file_rules)} 個のルールを追加しました。")

    output_path = os.path.join(domain_dir, "basic_rules.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(rule_master, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完了！『{output_path}』を作成しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="basic_rules.json を生成します")
    parser.add_argument("--domain", required=True, help="domains/ 配下のドメインフォルダ名（例: 雇用管理制度）")
    args = parser.parse_args()
    build_rule_knowledge(args.domain)
