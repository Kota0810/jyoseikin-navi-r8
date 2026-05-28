"""
add_form_knowledge.py
既存の form_structures.json に「未処理のPDFだけ」を追記する差分更新スクリプト。
既存エントリは一切上書きしない。

使い方:
    python tools/add_form_knowledge.py --domain 両立支援等
"""
import argparse
import json
import os
import time

from google.genai import Client, types
from dotenv import load_dotenv

load_dotenv()
client = Client(api_key=os.getenv("GEMINI_API_KEY"))


def add_form_knowledge(domain_key: str):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    domain_dir   = os.path.join(project_root, "domains", domain_key)
    template_dir = os.path.join(domain_dir, "templates")
    output_path  = os.path.join(domain_dir, "form_structures.json")

    if not os.path.exists(template_dir):
        print(f"エラー: '{template_dir}' フォルダが見当たりません。")
        return

    # 既存 JSON を読み込む（なければ空で開始）
    if os.path.isfile(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            form_master = json.load(f)
        print(f"既存 form_structures.json を読み込みました（{len(form_master)} 件）")
    else:
        form_master = {}
        print("form_structures.json が存在しないため、新規作成します。")

    # templates/ 内の全 PDF を取得
    all_pdfs = [f for f in os.listdir(template_dir) if f.lower().endswith(".pdf")]

    # 未処理（JSONに存在しない）PDFだけ抽出
    new_pdfs = [f for f in all_pdfs if f not in form_master]

    if not new_pdfs:
        print("追加すべき新しいPDFはありません。処理を終了します。")
        return

    print(f"新規PDF: {len(new_pdfs)} 件 / 既存: {len(form_master)} 件 / 合計: {len(all_pdfs)} 件")
    print("新規PDFのみ処理します:\n")

    for pdf_name in sorted(new_pdfs):
        pdf_path = os.path.join(template_dir, pdf_name)
        print(f"--- [{pdf_name}] を解析中 ---")

        prompt = f"""
        あなたは申請書の構造解析エキスパートです。
        ファイル'{pdf_name}'を解析し、以下の形式でJSONを出力してください。

        【解析ルール】
        1. 書類内の全ての記入項目（①、②、③、(1)、(2)など）を特定すること。
        2. 書類の後半にある『記入上の注意』や『提出上の注意』を読み取り、各項目番号に対応する具体的なヒントや制約事項を抽出すること。
        3. 項目名と注釈を必ずセットにすること。

        出力形式：
        {{
          "様式名": "{pdf_name}",
          "items": [
            {{
              "item_id": "項目番号(例: ③(1))",
              "label": "項目名",
              "instruction": "その項目に対応する記入上の注意・ヒントの全文",
              "logic_check": "AIが判断したこの項目の重要ルール（例：全チェック必須、期間制限あり等）"
            }}
          ]
        }}
        """

        response_schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "様式名": types.Schema(type=types.Type.STRING),
                "items": types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "item_id":     types.Schema(type=types.Type.STRING),
                            "label":       types.Schema(type=types.Type.STRING),
                            "instruction": types.Schema(type=types.Type.STRING),
                            "logic_check": types.Schema(type=types.Type.STRING),
                        },
                        required=["item_id", "label", "instruction", "logic_check"]
                    )
                ),
            },
            required=["様式名", "items"]
        )

        try:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt, types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_schema
                )
            )

            form_data = json.loads(response.text)
            form_master[pdf_name] = form_data
            print(f"成功: {len(form_data.get('items', []))} 個の項目をマッピングしました。")

            # 1件処理するたびに上書き保存（途中中断しても損失なし）
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(form_master, f, ensure_ascii=False, indent=2)

            time.sleep(2)

        except Exception as e:
            print(f"エラー発生 ({pdf_name}): {e}")

    print(f"\n完了！合計 {len(form_master)} 件の様式情報を '{output_path}' に保存しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="form_structures.json に新規PDFだけを差分追記します")
    parser.add_argument("--domain", required=True, help="domains/ 配下のドメインフォルダ名（例: 両立支援等）")
    args = parser.parse_args()
    add_form_knowledge(args.domain)
