import json, requests, pathlib, os

# Configuration values
DATA_DIR = pathlib.Path("data/kb")
API_URL = os.getenv("API_URL")
OUTPUT_FILE = "vectors_for_s3vectors.json"
SIZE, OVERLAP = 1500, 200


def get_embedding(text):
    if not API_URL:
        raise ValueError("API_URL environment variable is not set")
    return requests.post(API_URL, json={"text": text.strip()}).json().get("embedding")


def main():
    records = []
    for path in DATA_DIR.rglob("*.md"):
        text, cursor, count = path.read_text(encoding='utf-8'), 0, 0

        while cursor < len(text):
            chunk = text[cursor: cursor + SIZE]
            vector = get_embedding(chunk)

            if vector:
                records.append({
                    "key": f"{path.name}_{count}",
                    "data": {"float32": vector},
                    "metadata": {"chunk_text": chunk, "source": path.name}
                })
                print(f"✅ {path.name} [Chunk {count}]")

            if cursor + SIZE >= len(text): break
            cursor += (SIZE - OVERLAP)
            count += 1

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(records, f)


if __name__ == "__main__":
    main()