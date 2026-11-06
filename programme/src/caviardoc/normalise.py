"""Normalisation de casse, a longueur constante.

Les documents medicaux suisses ecrivent tres souvent les patronymes en
capitales — « ZORGLUB Jean-Bidule », « TARTEMPION Machin ». Le modele les manque
systematiquement : sur un rapport de laboratoire (exemple invente), le nom du medecin en
capitales a fui alors que le prenom en casse normale, situe juste a cote, etait
detecte.

La parade consiste a detecter une seconde fois sur une copie du texte remise en
casse de titre.

VALIDATION — mesuree sur corpus reel avec caviardage humain de reference :
la passe fait passer le rappel de 64 % a 66 %. Le gain est reel mais modeste,
et il se paie au prix fort : le temps de traitement double, puisqu'il faut
inferer deux fois. A desactiver (`normalise_case=False`) si la latence prime
sur les deux points de rappel. Point essentiel : la transformation **preserve la longueur
caractere par caractere**, donc les offsets obtenus sur la copie restent
valides sur l'original. Aucune reprojection n'est necessaire.

Tout le texte en capitales n'est pas un nom : les titres de sections
(« ECHO-DOPPLER VEINEUX DU MEMBRE INFERIEUR GAUCHE ») et les noms de
medicaments (« ATACAND cpr 16 mg ») le sont aussi. Les normaliser sans
discernement fabriquerait des faux positifs. On ne transforme donc qu'un jeton
en capitales **voisin, sur la meme ligne, d'un autre jeton en capitales ou en
casse de titre** — la forme que prend un nom propre, et pas un titre de section
isole ni un nom de medicament suivi d'une posologie en minuscules.
"""

from __future__ import annotations

import re

#: Un jeton alphabetique, accents compris, avec traits d'union et apostrophes.
_TOKEN = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]*")

#: Longueur minimale pour qu'un mot en capitales soit candidat.
MIN_LEN = 2

#: Longueur maximale d'une suite de mots en capitales pouvant former un nom.
#: Un patronyme fait un a trois mots ; au-dela, c'est un titre de section
#: (« ECHO-DOPPLER VEINEUX DU MEMBRE INFERIEUR GAUCHE »), et le normaliser
#: fabriquerait un faux positif qui detruirait le titre du document.
MAX_RUN = 3

#: Jetons en capitales qui ne sont jamais des patronymes.
STOPWORDS = {
    "FMH", "CH", "CHUV", "HUG", "CHF", "AVS", "IBAN", "NPA", "SA", "SARL",
    "DR", "PR", "MME", "MLLE", "NE", "VD", "GE", "FR", "JU", "BE", "VS",
    "TVP", "PSA", "ENMG", "IRM", "CT", "US", "ECG", "EEG", "ORL",
}


def _is_caps(tok: str) -> bool:
    return (len(tok) >= MIN_LEN and tok.isupper()
            and tok.upper() not in STOPWORDS and any(c.isalpha() for c in tok))


def _is_title(tok: str) -> bool:
    return (len(tok) >= MIN_LEN and tok[0].isupper()
            and any(c.islower() for c in tok[1:]))


def _titlecase(tok: str) -> str:
    """Casse de titre en preservant exactement la longueur.

    ``str.title()`` ne convient pas : il recasse apres chaque apostrophe et
    peut modifier la longueur sur certains caracteres (le S long, par exemple).
    """
    out = [tok[0].upper()]
    for ch in tok[1:]:
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


def normalise_casse(text: str) -> str:
    """Copie du texte, patronymes en capitales remis en casse de titre.

    Garantit ``len(resultat) == len(text)``.
    """
    # Jetons regroupes par ligne, pour que le voisinage ne franchisse pas un
    # saut de ligne : deux lignes differentes ne forment pas un nom.
    tokens: list[tuple[int, int, str, int]] = []  # (debut, fin, texte, no de ligne)
    line_no = 0
    pos = 0
    for line in text.splitlines(keepends=True):
        for m in _TOKEN.finditer(line):
            tokens.append((pos + m.start(), pos + m.end(), m.group(), line_no))
        pos += len(line)
        line_no += 1

    # Reperer les suites de mots en capitales consecutifs sur une meme ligne.
    runs: list[list[int]] = []
    current: list[int] = []
    for i, (_, _, tok, ln) in enumerate(tokens):
        same_line = bool(current) and tokens[current[-1]][3] == ln
        if _is_caps(tok) and (not current or same_line):
            current.append(i)
        else:
            if current:
                runs.append(current)
            current = [i] if _is_caps(tok) else []
    if current:
        runs.append(current)

    to_change: list[tuple[int, int, str]] = []
    for run in runs:
        if len(run) > MAX_RUN:
            continue  # titre de section, pas un nom
        first, last = run[0], run[-1]
        ln = tokens[first][3]
        neighbours = []
        if first > 0 and tokens[first - 1][3] == ln:
            neighbours.append(tokens[first - 1][2])
        if last + 1 < len(tokens) and tokens[last + 1][3] == ln:
            neighbours.append(tokens[last + 1][2])
        # Une suite d'au moins deux mots en capitales forme deja un nom
        # plausible ; un mot isole doit etre accole a un mot en casse de titre.
        if len(run) >= 2 or any(_is_title(n) for n in neighbours):
            for i in run:
                start, end, tok, _ = tokens[i]
                to_change.append((start, end, _titlecase(tok)))

    if not to_change:
        return text

    chars = list(text)
    for start, end, replacement in to_change:
        assert end - start == len(replacement)
        chars[start:end] = replacement
    result = "".join(chars)
    assert len(result) == len(text), "la normalisation doit preserver la longueur"
    return result
