# HTTP API

Text offsets in split plans and API responses use UTF-16 code units, as in JavaScript strings.
Cuts cannot fall between surrogate halves. Parsed API responses carry `offsetEncoding: "utf16"`.
Legacy files on disk use code-point line positions; the API converts those positions without
rewriting the file or changing the fingerprint. Saved plan offsets already use UTF-16 and are
not converted. Core defines presentation order (including page notes); the API validates each
position and increasing, unique cuts within a block without imposing a second block order.

The OpenAPI schema is served at `/api/openapi.json` (interactive docs at `/api/docs`). All
responses use JSON except the PDF download. Errors look like:

```json
{ "detail": { "code": "too_many_pages", "message": "The PDF has 45 pages; this parser accepts up to 30." } }
```

Documents are private: every document endpoint requires the access token returned when the
document was created, as `Authorization: Bearer <token>`. Unknown ids, wrong tokens and expired
documents all return `404 not_found`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness check |
| GET | `/api/config` | Available parsers, limits, retention |
| GET | `/api/samples` | Bundled sample documents |
| POST | `/api/samples/{key}/documents?parser=` | Create a private copy of a sample |
| POST | `/api/documents?parser=` | Upload a PDF (raw body, `Content-Type: application/pdf`, optional `X-Filename`) |
| GET | `/api/documents/{id}/status` | Parse status: `queued`, `processing`, `completed`, `failed` |
| GET | `/api/documents/{id}` | Status plus the parsed document and the saved split plan |
| GET | `/api/documents/{id}/file` | The original PDF |
| PUT | `/api/documents/{id}/split-plan` | Save the split plan (optimistic concurrency) |
| POST | `/api/documents/{id}/parse?parser=` | Parse again with another parser (discards the split plan) |
| DELETE | `/api/documents/{id}` | Delete the document and everything derived from it |

## Upload

```bash
curl -sS -X POST 'http://localhost:8300/api/documents?parser=docling' \
  -H 'Content-Type: application/pdf' -H 'X-Filename: report.pdf' \
  --data-binary @report.pdf
```

Returns `201` with the status payload plus `accessToken`. The upload is checked for size, the
`%PDF-` signature, encryption, damage and page count before it is queued. Parsing runs in a
background job; poll `/status` (it includes `progress` and `queuePosition`).

## Saving a split plan

```json
PUT /api/documents/{id}/split-plan
{ "baseRevision": 3, "plan": { "documentId": "…", "blocksFingerprint": "…", "revision": 3,
  "boundaries": [{ "blockId": "b0006" }, { "blockId": "b0014", "offset": 212 }],
  "settings": { "targetTokens": 500, "maxTokens": 800, "overlapTokens": 0, "structureAware": true,
  "keepHeadingWithBody": true, "excludeFurniture": true },
  "excluded": [{ "blockId": "b0002", "start": 0, "end": 48 }] } }
```

A boundary without `offset` ends a chunk after that block; with `offset`, the next chunk starts at
that character of the block's text. The server checks that the plan belongs to this parse
(`blocksFingerprint`), that every boundary names a known block, that offsets fall strictly inside
blocks that have character positions, that cuts are unique and increasing within each block,
that deleted text (`excluded`, optional) lies inside known blocks (an empty range only for a
block without text), and that `baseRevision` is the latest saved revision. A stale `baseRevision` returns
`409 stale_revision` with the current revision and plan, so an old tab can never overwrite newer
edits.

## Where are the chunks?

Chunks are computed from the parsed blocks and the split plan by `packages/core`, which is the
only chunking implementation. The browser computes them on every edit and builds the exports, so
the server has no chunk or export endpoints (the original draft listed `GET /chunks` and
`POST /exports`). To produce chunks headlessly, fetch `GET /api/documents/{id}` and run
`normalizeSavedPlan(parsed, splitPlan)` from `@chunkcanvas/core` first. The API checks stored
positions and revisions; Core alone determines reading order, including footnotes and excluded
page furniture. Raw stored plans can therefore contain boundaries that Core reorders or removes.
Keep the original plan and inspect the returned `warnings` when `changed` is true. A `null` result
means the plan cannot be used with this document. Pass the normalized `plan` to `generateChunks`
or `buildJsonExport` (and a tokenizer as `encoder` to apply overlap). These export functions
intentionally reject noncanonical plans rather than silently exporting different edits.

## Importing exported plans after a parser update

Split-plan file version 2 optionally includes `blockAnchors` with a source block ID, type and
`textHash` (lowercase SHA-256 of the exact UTF-8 text). These anchors contain no body text.
Export includes anchors only for blocks whose type and content are unique in the source parse.
On a fingerprint mismatch, Core maps a boundary only when its anchor also matches exactly one
block in the new parse. Ambiguous/missing matches are reported; if none match, current boundaries
are kept. Imported deletion ranges are never applied to a different parse. Existing deletion
ranges in the open document remain. Older files without anchors still work with their original
fingerprint; a different parse cannot safely recover their boundaries from block numbers alone.
