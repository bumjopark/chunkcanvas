"""Python parsers use code points; the public editing protocol uses UTF-16 offsets.

Keep legacy parses unchanged on disk. Convert at the API boundary and mark the
result, so gallery files made from API responses are never converted twice.
"""


def positions(text: str) -> list[int]:
    out = [0]
    for char in text:
        out.append(out[-1] + (2 if ord(char) > 0xFFFF else 1))
    return out


def length(text: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def wire_document(parsed: dict | None) -> dict | None:
    if parsed is None or parsed.get("offsetEncoding") == "utf16":
        return parsed
    blocks = []
    for block in parsed["blocks"]:
        pos = positions(block["text"])
        lines = []
        for line in block.get("lines") or []:
            start, end = line["start"], line["end"]
            xs = [x for i, x in enumerate(line["xs"], start)
                  for _ in range(pos[i + 1] - pos[i])]
            lines.append({**line, "start": pos[start], "end": pos[end], "xs": xs})
        blocks.append({**block, **({"lines": lines, "lineGeometry": block.get("lineGeometry") or "unverified"}
                                  if block.get("lines") else {})})
    return {**parsed, "blocks": blocks, "offsetEncoding": "utf16"}
