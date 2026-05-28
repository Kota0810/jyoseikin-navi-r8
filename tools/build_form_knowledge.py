import argparse
import json
import os
import time

from google.genai import Client, types
from dotenv import load_dotenv

load_dotenv()
client = Client(api_key=os.getenv("GEMINI_API_KEY"))


def build_form_knowledge(domain_key: str):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    domain_dir   = os.path.join(project_root, "domains", domain_key)
    template_dir = os.path.join(domain_dir, "templates")

    if not os.path.exists(template_dir):
        print(f"エラー: '{template_dir}' フォルダが見当たりません。")
        return

    pdf_files = [
        os.path.join(template_dir, f)
        for f in os.listdir(template_dir)
        if f.lower().endswith(".pdf")
    ]
    if not pdf_files:
        print(f"'{template_dir}' 内にPDFが見つかりません。")
        return

    print(f"合計 {len(pdf_files)} 個の様式から構造情報を抽出します...")
    form_master = {}

    for pdf_path in pdf_files:
        pdf_name = os.path.basename(pdf_path)
        print(f"--- [{pdf_name}] を解析中 ---")

        prompt = f"""
        あなたは申請書の構造解析エキスパートです。
        ファイル『{pdf_name}』を解析し、以下の形式でJSONを出力してください。

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
            time.sleep(2)

        except Exception as e:
            print(f"エラー発生 ({pdf_name}): {e}")

    output_path = os.path.join(domain_dir, "form_structures.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(form_master, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完了！『{output_path}』を作成しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="form_structures.json を生成します")
    parser.add_argument("--domain", required=True, help="domains/ 配下のドメインフォルダ名（例: 雇用管理制度）")
    args = parser.parse_args()
    build_form_knowledge(args.domain)
