"""Generate the bundled sample PDFs.

The texts are original and the PDFs embed Nanum Gothic (SIL Open Font License), so the
samples can ship with the MIT-licensed repository. Run from the repository root:

    services/api/.venv/bin/python samples/generate_samples.py
"""
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
FONT_DIRS = ["/usr/share/fonts/truetype/nanum", "/usr/share/fonts/nanum"]

CSS = """
@font-face { font-family: Nanum; src: url(NanumGothic.ttf); }
@font-face { font-family: Nanum; src: url(NanumGothicBold.ttf); font-weight: bold; }
* { font-family: Nanum; }
body { font-size: 9.5pt; line-height: 1.45; color: #111; }
h1 { font-size: 21pt; margin: 0 0 4pt 0; }
h2 { font-size: 13pt; margin: 12pt 0 4pt 0; }
h3 { font-size: 10.5pt; margin: 9pt 0 3pt 0; }
p { margin: 0 0 6pt 0; text-align: justify; }
.lead { font-size: 11pt; color: #333; }
li { margin-bottom: 2pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 2pt 0; }
th, td { border: 0.6pt solid #888; padding: 2pt 4pt; font-size: 8.5pt; text-align: left; }
th { background-color: #e8e8e8; }
.caption { font-size: 8.5pt; color: #444; margin: 2pt 0 8pt 0; }
"""

GUIDE_TITLE = """
<h1>The Chunking Field Guide</h1>
<p class="lead">How document structure, chunk size and boundary placement shape what a
retrieval system can find, and why people still need to look at the result.</p>
"""

GUIDE_BODY = """
<h2>1. Why chunking matters</h2>
<p>Retrieval-augmented generation answers a question by first searching a collection of text
fragments, then handing the best matches to a language model. Those fragments are chunks. The
model never sees the whole document, only the chunks that the search returned, so every
answer is limited by how the document was cut.</p>
<p>A good chunk is self-contained: it carries enough context to be understood on its own and is
focused enough that its embedding represents one idea. A poor chunk either mixes unrelated
topics, which blurs its embedding, or splits a single idea across two places, which leaves each
half incomplete.</p>
<p>Most pipelines choose chunk boundaries automatically, using a fixed token budget with some
overlap. That works well for uniform prose and badly for documents with headings, tables,
captions and multi-column layouts.</p>

<h2>2. Common failure modes</h2>
<p>Reviewing automatic chunks on real documents reveals the same handful of problems again and
again:</p>
<ul>
<li>A heading ends one chunk while its explanation starts the next.</li>
<li>A table is separated from the sentence that explains what it measures.</li>
<li>One paragraph is cut in the middle because the token budget ran out.</li>
<li>Closely related paragraphs land in different chunks.</li>
<li>Unrelated content, such as a sidebar, is merged into a chunk.</li>
<li>Text from two columns is read in the wrong order.</li>
</ul>
<p>Each of these lowers retrieval quality in a way that metrics rarely make obvious. The search
still returns something; it is just less useful than it could have been.</p>

<h2>3. Structure-aware splitting</h2>
<p>Structure-aware splitting starts from the document layout instead of a flat string. A parser
first identifies blocks such as titles, section headers, paragraphs, list items, tables and
captions, together with their reading order and position on the page. Chunks are then formed by
grouping consecutive blocks.</p>
<h3>3.1 Rules that usually help</h3>
<ul>
<li>Start a new chunk at a section header once the current chunk is substantial.</li>
<li>Never end a chunk with a heading; move the heading forward instead.</li>
<li>Keep a caption next to its table or figure.</li>
<li>Treat a table as one atomic block in the first pass.</li>
</ul>
<h3>3.2 Choosing a size</h3>
<p>The right budget depends on the embedding model and the kind of question users ask. The
table below lists starting points that teams commonly report. Treat them as defaults to test,
not as rules.</p>
<table>
<tr><th>Content type</th><th>Target tokens</th><th>Max tokens</th><th>Notes</th></tr>
<tr><td>FAQ and short answers</td><td>150</td><td>300</td><td>One question per chunk</td></tr>
<tr><td>Policies and contracts</td><td>400</td><td>700</td><td>Keep clauses whole</td></tr>
<tr><td>Technical manuals</td><td>500</td><td>800</td><td>Split at sections</td></tr>
<tr><td>Research papers</td><td>600</td><td>1000</td><td>Keep tables with text</td></tr>
</table>
<p class="caption">Table 1. Typical starting budgets by content type.</p>
<p>Smaller chunks give sharper embeddings but less context per hit. Larger chunks carry more
context but compete with each other on fewer, broader topics. When in doubt, measure retrieval
on a set of real questions before and after a change.</p>

<h2>4. Why visual review helps</h2>
<p>Looking at chunk text in isolation hides the most important information: where the text came
from. A reviewer who sees chunk boundaries drawn on the original page can tell at a glance that
a table has been separated from its explanation or that a column was read out of order.</p>
<p>Visual review also makes fixes cheap. Moving a boundary by one block is a single drag, and the
updated text and token count can be checked immediately. Because the edit is stored as a list of
boundaries over parsed blocks, it can be saved, reviewed and applied again later without
re-parsing the document.</p>
<h3>4.1 What to check first</h3>
<ol>
<li>Headings that sit alone at the end of a chunk.</li>
<li>Tables whose caption or explanation is elsewhere.</li>
<li>Chunks far above the target size.</li>
<li>Tiny chunks that carry no meaning on their own.</li>
</ol>

<h2>5. Exporting the result</h2>
<p>Edited chunks are only useful if they can leave the editor in a form that indexing pipelines
accept. JSON Lines with one chunk per line is the most portable choice: each line holds the chunk
text, its token count and metadata such as the source file, page range and the identifiers of
the blocks it contains. A Markdown export is convenient for human review, and the split plan
itself can be exported so the same boundaries can be re-applied to the same parse.</p>
<p>Keep overlap out of the editing step. Overlap is a property of how chunks are embedded, not of
where the document is divided, so it belongs in the export or indexing stage where it can be
tuned without touching the reviewed boundaries.</p>

<h2>6. Checklist</h2>
<ul>
<li>Parse with a layout-aware parser and keep page coordinates.</li>
<li>Generate a first split automatically with structure-aware rules.</li>
<li>Review the split on the original pages, fixing headings, tables and columns.</li>
<li>Export JSONL for indexing and keep the split plan for reproducibility.</li>
<li>Measure retrieval quality on real questions after every significant change.</li>
</ul>
"""

KOREAN = """
<h1>문서 청킹 운영 가이드</h1>
<p class="lead">사내 지식 검색 시스템에 문서를 등록할 때 청크 경계를 검토하고 수정하는 절차를 설명합니다.</p>

<h2>제1장 목적과 적용 범위</h2>
<p>이 가이드는 검색 증강 생성(RAG) 시스템에 등록되는 모든 내부 문서에 적용됩니다. 문서는 자동으로
분할된 뒤 담당자가 원본 화면에서 분할 결과를 확인하고, 필요한 경우 경계를 직접 수정합니다.</p>
<p>자동 분할은 빠르지만 문서의 의미 구조를 항상 반영하지는 못합니다. 특히 표, 목록, 여러 단으로 구성된
페이지에서는 사람이 확인하는 단계가 검색 품질에 큰 영향을 줍니다.</p>

<h2>제2장 청크 기준</h2>
<h3>2.1 기본 크기</h3>
<p>기본 목표 크기는 500 토큰이며 최대 800 토큰을 넘지 않도록 합니다. 하나의 표처럼 나눌 수 없는 블록이
최대 크기를 넘는 경우에는 경고만 표시하고 강제로 자르지 않습니다.</p>
<h3>2.2 구조 규칙</h3>
<ul>
<li>제목은 항상 뒤따르는 본문과 같은 청크에 둡니다.</li>
<li>표와 표 제목(캡션)은 분리하지 않습니다.</li>
<li>하나의 조항이나 절은 가능한 한 하나의 청크에 담습니다.</li>
<li>머리글과 바닥글은 청크에서 제외합니다.</li>
</ul>

<h2>제3장 검토 절차</h2>
<p>담당자는 다음 순서로 분할 결과를 검토합니다. 각 단계에서 수정한 내용은 자동으로 저장되며, 언제든지
자동 분할 상태로 되돌릴 수 있습니다.</p>
<ol>
<li>문서를 업로드하고 파싱이 끝날 때까지 기다립니다.</li>
<li>원본 페이지 위에 표시된 청크 영역을 처음부터 끝까지 훑어봅니다.</li>
<li>제목만 남은 청크, 표와 설명이 분리된 청크를 우선 수정합니다.</li>
<li>목표 크기를 크게 넘는 청크는 문단 경계에서 나눕니다.</li>
<li>결과를 JSONL로 내보내 색인 파이프라인에 등록합니다.</li>
</ol>

<h2>제4장 문서 유형별 권장값</h2>
<table>
<tr><th>문서 유형</th><th>목표 토큰</th><th>최대 토큰</th><th>비고</th></tr>
<tr><td>사내 규정</td><td>400</td><td>700</td><td>조항 단위 유지</td></tr>
<tr><td>제품 매뉴얼</td><td>500</td><td>800</td><td>절 단위 분할</td></tr>
<tr><td>회의록</td><td>300</td><td>600</td><td>안건 단위 분할</td></tr>
<tr><td>연구 보고서</td><td>600</td><td>1000</td><td>표와 설명 함께</td></tr>
</table>
<p class="caption">표 1. 문서 유형별 청크 크기 권장값</p>
<p>권장값은 출발점일 뿐이며, 실제 질문으로 검색 결과를 비교해 조정합니다. 값을 바꾼 경우에는 변경 이유와
함께 기록을 남깁니다.</p>

<h2>제5장 보관과 삭제</h2>
<p>업로드한 원본과 파싱 결과는 검토가 끝나면 삭제합니다. 공개 데모 환경에서는 업로드 후 1시간이 지나면
원본 파일, 추출한 텍스트, 편집 상태가 모두 자동으로 삭제됩니다.</p>
<p>내보낸 결과 파일에는 원본 블록 식별자와 페이지 범위가 포함되므로, 같은 파싱 결과에 분할 계획을 다시
적용하여 동일한 청크를 재현할 수 있습니다.</p>
"""


def font_dir() -> Path:
    for d in FONT_DIRS:
        if Path(d, "NanumGothic.ttf").exists():
            return Path(d)
    raise SystemExit("Nanum Gothic fonts not found (apt install fonts-nanum)")


def write_story(path: Path, title_html: str | None, body_html: str, columns: int, header: str) -> None:
    fonts = font_dir()
    archive = pymupdf.Archive(str(fonts))
    page_rect = pymupdf.paper_rect("a4")
    left, right, top, bottom, gap = 54, page_rect.width - 54, 72, page_rect.height - 64, 18
    width = (right - left - gap * (columns - 1)) / columns
    writer = pymupdf.DocumentWriter(str(path))
    body = pymupdf.Story(html=body_html, user_css=CSS, archive=archive)
    first = True
    more = True
    while more:
        device = writer.begin_page(page_rect)
        body_top = top
        if first and title_html:
            title = pymupdf.Story(html=title_html, user_css=CSS, archive=archive)
            _, filled = title.place(pymupdf.Rect(left, top, right, top + 140))
            title.draw(device)
            body_top = pymupdf.Rect(filled).y1 + 14
        first = False
        for c in range(columns):
            x0 = left + c * (width + gap)
            more, _ = body.place(pymupdf.Rect(x0, body_top, x0 + width, bottom))
            body.draw(device)
            if not more:
                break
        writer.end_page()
    writer.close()

    doc = pymupdf.open(path)
    fontfile = str(fonts / "NanumGothic.ttf")
    for page in doc:
        page.insert_font(fontname="nanum", fontfile=fontfile)
        page.insert_text((left, 44), header, fontname="nanum", fontsize=7.5, color=(0.45, 0.45, 0.45))
        page.insert_text((right - 40, page_rect.height - 36), f"{page.number + 1} / {doc.page_count}",
                         fontname="nanum", fontsize=7.5, color=(0.45, 0.45, 0.45))
    doc.set_metadata({"title": header, "author": "ChunkCanvas sample", "producer": "ChunkCanvas"})
    doc.subset_fonts()
    doc.save(path.with_suffix(".tmp.pdf"), garbage=3, deflate=True)
    doc.close()
    path.with_suffix(".tmp.pdf").replace(path)


def main() -> None:
    write_story(HERE / "chunking-field-guide.pdf", GUIDE_TITLE, GUIDE_BODY, 2, "ChunkCanvas sample · The Chunking Field Guide")
    write_story(HERE / "chunking-guide-ko.pdf", None, KOREAN, 1, "ChunkCanvas 샘플 · 문서 청킹 운영 가이드")
    print("wrote", ", ".join(p.name for p in sorted(HERE.glob("*.pdf"))))


if __name__ == "__main__":
    main()
