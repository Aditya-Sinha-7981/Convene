"""Devanagari to Hinglish-style Latin letters: the backstop that keeps ADR-24 true for cloud STT.

Convene stores Hindi the way people type it ("kya kar rahe ho"), never in Devanagari. The local Whisper adapter
cannot produce Devanagari (it decodes in English mode). A cloud model can ignore its instruction, so any Devanagari
it returns is converted here, deterministically, before the text is stored.

It is a readable approximation, not a formal transliteration. Consonants carry an inherent "a" that Hindi drops at the
end of a word and in the middle of V C _ C V (so करना becomes "karna", not "karana"). Long vowels are doubled inside
a word and single at its end ("kaam", "raha", "theek", "bhi"). Nukta letters map to their usual Hinglish spelling
(ज़ z, फ़ f, ड़ d).
"""
import re

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "ch", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f", "य़": "y",
}
NUKTA_FORMS = {"क": "क़", "ख": "ख़", "ग": "ग़", "ज": "ज़", "ड": "ड़", "ढ": "ढ़", "फ": "फ़", "य": "य़"}
# (inside a word, at the end of a word)
VOWELS = {
    "अ": ("a", "a"), "आ": ("aa", "a"), "इ": ("i", "i"), "ई": ("ee", "i"), "उ": ("u", "u"), "ऊ": ("oo", "u"),
    "ए": ("e", "e"), "ऐ": ("ai", "ai"), "ओ": ("o", "o"), "औ": ("au", "au"), "ऋ": ("ri", "ri"), "ऑ": ("o", "o"),
    "ऍ": ("e", "e"),
}
MATRAS = {
    "ा": ("aa", "a"), "ि": ("i", "i"), "ी": ("ee", "i"), "ु": ("u", "u"), "ू": ("oo", "u"), "ृ": ("ri", "ri"),
    "े": ("e", "e"), "ै": ("ai", "ai"), "ो": ("o", "o"), "ौ": ("au", "au"), "ॉ": ("o", "o"), "ॅ": ("e", "e"),
}
# Very common words people spell a fixed way in Hinglish, where the letter rules would read oddly.
COMMON = {"हम": "hum", "तुम": "tum", "में": "mein", "मैं": "main", "यह": "yeh", "वह": "woh", "नहीं": "nahi",
          "और": "aur", "ये": "ye", "वो": "wo", "है": "hai", "हैं": "hain", "क्या": "kya", "हाँ": "haan", "हां": "haan"}
VIRAMA, NUKTA, ANUSVARA, CHANDRABINDU, VISARGA = "्", "़", "ं", "ँ", "ः"
DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
WORD = re.compile(r"[ऀ-ॿ]+")


def has_devanagari(text: str) -> bool:
    return bool(re.search(r"[ऀ-ॿ]", text))


def _word(word: str) -> str:
    if word in COMMON:
        return COMMON[word]
    # units: [text, kind] with kind C (consonant), A (inherent a), V (vowel, (medial, final)), N (nasal)
    units: list[list] = []
    for ch in word:
        if ch == NUKTA and units and units[-1][1] == "A" and len(units) >= 2:
            base = units[-2][2]
            if base in NUKTA_FORMS:
                units[-2] = [CONSONANTS[NUKTA_FORMS[base]], "C", NUKTA_FORMS[base]]
        elif ch in CONSONANTS:
            units += [[CONSONANTS[ch], "C", ch], [("a", "a"), "A", None]]
        elif ch in MATRAS and units and units[-1][1] == "A":
            units[-1] = [MATRAS[ch], "V", None]
        elif ch == VIRAMA and units and units[-1][1] == "A":
            units.pop()
        elif ch in VOWELS:
            units.append([VOWELS[ch], "V", None])
        elif ch in (ANUSVARA, CHANDRABINDU):
            units.append(["n", "N", None])
        elif ch == VISARGA:
            units.append(["h", "C", None])
        elif ch in ("।", "॥"):
            units.append([".", "P", None])
        else:
            units.append([ch.translate(DIGITS), "P", None])
    vowels = [i for i, u in enumerate(units) if u[1] in ("A", "V")]
    # Hindi drops the inherent "a" at the end of a word (but a one-syllable word keeps it: न is "na").
    if len(vowels) > 1 and units[vowels[-1]][1] == "A" and vowels[-1] == len(units) - 1:
        units[vowels[-1]][1] = "X"
    # ...and in V C _ C V, scanning right to left (करना -> karna, समझना -> samajhna, कमरा -> kamra).
    for i in range(len(units) - 3, 1, -1):
        u = units[i]
        if (u[1] == "A" and units[i - 1][1] == "C" and units[i - 2][1] in ("A", "V") and units[i + 1][1] == "C"
                and units[i + 2][1] in ("A", "V")):
            u[1] = "X"
    kept = [u for u in units if u[1] != "X"]
    out = []
    for n, (text, kind, _) in enumerate(kept):
        if kind in ("A", "V"):
            at_end = all(k[1] in ("P", "N") for k in kept[n + 1:])
            out.append(text[1] if at_end else text[0])
        elif kind == "N":
            following = next((k[0] for k in kept[n + 1:] if k[1] == "C"), "")
            out.append("m" if following[:1] in ("p", "b", "m") else "n")
        else:
            out.append(text)
    return "".join(out).replace("chch", "cch")


def romanize(text: str) -> str:
    """Replace every run of Devanagari in ``text`` with Hinglish-style Latin letters; other text is unchanged."""
    if not has_devanagari(text):
        return text
    result = WORD.sub(lambda m: _word(m.group(0)), text)
    return re.sub(r"\s+([.,!?])", r"\1", result)
