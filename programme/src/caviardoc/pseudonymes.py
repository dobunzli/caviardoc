"""Table de correspondance entre entites et pseudonymes.

Un document destine a un modele de langage ne doit pas etre crible de
`[PERSONNE]` indistincts : le modele perd alors le fil de qui fait quoi, et le
document devient inexploitable pour l'usage meme qui a motive son
anonymisation. Des pseudonymes numerotes et **stables** — `PERSONNE_1` designe
toujours la meme personne, dans le document comme dans le lot — preservent la
structure du raisonnement sans rien reveler.

La table est aussi ce qui permettra, plus tard, de re-identifier localement la
reponse du modele. Elle contient donc en clair les donnees qu'on protege : si
elle est un jour persistee, elle devra etre chiffree et ne jamais quitter la
machine.
"""

from __future__ import annotations

import threading
import unicodedata

from .engine import est_personne
from .noms import composants_du_nom, est_mot_etablissement, formes_canoniques, sans_titres, variantes


class TablePseudonymes:
    """Attribue des pseudonymes stables, partageables entre plusieurs documents."""

    def __init__(self) -> None:
        #: Forme canonique -> pseudonyme. Indexer sur la forme canonique est
        #: indispensable : « Zorglub », « ZORGLUB » et « zorglub »
        #: designent la meme personne et doivent recevoir le meme pseudonyme.
        self._table: dict[str, str] = {}
        #: Forme d'origine -> pseudonyme, pour l'anonymisation des noms de
        #: fichiers, qui a besoin des ecritures reelles.
        self._formes: dict[str, str] = {}
        #: Formes d'origine (cles de `_formes`) qui designent une personne.
        self._formes_personnes: set[str] = set()
        self._compteurs: dict[str, int] = {}
        #: Forme canonique d'une personne -> son role (`roles.Role`). Une
        #: personne qualifiee recoit « MEDECIN_2_RHUMATOLOGUE » plutot que
        #: « PERSONNE_5 ».
        self._roles: dict[str, tuple[str, str]] = {}
        #: Personnes : nom entier (avec et sans titre) -> pseudonyme.
        self._personnes: dict[str, str] = {}
        #: Personnes : prenom ou patronyme -> pseudonymes de ceux qui le
        #: portent, dans l'ordre ou on les a rencontres. Un patronyme est
        #: partage par une famille : il ne suffit pas a reconnaitre quelqu'un.
        self._porteurs: dict[str, list[str]] = {}
        #: Plusieurs documents d'un lot partagent la table et peuvent etre
        #: traites en meme temps : deux noms nouveaux ne doivent pas recevoir
        #: le meme numero.
        self._verrou = threading.RLock()

    @property
    def correspondances(self) -> dict[str, str]:
        with self._verrou:
            return dict(self._formes)

    @property
    def formes_personnes(self) -> frozenset[str]:
        """Cles de `correspondances` qui sont des personnes du document."""
        with self._verrou:
            return frozenset(self._formes_personnes)

    def __len__(self) -> int:
        return len(self._table)

    def qualifier(self, roles: dict[str, tuple[str, str]]) -> None:
        """Retient le role des personnes, avant qu'elles soient enregistrees.

        Le role ne s'applique qu'a un pseudonyme encore a creer : une personne
        deja nommee dans le lot garde le sien, sans quoi un meme medecin
        porterait deux noms d'un document a l'autre.
        """
        with self._verrou:
            for texte, role in roles.items():
                # Sous toutes ses formes entieres : le role est reconnu sur
                # « M. Jean Dupont », le pseudonyme cree sur « Monsieur Jean
                # Dupont ». Indexe sur la seule forme exacte, le patient
                # devenait « PERSONNE_1 ».
                for cle in _cles_entieres(texte):
                    self._roles.setdefault(cle, role)

    def pseudonyme(self, texte: str, etiquette: str) -> str:
        """Pseudonyme de cette entite, en la creant au besoin."""
        with self._verrou:
            return self._pseudonyme(texte, etiquette)

    def _pseudonyme(self, texte: str, etiquette: str) -> str:
        formes = variantes(texte, etiquette)
        if not formes:
            return self._nouveau(etiquette)

        cles = [(c, f) for f in formes for c in formes_canoniques(f)]
        personne = est_personne(etiquette)
        if personne:
            connu = self._personne_connue(texte)
        else:
            connu = next((self._table[c] for c, _ in cles if c in self._table),
                         None)
        if connu is None:
            role = (next((self._roles[c] for c in _cles_entieres(texte)
                          if c in self._roles), None) if personne else None)
            connu = (self._nouveau(*role) if role
                     else self._nouveau(etiquette))
        for c, forme in cles:
            if c:
                self._table.setdefault(c, connu)
                if not est_mot_etablissement(forme):
                    self._formes.setdefault(forme, connu)
                if personne:
                    self._formes_personnes.add(forme)
        if personne:
            for c in _cles_entieres(texte):
                self._personnes.setdefault(c, connu)
            for nom in composants_du_nom(texte):
                for c in formes_canoniques(nom):
                    porteurs = self._porteurs.setdefault(c, [])
                    if connu not in porteurs:
                        porteurs.append(connu)
        return connu

    def _personne_connue(self, texte: str) -> str | None:
        """Le pseudonyme d'une personne deja rencontree, ou None.

        Un prenom et un patronyme ne designent la meme personne que s'ils ont
        deja ete vus ensemble : « Claire Dupont » n'est pas « Jean Dupont »,
        alors que « DUPONT Jean » l'est. Un nom d'un seul mot — « M. Dupont »
        — designe celui qui le porte ; s'ils sont plusieurs, le patient d'abord,
        puis le premier rencontre. Confondre la fille et le patient faisait
        dire au texte exporte que le patient s'accompagnait lui-meme.
        """
        for cle in _cles_entieres(texte):
            if cle in self._personnes:
                return self._personnes[cle]
        noms = [f for f in (formes_canoniques(n) for n in composants_du_nom(texte))
                if f]
        if not noms:
            return None
        # Un composant est porte par quiconque porte l'une de ses formes :
        # « TARTEMPUENZ » est le « Tartempünz » deja rencontre.
        porteurs = [list(dict.fromkeys(p for c in formes
                                       for p in self._porteurs.get(c, [])))
                    for formes in noms]
        communs = [p for p in porteurs[0] if all(p in q for q in porteurs[1:])]
        if not communs:
            return None
        return next((p for p in communs if p.startswith("PATIENT")), communs[0])

    def _nouveau(self, etiquette: str, precision: str = "") -> str:
        """Pseudonyme neuf : « PERSONNE_3 », ou « MEDECIN_2_RHUMATOLOGUE ».

        Le numero compte par etiquette, la precision n'y entre pas : deux
        medecins de specialites differentes sont MEDECIN_1 et MEDECIN_2, et
        le numero suffit a les distinguer quand la specialite manque.
        """
        cle = _majuscules(etiquette)
        self._compteurs[cle] = self._compteurs.get(cle, 0) + 1
        nom = f"{cle}_{self._compteurs[cle]}"
        return f"{nom}_{_majuscules(precision)}" if precision else nom

    def enregistrer(self, spans) -> None:
        """Enregistre un lot de spans, les plus longs d'abord.

        L'ordre compte : « ZORGLUB Jean-Bidule » doit etre enregistre avant
        « Jean-Bidule » seul, sans quoi les deux moities d'un meme nom recoivent
        deux pseudonymes differents.
        """
        with self._verrou:
            for sp in sorted(spans, key=lambda s: -len(s.text.strip())):
                texte = sp.text.strip()
                if len(texte) >= 3:
                    self._pseudonyme(texte, sp.label)

    def rendre(self, span) -> str:
        """Texte de remplacement d'un span, entre crochets."""
        return f"[{self.pseudonyme(span.text.strip(), span.label)}]"


def _cles_entieres(texte: str) -> list[str]:
    """Formes canoniques du nom entier, avec et sans titre."""
    cles = (formes_canoniques(texte)
            + formes_canoniques(" ".join(sans_titres(texte))))
    return list(dict.fromkeys(cles))


def _majuscules(s: str) -> str:
    """« sage-femme » -> « SAGE_FEMME » : un pseudonyme reste un seul mot."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.upper().replace(" ", "_").replace("-", "_").replace("'", "")
