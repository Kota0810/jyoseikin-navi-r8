import argparse
import json
import os

from PyPDF2 import PdfReader


def create_chunks_from_pdf(pdf_path: str, domain_key: str, chunk_size: int = 500, overlap: int = 100):
    reader = PdfReader(pdf_path)
    full_text = ""
    for page in reader.pages:
        text = page.extract_text()
        if text:
            full_text += text.replace("\n", " ")

    chunks = []
    start = 0
    while start < len(full_text):
        end = start + chunk_size
        chunks.append({
            "source":  os.path.basename(pdf_path),
            "content": full_text[start:end],
            "domain":  domain_key,
        })
        start += (chunk_size - overlap)
    return chunks


def create_chunks(domain_key: str):
    project_root  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    domain_dir    = os.path.join(project_root, "domains", domain_key)
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

    all_chunks = []
    for pdf_path in pdf_files:
        print(f"Breaking down {os.path.basename(pdf_path)} for RAG...")
        all_chunks.extend(create_chunks_from_pdf(pdf_path, domain_key))

    output_path = os.path.join(domain_dir, "pdf_chunks.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)

    print(f"Success: 合計 {len(all_chunks)} 個の断片を『{output_path}』に保存しました。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="pdf_chunks.json を生成します")
    parser.add_argument("--domain", required=True, help="domains/ 配下のドメインフォルダ名（例: 雇用管理制度）")
    args = parser.parse_args()
    create_chunks(args.domain)
