"""Dates relatives : « J0, J+4, J+371 » au lieu des dates.

Dans l'extrait d'arret du Tribunal federal qu'une IA a suffi a re-identifier,
c'est la date qui ancrait le recoupement : un evenement date se cherche. Or ce
qui porte l'information clinique est presque toujours l'intervalle — trois
jours entre la chute et l'operation, un an entre deux controles. Masquer
toutes les dates le detruit, les garder toutes livre l'ancre.

Chaque date (hors date de naissance) est remplacee par son ecart en jours avec
la plus ancienne du document, qui devient J0. L'origine est propre a chaque
document : deux documents d'un meme lot n'ont pas de J0 commun, et ne se
recoupent donc pas par leurs dates.

La date de naissance reste masquee comme avant : son ecart a J0 donnerait
l'age exact au jour pres. Une date qu'on ne sait pas situer — « 1er mars »
sans annee, « mars 2025 » sans jour — l'est aussi : un J+n approximatif
ferait passer une estimation pour un intervalle.
"""

from __future__ import annotations

import datetime
import re
import unicodedata

from .detect import rules
from .engine import Categorie

#: Etiquette d'une date a rendre en J+n. Posee par `pipeline.filtrer_dates`
#: avec la politique `RELATIVES`, sur les seules dates qui ne sont pas de
#: naissance. Elle contient « date » : tous les predicats de date la voient.
ETIQUETTE = Categorie.DATE_RELATIVE.value

_MOIS = {
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "fev": 2, "mars": 3,
    "avril": 4, "avr": 4, "mai": 5, "juin": 6, "juillet": 7, "juil": 7,
    "aout": 8, "septembre": 9, "sept": 9, "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11, "decembre": 12, "dec": 12,
}
_ECRITE = re.compile(r"^(1er|\d{1,2})\s+([a-z]+)\.?\s+((?:19|20)\d{2})$")


def _sans_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _annee(brute: str, aujourd_hui: datetime.date) -> int:
    """« 16 » -> 2016, « 55 » -> 1955 : au-dela de l'annee en cours, le
    siecle precedent."""
    a = int(brute)
    if a >= 100:
        return a
    return 2000 + a if 2000 + a <= aujourd_hui.year else 1900 + a


def lire_date(texte: str, aujourd_hui: datetime.date | None = None
              ) -> datetime.date | None:
    """La date que ``texte`` ecrit entierement, ou ``None``.

    Les memes formes que les regles (`rules.MOTIF_DATE`, `MOTIF_DATE_ECRITE`,
    `MOTIF_DATE_COMPACTE`), a condition d'avoir jour, mois et annee.
    """
    aujourd_hui = aujourd_hui or datetime.date.today()
    t = texte.strip()
    try:
        if rules.MOTIF_DATE.fullmatch(t):
            d, m, a = re.split(r"[./-]", t)
            return datetime.date(_annee(a, aujourd_hui), int(m), int(d))
        if rules.MOTIF_DATE_COMPACTE.fullmatch(t):
            return datetime.date(int(t[4:]), int(t[2:4]), int(t[:2]))
        ecrite = _ECRITE.match(re.sub(r"\s+", " ", _sans_accents(t)))
        if ecrite and ecrite.group(2) in _MOIS:
            jour = 1 if ecrite.group(1) == "1er" else int(ecrite.group(1))
            return datetime.date(int(ecrite.group(3)), _MOIS[ecrite.group(2)],
                                 jour)
    except ValueError:
        return None
    return None


def libelle(ecart: int) -> str:
    return "J0" if ecart == 0 else f"J{ecart:+d}"


def echelle(spans, aujourd_hui: datetime.date | None = None) -> dict[str, str]:
    """Texte de chaque date relative -> son libelle, « J0 » pour la plus ancienne.

    ``spans`` sont tous les spans retenus du document, toutes parties ou pages
    confondues : l'origine doit etre la meme partout.
    """
    lues: dict[str, datetime.date] = {}
    for sp in spans:
        if sp.label != ETIQUETTE:
            continue
        d = lire_date(sp.text, aujourd_hui)
        if d is not None:
            lues[sp.text.strip()] = d
    if not lues:
        return {}
    origine = min(lues.values())
    return {texte: libelle((d - origine).days) for texte, d in lues.items()}


class TableDatesRelatives:
    """La table de pseudonymes d'un lot, vue depuis un document.

    Elle rend « J+4 » pour les dates de ce document, et confie tout le reste a
    la table partagee. Une table a part par document, et non un etat de la
    table partagee : plusieurs documents d'un lot se traitent en meme temps,
    chacun avec son J0.
    """

    def __init__(self, table, libelles: dict[str, str]):
        self._table = table
        self.libelles = libelles

    def __getattr__(self, nom):
        return getattr(self._table, nom)

    def __len__(self) -> int:
        return len(self._table)

    def pseudonyme(self, texte: str, etiquette: str) -> str:
        if etiquette == ETIQUETTE:
            cle = texte.strip()
            if cle in self.libelles:
                return self.libelles[cle]
            # Une date qu'on ne sait pas situer reste une date masquee.
            etiquette = Categorie.DATE.value
        return self._table.pseudonyme(texte, etiquette)

    def rendre(self, span) -> str:
        return f"[{self.pseudonyme(span.text.strip(), span.label)}]"

    def enregistrer(self, spans) -> None:
        self._table.enregistrer(
            sp for sp in spans if sp.label != ETIQUETTE)
