"""Word spacing for Korean text.

The Korean PP-OCR recognizer reads the characters well but often drops the spaces between words
("기존기술현황"), worst on noisy scans. Kiwi's spacing model puts them back; spaces the OCR did
read are kept. Text-layer text only asks it where a line break falls: Korean justified text
breaks lines inside words ("최" / "신의"), so a line break is not always a space.
"""
import re

HANGUL = re.compile(r"[가-힣]")
_kiwi = None


def _model():
    global _kiwi
    if _kiwi is None:
        try:
            from kiwipiepy import Kiwi
        except ImportError:  # optional dependency: unspaced text is still searchable
            return None
        _kiwi = Kiwi()
    return _kiwi


def one_word(left: str, right: str) -> bool:
    """Whether the end of one line and the start of the next are halves of one word."""
    kiwi = _model()
    if kiwi is None or not left or not right:
        return False
    spaced = kiwi.space(left + right, reset_whitespace=True)
    seen = 0
    for i, c in enumerate(spaced):
        if c != " ":
            seen += 1
            if seen == len(left):
                return i + 1 >= len(spaced) or spaced[i + 1] != " "
    return False


def restore_spacing(text: str) -> str:
    if not text or not HANGUL.search(text):
        return text
    _kiwi = _model()
    if _kiwi is None:
        return text
    return "\n".join(_kiwi.space(line, reset_whitespace=False) if HANGUL.search(line) else line for line in text.split("\n"))
