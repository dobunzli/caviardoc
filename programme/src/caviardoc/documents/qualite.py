"""Nature d'un PDF : sa couche texte est-elle exploitable, trompeuse ou absente ?

La mesure sur corpus reel a montre qu'un document peut porter une couche texte
abondante et pourtant inexploitable : l'OCR rate d'un formulaire manuscrit
produit des milliers de caracteres illisibles. Une fiche de transfert affichait
1 651 caracteres par page, dont le nom du patient sous la forme
``f t t g ÏÏTRWcKljê^``.

Le danger est precis : anonymiser cette couche texte donne un document qui
parait traite alors que le scan reste parfaitement lisible en dessous. Ces
documents doivent etre orientes vers la chaine image, jamais vers la chaine
texte.

Deux signaux sont combines, calibres sur un corpus de reference de douze
documents medicaux :

- la proportion de jetons de plus d'un caractere ;
- la longueur mediane des jetons.

Aucun des deux ne suffit seul. Le premier condamne a tort les documents sains
dont le pied de page est compose en lettres espacees (« C a b i n e t » fait
chuter la mesure a 77 %). Le second confond une ordonnance laconique avec un
scan rate, tous deux a 3 caracteres de mediane. Ensemble, ils separent les
douze documents sans erreur.

Le seuil penche volontairement vers la chaine image, car l'erreur est
asymetrique : router un document sain vers l'image coute du temps d'OCR et la
selection du texte ; router un document corrompu vers la chaine texte laisse
fuir l'integralite du contenu.

Calibrage a revoir des que le corpus s'elargit : douze documents, c'est peu.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from enum import Enum

from ..concurrence import sous_pdf

MOT = re.compile(r"\S+")

#: En dessous de cette proportion de jetons longs ET de cette longueur mediane,
#: la couche texte est jugee corrompue. Les deux conditions doivent etre
#: reunies : voir la discussion en tete de module.
SEUIL_QUALITE = 0.80
SEUIL_MEDIANE = 3

#: En dessous, on considere qu'il n'y a pas de couche texte du tout.
MOTS_MINIMUM = 20


class Nature(Enum):
    TEXTE = "texte"
    CORROMPUE = "corrompue"
    ABSENTE = "absente"

    @property
    def chaine_image(self) -> bool:
        """La chaine image est-elle obligatoire pour ce document ?"""
        return self is not Nature.TEXTE


@dataclass(frozen=True)
class Diagnostic:
    nature: Nature
    qualite: float
    mots: int
    pages: int
    mediane: float = 0.0

    @property
    def explication(self) -> str:
        if self.nature is Nature.ABSENTE:
            return "aucune couche texte : OCR obligatoire"
        if self.nature is Nature.CORROMPUE:
            return (f"couche texte corrompue ({self.qualite*100:.0f} % de mots "
                    f"exploitables, mediane {self.mediane:.0f} caracteres) : "
                    "probablement l'OCR rate d'un document manuscrit ou "
                    "scanne. Traitement par la chaine image.")
        return f"couche texte saine ({self.qualite*100:.0f} %)"


def qualite_couche_texte(texte: str) -> tuple[float, int, float]:
    """Proportion de jetons longs, nombre de jetons, longueur mediane."""
    jetons = MOT.findall(texte)
    if not jetons:
        return 0.0, 0, 0.0
    isoles = sum(1 for j in jetons if len(j) == 1)
    mediane = statistics.median(len(j) for j in jetons)
    return 1 - isoles / len(jetons), len(jetons), mediane


def diagnostiquer(texte: str, pages: int = 1) -> Diagnostic:
    q, n, med = qualite_couche_texte(texte)
    if n < MOTS_MINIMUM:
        return Diagnostic(Nature.ABSENTE, q, n, pages, med)
    if q < SEUIL_QUALITE and med <= SEUIL_MEDIANE:
        return Diagnostic(Nature.CORROMPUE, q, n, pages, med)
    return Diagnostic(Nature.TEXTE, q, n, pages, med)


@sous_pdf
def diagnostiquer_pdf(chemin) -> Diagnostic:
    import pymupdf
    d = pymupdf.open(chemin)
    try:
        return diagnostiquer("\n".join(p.get_text() for p in d), len(d))
    finally:
        d.close()
