package cleaning

// emoji.go — Unicode-correct emoji removal (the F-03 fix).
//
// The pre-4.4 pattern
// ([\x{10000}-\x{10ffff}]|[\x{2600}-\x{27bf}]|[\x{2300}-\x{23ff}]|[\x{2b50}-\x{2b55}]|[\x{fe00}-\x{fe0f}])
// matched EVERY non-BMP code point, so cuneiform, math alphanumerics and
// other non-emoji supplementary-plane scripts were destroyed, ZWJ family
// emoji were shredded into dangling joiners, and keycaps lost their
// enclosing mark but kept the base digit. Both engines did this; the audit
// pinned it as F-03 with real customer impact.
//
// This implementation removes each emoji SEQUENCE as a unit — pictographs
// joined by ZWJ, flags (regional indicator pairs), skin-toned pictographs,
// keycaps and variation-selector sequences — and leaves every
// non-Extended_Pictographic code point alone. The predicate comes from the
// same Unicode 15.0.0 Extended_Pictographic table the Python side reads
// (core/domain/emoji_table.py), generated from the same source, so the two
// engines cannot drift.
//
// The table is a sorted, merged range array; membership is a binary search,
// which is why this file carries no compiled regex at all.

// isEmojiRune reports whether the rune is an Extended_Pictographic code
// point or one of the emoji-sequence markers (ZWJ, variation selectors, the
// keycap enclosing mark, regional indicators, skin-tone modifiers).
func isEmojiRune(r rune) bool {
	lo, hi := 0, len(extendedPictographicRanges)-1
	for lo <= hi {
		mid := (lo + hi) / 2
		low, high := extendedPictographicRanges[mid][0], extendedPictographicRanges[mid][1]
		switch {
		case r < low:
			hi = mid - 1
		case r > high:
			lo = mid + 1
		default:
			return true
		}
	}
	return false
}

// isKeycapBase reports whether the rune can be the base of a Unicode keycap
// sequence (<base><FE0F?><20E3>): digits, ASCII letters, '*' and '#'.
// Removing the enclosing mark but leaving the base turns "5️⃣" into "5";
// removing the base but leaving the mark leaves a dangling U+20E3.
func isKeycapBase(r rune) bool {
	switch {
	case r >= '0' && r <= '9':
		return true
	case r >= 'a' && r <= 'z':
		return true
	case r >= 'A' && r <= 'Z':
		return true
	case r == '*' || r == '#':
		return true
	}
	return false
}

// sequenceSeparator reports whether the rune can bind the following code
// point into the emoji sequence in front of it: ZWJ and the variation
// selectors.
func sequenceSeparator(r rune) bool {
	if r == 0x200D {
		return true
	}
	return r >= 0xFE00 && r <= 0xFE0F
}

// RemoveEmojisGrapheme removes emoji sequences, keeping everything else
// byte-identical. It is the F-03-correct replacement for the old regex
// pass; the pipeline calls this when the task asked for emoji removal.
func RemoveEmojisGrapheme(text string) string {
	if text == "" {
		return text
	}
	runes := []rune(text)
	out := make([]rune, 0, len(runes))
	i := 0
	n := len(runes)
	for i < n {
		cp := runes[i]
		if !isEmojiRune(cp) {
			out = append(out, cp)
			i++
			continue
		}
		// Consume one emoji sequence: a maximal run of emoji runes,
		// extended across a ZWJ or variation separator when the rune after
		// it is another emoji rune or a keycap base. The scan only
		// advances, so it cannot loop.
		j := i
		for j < n {
			c := runes[j]
			if isEmojiRune(c) {
				j++
				continue
			}
			if sequenceSeparator(c) && j+1 < n &&
				(isEmojiRune(runes[j+1]) || isKeycapBase(runes[j+1])) {
				j++
				continue
			}
			break
		}
		// A keycap is <base><FE0F?><20E3> and its base is NOT an emoji
		// rune, so the run above starts at FE0F/20E3 and would leave the
		// base behind. When the run started just after a keycap base —
		// the first rune of the run is FE0F or 20E3 and the rune before
		// the run is a keycap base — extend the removal backwards over
		// that base. The whole keycap goes, base included.
		if i > 0 && isKeycapBase(runes[i-1]) && (cp == 0x20E3 || cp == 0xFE0F) {
			if len(out) > 0 {
				out = out[:len(out)-1]
			}
		}
		i = j
	}
	return string(out)
}
