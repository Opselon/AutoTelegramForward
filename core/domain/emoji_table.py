"""Extended_Pictographic code-point ranges — shared emoji-cleanup table.

Generated from github.com/rivo/uniseg@v0.4.7 (graphemeproperties.go, which is
itself generated from the Unicode 15.0.0 emoji-data.txt Extended_Pictographic
property). This is the single source of truth for "which code points are
emoji" for BOTH engines: the Go Worker uses uniseg's table directly and the
Python side imports this module, so a Unicode update is one regeneration.

Do not edit by hand — regenerate with scripts/gen_emoji_table.py.
"""

# (low, high) inclusive code-point ranges, sorted, adjacent ranges merged.
EXTENDED_PICTOGRAPHIC_RANGES = [
	(0x00A9, 0x00A9),
	(0x00AE, 0x00AE),
	(0x200D, 0x200D),
	(0x203C, 0x203C),
	(0x2049, 0x2049),
	(0x20E3, 0x20E3),
	(0x2122, 0x2122),
	(0x2139, 0x2139),
	(0x2194, 0x2199),
	(0x21A9, 0x21AA),
	(0x231A, 0x231B),
	(0x2328, 0x2328),
	(0x2388, 0x2388),
	(0x23CF, 0x23CF),
	(0x23E9, 0x23F3),
	(0x23F8, 0x23FA),
	(0x24C2, 0x24C2),
	(0x25AA, 0x25AB),
	(0x25B6, 0x25B6),
	(0x25C0, 0x25C0),
	(0x25FB, 0x25FE),
	(0x2600, 0x2605),
	(0x2607, 0x2612),
	(0x2614, 0x2685),
	(0x2690, 0x2705),
	(0x2708, 0x2712),
	(0x2714, 0x2714),
	(0x2716, 0x2716),
	(0x271D, 0x271D),
	(0x2721, 0x2721),
	(0x2728, 0x2728),
	(0x2733, 0x2734),
	(0x2744, 0x2744),
	(0x2747, 0x2747),
	(0x274C, 0x274C),
	(0x274E, 0x274E),
	(0x2753, 0x2755),
	(0x2757, 0x2757),
	(0x2763, 0x2767),
	(0x2795, 0x2797),
	(0x27A1, 0x27A1),
	(0x27B0, 0x27B0),
	(0x27BF, 0x27BF),
	(0x2934, 0x2935),
	(0x2B05, 0x2B07),
	(0x2B1B, 0x2B1C),
	(0x2B50, 0x2B50),
	(0x2B55, 0x2B55),
	(0x3030, 0x3030),
	(0x303D, 0x303D),
	(0x3297, 0x3297),
	(0x3299, 0x3299),
	(0xFE00, 0xFE0F),
	(0x1F000, 0x1F0FF),
	(0x1F10D, 0x1F10F),
	(0x1F12F, 0x1F12F),
	(0x1F16C, 0x1F171),
	(0x1F17E, 0x1F17F),
	(0x1F18E, 0x1F18E),
	(0x1F191, 0x1F19A),
	(0x1F1AD, 0x1F1FF),
	(0x1F201, 0x1F20F),
	(0x1F21A, 0x1F21A),
	(0x1F22F, 0x1F22F),
	(0x1F232, 0x1F23A),
	(0x1F23C, 0x1F23F),
	(0x1F249, 0x1F53D),
	(0x1F546, 0x1F64F),
	(0x1F680, 0x1F6FF),
	(0x1F774, 0x1F77F),
	(0x1F7D5, 0x1F7FF),
	(0x1F80C, 0x1F80F),
	(0x1F848, 0x1F84F),
	(0x1F85A, 0x1F85F),
	(0x1F888, 0x1F88F),
	(0x1F8AE, 0x1F8FF),
	(0x1F90C, 0x1F93A),
	(0x1F93C, 0x1F945),
	(0x1F947, 0x1FAFF),
	(0x1FC00, 0x1FFFD),
]


def is_extended_pictographic(code_point: int) -> bool:
    """True when the code point carries Unicode's Extended_Pictographic property."""
    import bisect

    lo, hi = 0, len(EXTENDED_PICTOGRAPHIC_RANGES) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        low, high = EXTENDED_PICTOGRAPHIC_RANGES[mid]
        if code_point < low:
            hi = mid - 1
        elif code_point > high:
            lo = mid + 1
        else:
            return True
    return False


# The zero-width joiners, variation selectors, keycap enclosing marks, regional
# indicators and skin-tone modifiers that turn pictographs into emoji
# sequences. A cleanup that removes the pictograph but leaves these produces
# the dangling-joiner corruption F-03 pinned.
EMOJI_SEQUENCE_MARKERS = {
    0x200D,  # ZERO WIDTH JOINER
    0xFE00, 0xFE01, 0xFE02, 0xFE03, 0xFE04, 0xFE05, 0xFE06, 0xFE07,
    0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFE0D, 0xFE0E, 0xFE0F,  # VS1..VS16
    0x20E3,  # COMBINING ENCLOSING KEYCAP
    0x1F1E6, 0x1F1E7, 0x1F1E8, 0x1F1E9, 0x1F1EA, 0x1F1EB, 0x1F1EC,
    0x1F1ED, 0x1F1EE, 0x1F1EF, 0x1F1F0, 0x1F1F1, 0x1F1F2, 0x1F1F3,
    0x1F1F4, 0x1F1F5, 0x1F1F6, 0x1F1F7, 0x1F1F8, 0x1F1F9, 0x1F1FA,
    0x1F1FB, 0x1F1FC, 0x1F1FD, 0x1F1FE, 0x1F1FF,  # regional indicators
    0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF,  # skin tone modifiers
}


def _is_keycap_base(ch: str) -> bool:
    """A keycap base: the digits, Latin letters, '*' and '#' that Unicode's
    keycap sequences are built on. Removing the enclosing mark but leaving
    the base would turn "5\u20e3" into "5"; removing the base but leaving
    the mark would leave a dangling U+20E3.
    """
    return ch in _KEYCAP_BASES


_KEYCAP_BASES = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ*#"
)

# The code points that can sit between two emoji code points and bind them
# into one sequence: ZWJ and the variation selectors.
_SEQUENCE_SEPARATORS = frozenset({0x200D} | {cp for cp in range(0xFE00, 0xFE10)})


def strip_emoji(text: str) -> str:
    """Remove emoji sequences grapheme-correctly (F-03 fix).

    A whole emoji sequence — pictographs joined by ZWJ, keycaps, flags, and
    any pictograph carrying a variation selector or skin tone — is removed as
    a unit, so no dangling joiner or orphaned keycap survives. Non-emoji
    supplementary-plane scripts (cuneiform, math alphanumerics) are not
    Extended_Pictographic and pass through untouched.
    """
    if not text:
        return text

    def emoji(cp: int) -> bool:
        return is_extended_pictographic(cp) or cp in EMOJI_SEQUENCE_MARKERS

    out = []
    i = 0
    n = len(text)
    while i < n:
        cp = ord(text[i])
        if not emoji(cp):
            out.append(text[i])
            i += 1
            continue

        # Consume one emoji sequence: a maximal run of emoji code points,
        # extended across any ZWJ or variation selector that is followed by
        # another emoji code point or a keycap base. The scan only advances,
        # so it cannot loop.
        j = i
        while j < n:
            c = ord(text[j])
            if emoji(c):
                j += 1
                continue
            if c in _SEQUENCE_SEPARATORS and j + 1 < n and (
                emoji(ord(text[j + 1])) or _is_keycap_base(text[j + 1])
            ):
                j += 1
                continue
            break

        # A keycap is <base><FE0F?><20E3> and the base is NOT an emoji code
        # point, so the run above starts at FE0F/20E3 and would leave the
        # base behind ("5\u20e3" -> "5"). When the run started just after a
        # keycap base — i.e. the run's first code point is FE0F or 20E3 and
        # the character before the run is a keycap base — extend the removal
        # backwards over that base. The whole keycap goes, base included.
        if i > 0 and _is_keycap_base(text[i - 1]) and (cp == 0x20E3 or cp == 0xFE0F):
            out.pop()  # the base character, appended a step ago

        i = j
    return "".join(out)
