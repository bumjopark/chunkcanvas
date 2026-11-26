"""Generate reference outputs from the real LangChain splitters for packages/core parity tests.

    services/api/.venv/bin/pip install langchain-text-splitters tiktoken
    services/api/.venv/bin/python scripts/gen_splitter_fixtures.py
"""
import json
import re
from importlib.metadata import version
from pathlib import Path

from langchain_text_splitters import CharacterTextSplitter, RecursiveCharacterTextSplitter, TokenTextSplitter

TEXT = """# The Chunking Field Guide

How document structure, chunk size and boundary placement shape what a retrieval system can find. Retrieval-augmented generation answers a question by first searching a collection of text fragments, then handing the best matches to a language model.

## 1. Why chunking matters

A good chunk is self-contained: it carries enough context to be understood on its own and is focused enough that its embedding represents one idea.
Most pipelines choose chunk boundaries automatically, using a fixed token budget with some overlap.

- A heading ends one chunk while its explanation starts the next.
- A table is separated from the sentence that explains what it measures.

| Content type | Target tokens | Max tokens |
| --- | --- | --- |
| FAQ | 150 | 300 |
| Manuals | 500 | 800 |

## 제2장 청크 기준

기본 목표 크기는 500 토큰이며 최대 800 토큰을 넘지 않도록 합니다. 하나의 표처럼 나눌 수 없는 블록이 최대 크기를 넘는 경우에는 경고만 표시하고 강제로 자르지 않습니다.

これは\u65e5\u672c\u8a9eの\u6587です。\u6b21の\u6587も\u7d9aきます。\u4e2d\u6587\u7684\u53e5\u5b50\u4e5f\u5728\u8fd9\u91cc。\u6ca1\u6709\u7a7a\u683c\u7684\u6587\u672c\u4f1a\u6309\u5b57\u7b26\u5207\u5206。

Emoji and symbols 😀 survive: café, naïve, 𝔘𝔫𝔦𝔠𝔬𝔡𝔢."""

CASES = [
    ("recursive-chars-120-30", RecursiveCharacterTextSplitter(chunk_size=120, chunk_overlap=30)),
    ("recursive-chars-300-0", RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=0)),
    ("recursive-chars-40-10", RecursiveCharacterTextSplitter(chunk_size=40, chunk_overlap=10)),
    ("character-200-50", CharacterTextSplitter(separator="\n\n", chunk_size=200, chunk_overlap=50)),
    ("recursive-tokens-60-10", RecursiveCharacterTextSplitter.from_tiktoken_encoder(encoding_name="cl100k_base", chunk_size=60, chunk_overlap=10)),
    ("token-50-10", TokenTextSplitter(encoding_name="cl100k_base", chunk_size=50, chunk_overlap=10)),
]

out = {
    "generator": f"langchain-text-splitters {version('langchain-text-splitters')}, tiktoken {version('tiktoken')}",
    "text": TEXT,
    "cases": {name: splitter.split_text(TEXT) for name, splitter in CASES},
}
path = Path(__file__).resolve().parents[1] / "packages/core/test/fixtures/langchain-splitters.json"
# Han characters are written as \u escapes, the rest as it is.
text = re.sub("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", lambda m: "\\u%04x" % ord(m.group()), json.dumps(out, ensure_ascii=False, indent=1))
path.write_text(text + "\n", encoding="utf-8")
print(f"wrote {path} ({', '.join(f'{k}: {len(v)}' for k, v in out['cases'].items())})")
