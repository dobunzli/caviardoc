"""Ce qui a survecu au caviardage, et ce que ca vaut.

Une fuite etait jusqu'ici une chaine de caracteres, sans plus. L'interface
affichait donc « 1 fuite(s) : AT 50% » en rouge, exactement comme elle aurait
affiche le nom du patient — alors que l'un est vraisemblablement un faux
positif du detecteur (un arret de travail a 50 %) et l'autre une
re-identification.

Or l'information manquante existait : l'entite detectee portait une etiquette,
perdue entre la detection et la verification. On la garde, avec la page ou la
chaine a ete relue. L'utilisateur voit alors *pourquoi* la chaine etait masquee
et *ou* elle subsiste — les deux choses qui lui permettent de decider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..engine import est_contextuel, est_identifiant_direct


@dataclass(frozen=True, order=True)
class Fuite:
    """Une chaine qui devait etre masquee et qu'on relit dans le document."""

    texte: str
    #: Etiquette de l'entite d'ou venait la chaine. Vide si on l'a perdue :
    #: dans ce cas on la traite comme identifiante, par prudence.
    etiquette: str = ""
    #: Pages ou la chaine a ete relue, numerotees a partir de 1. Vide pour un
    #: .docx, qui n'a pas de pagination avant d'etre rendu.
    pages: tuple[int, ...] = field(default=())
    #: Pourquoi la chaine est encore la, en une phrase : « laissee visible par
    #: vous », « noircie mais relue »... Sans elle, on sait qu'une donnee
    #: reste mais pas si c'est un choix, un oubli ou une limite de la lecture.
    raison: str = ""

    @property
    def identifiante(self) -> bool:
        """Cette fuite suffit-elle a reconnaitre la personne ?"""
        return est_identifiant_direct(self.etiquette)

    @property
    def contextuelle(self) -> bool:
        return est_contextuel(self.etiquette)

    def vue(self) -> dict:
        return {"texte": self.texte, "etiquette": self.etiquette,
                "pages": list(self.pages), "identifiante": self.identifiante,
                "raison": self.raison}


def depuis_chaines(chaines, etiquettes: dict | None = None,
                   pages: dict | None = None,
                   raisons: dict | None = None) -> list[Fuite]:
    """Assemble les fuites a partir des chaines relues et de ce qu'on sait."""
    etiquettes = etiquettes or {}
    pages = pages or {}
    raisons = raisons or {}
    return sorted(Fuite(chaine, etiquettes.get(chaine, ""),
                        tuple(pages.get(chaine, ())), raisons.get(chaine, ""))
                  for chaine in chaines)


def identifiantes(fuites) -> list[Fuite]:
    return [f for f in fuites if f.identifiante]
