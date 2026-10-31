"""
Dean Edwards P.A.C.K.E.R. unpacker for Python (Standalone, zero dependencies).
Used for unpacking obfuscated player scripts (e.g., Mp4Upload).
"""

import re
import string


class UnpackingError(Exception):
    pass


def detect(source: str) -> bool:
    """Detects whether `source` is P.A.C.K.E.R. coded."""
    return re.search(r"eval[ ]*\([ ]*function[ ]*\([ ]*p[ ]*,[ ]*a[ ]*,[ ]*c[ ]*,[ ]*k[ ]*,[ ]*e[ ]*,", source) is not None


def unpack(source: str) -> str:
    """Unpacks P.A.C.K.E.R. packed javascript."""
    match = re.search(
        r"\}\s*\(\s*'(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'\.split\('\|'\)",
        source,
        re.DOTALL
    )
    if not match:
        # Fallback to alternate regex variant
        match = re.search(
            r"\}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)",
            source,
            re.DOTALL
        )
    if not match:
        raise UnpackingError("Could not parse P.A.C.K.E.R. arguments.")

    payload, radix_str, count_str, symtab_str = match.groups()
    radix = int(radix_str)
    count = int(count_str)
    symtab = symtab_str.split('|')

    if count != len(symtab):
        # Some packers have trailing empty split entries
        pass

    def unbase(s, rad):
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = 0
        for char in s:
            res = res * rad + alphabet.index(char)
        return res

    def lookup(match_obj):
        word = match_obj.group(0)
        try:
            if radix <= 36:
                idx = int(word, radix)
            else:
                idx = unbase(word, radix)
            if idx < len(symtab) and symtab[idx]:
                return symtab[idx]
        except Exception:
            pass
        return word

    return re.sub(r'\b\w+\b', lookup, payload)
