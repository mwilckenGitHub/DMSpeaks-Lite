"""Select the fixed word immediately before a spoken replacement."""

from dataclasses import dataclass
import re


WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
ARPABET_VOWELS = frozenset('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
ARPABET_CONSONANTS = frozenset('B CH D DH F G HH JH K L M N NG P R S SH T TH V W Y Z ZH'.split())


@dataclass(frozen=True)
class LeadingWord:
    start: int
    end: int
    text: str
    indefinite_article: bool


def leading_word(text, start, delimiter_spans, cue_positions=()):
    """Return a fixed word that can share ownership with this occurrence."""
    match = None
    for match in WORD.finditer(text, 0, start):
        pass
    if match is None:
        return None
    left, right = match.span()
    if any(a < right and left < b for a, b in delimiter_spans):
        return None
    if any(left <= point <= start for point in cue_positions):
        return None
    word = match.group()
    return LeadingWord(left, right, word,
                       word.casefold() in {'a', 'an'} and not text[right:start].strip())


def indefinite_article(original, onsets):
    """Choose a/an by the spoken onset and preserve initial capitalization."""
    phones = {phone.rstrip('012') for phone in onsets}
    if phones and phones <= ARPABET_VOWELS:
        article = 'an'
    elif phones and phones <= ARPABET_CONSONANTS:
        article = 'a'
    else:
        raise ValueError('The replacement needs a resolved vowel or consonant onset.')
    return article.capitalize() if original[0].isupper() else article
