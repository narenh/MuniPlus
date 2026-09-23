"""The naming rules shared by line names and stop names.

The word lists come from tools/build_bus_data.py, the script behind the names the
App Store app already shows, so a name reads the same whichever path built it.
"""

import re

ACRONYMS = {"sf", "bart", "ucsf", "sfsu", "ccsf", "usf", "va", "vamc", "sfo", "mlk", "ymca"}
MINOR_WORDS = {"at", "of", "the", "and", "to", "via", "on"}
"""Lower case unless they start a name or follow punctuation ("& The Embarcadero")."""
FIXUPS = {"Mcallister": "McAllister"}

_WORD = re.compile(r"[A-Za-z]+")
_MC = re.compile(r"Mc[a-z]+")


def tidy_stop_name(name: str) -> str:
    """Undo the casing 511 applies when it republishes SFMTA's stop names.

    511 title-cases every word mechanically, so SFMTA's "UCSF" arrives as "Ucsf",
    "McAllister" as "Mcallister" and "Legion of Honor" as "Legion Of Honor". On
    the 2026-08-29 feed that is 17 acronyms, 30 Mc names and 21 small words in
    3,240 stops. Only casing changes; the words are 511's.
    """

    def fix(match: re.Match) -> str:
        word = match.group(0)
        lower = word.lower()
        if lower in ACRONYMS:
            return word.upper()
        if _MC.fullmatch(word):
            return "Mc" + word[2].upper() + word[3:]
        before = name[: match.start()].rstrip()
        if lower in MINOR_WORDS and before and before[-1].isalnum():
            return lower
        return word

    return _WORD.sub(fix, name)
