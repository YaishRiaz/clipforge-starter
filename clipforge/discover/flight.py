"""Read the data a Next.js page ships with itself.

Both Content Rewards and Vyro render their campaign lists on the server and
embed the same data as `self.__next_f.push([1,"..."])` chunks. Joining the
chunks gives one text with plain JSON objects inside it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_DECODER = json.JSONDecoder()


def decode(html: str) -> str:
    chunks = []
    for raw in _PUSH.findall(html):
        try:
            chunks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def objects_after(text: str, marker: str) -> Iterator[Any]:
    """Every JSON value that directly follows `marker` in the text."""
    pos = 0
    while True:
        i = text.find(marker, pos)
        if i < 0:
            return
        start = i + len(marker)
        try:
            value, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start
            continue
        yield value
        pos = end
