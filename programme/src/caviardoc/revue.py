"""Revue humaine des detections, avant ecriture du document anonymise.

Aucun modele ne detecte tout. Sur le corpus de reference, 99 % des identifiants
du patient sont masques au niveau complet — ce qui laisse un pour cent, et un
transfert vers un service en nuage est irreversible. La relecture n'est donc
pas un confort : c'est la seule etape qui puisse rattraper ce que la mesure
annonce comme manquant.

Trois principes gouvernent ce module.

**Le defaut est de masquer.** Une detection non revue reste retenue. Une session
abandonnee en cours de route ne produit jamais un document moins masque qu'une
session jamais ouverte. Seule exception, et elle va dans le meme sens : les
quasi-identifiants (`engine.QUASI_IDENTIFIANTS`) arrivent « a juger », non
masques. Sans relecture, on ne les cherche meme pas ; ils ne decouvrent donc
rien que le document n'aurait montre sans eux.

**Le rejet est trace.** Retirer un masque est une decision, pas un oubli : elle
porte un motif et se relit. C'est ce qui distingue un caviardage assume d'un
caviardage rate.

**La session ne quitte pas la machine.** Elle contient les entites detectees en
clair — donc les noms qu'on cherche precisement a proteger. Elle est ecrite
dans le repertoire de donnees local, jamais dans le depot, et supprimee des
qu'elle est appliquee.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .engine import Span, est_quasi_identifiant
from .paths import data_dir

#: Nombre de caracteres montres de part et d'autre d'une entite. Une detection
#: hors contexte n'est pas jugeable : « Mercier » peut etre un patient, un
#: medecin ou une rue.
CONTEXTE = 90


class Etat(str, Enum):
    """Sort d'une detection apres relecture."""

    #: Detectee par l'outil et conservee — l'etat par defaut.
    RETENUE = "retenue"
    #: Ecartee par un humain, avec un motif.
    REJETEE = "rejetee"
    #: Ajoutee par un humain, que l'outil avait manquee.
    AJOUTEE = "ajoutee"
    #: Quasi-identifiant signale et laisse lisible tant qu'un humain ne l'a
    #: pas masque : une profession, un age, un evenement.
    A_JUGER = "a_juger"


@dataclass
class Detection:
    """Une entite soumise a relecture, avec ce qu'il faut pour la juger."""

    identifiant: str
    partie: str
    debut: int
    fin: int
    texte: str
    etiquette: str
    score: float
    origine: str
    avant: str
    apres: str
    etat: str = Etat.RETENUE.value
    motif: str = ""

    @property
    def masquee(self) -> bool:
        return self.etat not in (Etat.REJETEE.value, Etat.A_JUGER.value)

    @property
    def a_juger(self) -> bool:
        """Passage qui peut aider a reconnaitre la personne, sans la nommer.

        Tient a l'etiquette et non a l'etat : masque d'un clic, le passage
        reste un quasi-identifiant, montre comme tel.
        """
        return est_quasi_identifiant(self.etiquette)

    @property
    def doute(self) -> bool:
        """Detection que rien ne recoupe : une estimation du modele.

        Une regle est explicable (somme de controle, format), une propagation
        reporte une decision deja prise ailleurs, un humain a designe : seul le
        modele estime. C'est ce que la page montre en orange, et ce que la
        relecture « doutes seulement » demande de regarder. Le critere vit ici
        pour n'avoir qu'un endroit a ajuster, un seuil de score par exemple.

        Un passage a juger n'est pas un doute : il n'y a pas a se demander si
        c'est une donnee, mais si elle aide a reconnaitre la personne.
        """
        return self.origine == "modele" and not self.a_juger


#: Libelle montre pour chaque genre de zone.
LIBELLES_ZONES = {
    "entete": "zone : bloc d'en-tête",
    "logo": "zone : logo ou tampon",
    "bande": "zone : bandeau du haut",
}

#: Prefixe des cles par lesquelles une relecture rend les zones retenues d'une
#: partie, a cote des spans retenus (cle : intitule de la partie).
CLE_ZONES = "zones:"


@dataclass
class Zone:
    """Un rectangle que le caviardage noircirait d'un bloc, soumis a relecture.

    Distinct d'une detection : il ne vient pas d'un passage du texte mais d'une
    option de zone (en-tetes, logos). Le rectangle est en pixels de la page
    rendue, comme les boites de la lecture. Retenu par defaut : une zone non
    relue reste noircie, comme sans relecture.
    """

    identifiant: str
    partie: str
    genre: str
    boite: list[float]
    #: Ce que la zone recouvre en texte lu, pour la reconnaitre dans la liste.
    texte: str = ""
    etat: str = Etat.RETENUE.value
    motif: str = ""

    @property
    def etiquette(self) -> str:
        return LIBELLES_ZONES.get(self.genre, "zone")

    @property
    def masquee(self) -> bool:
        return self.etat != Etat.REJETEE.value


@dataclass
class SessionRevue:
    """Un lot de documents en attente de relecture."""

    document: str
    cree: float = field(default_factory=time.time)
    detections: list[Detection] = field(default_factory=list)
    identifiant: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    zones: list[Zone] = field(default_factory=list)

    @property
    def retenues(self) -> list[Detection]:
        return [d for d in self.detections if d.masquee]

    @property
    def rejetees(self) -> list[Detection]:
        """Masques retires par un humain — pas les passages a juger laisses
        lisibles, qui ne l'ont jamais ete."""
        return [d for d in self.detections if d.etat == Etat.REJETEE.value]

    @property
    def doutes(self) -> list[Detection]:
        return [d for d in self.detections if d.doute]

    @property
    def a_juger(self) -> list[Detection]:
        return [d for d in self.detections if d.a_juger]

    @property
    def ajoutees(self) -> list[Detection]:
        return [d for d in self.detections if d.etat == Etat.AJOUTEE.value]

    def chemin(self) -> Path:
        return dossier_sessions() / f"{self.identifiant}.json"

    def enregistrer(self) -> Path:
        c = self.chemin()
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return c

    def supprimer(self) -> None:
        self.chemin().unlink(missing_ok=True)

    @classmethod
    def charger(cls, chemin: Path | str) -> "SessionRevue":
        brut = json.loads(Path(chemin).read_text(encoding="utf-8"))
        detections = [Detection(**d) for d in brut.pop("detections", [])]
        zones = [Zone(**z) for z in brut.pop("zones", [])]
        return cls(detections=detections, zones=zones, **brut)


def dossier_sessions() -> Path:
    """Ou vivent les sessions : hors du depot, local a la machine."""
    return data_dir() / "revues"


def preparer(document: str, parties: list[tuple[str, str, list[Span]]],
             zones: dict[str, list[tuple[str, tuple, str]]] | None = None
             ) -> SessionRevue:
    """Construit une session a partir des detections d'un document.

    ``parties`` associe a chaque morceau son intitule, son texte et ses spans.
    Un span de quasi-identifiant arrive « a juger » : lisible tant qu'on ne le
    masque pas.

    ``zones`` associe a une partie ses zones : des triplets (genre, rectangle
    en pixels, texte recouvert). Elles arrivent retenues, refusables.
    """
    session = SessionRevue(document=document)
    for partie, lot in (zones or {}).items():
        for i, (genre, boite, texte) in enumerate(lot):
            session.zones.append(Zone(
                identifiant=f"{partie}:zone:{i}", partie=partie, genre=genre,
                boite=[float(v) for v in boite], texte=texte))
    for partie, texte, spans in parties:
        for i, sp in enumerate(sorted(spans, key=lambda s: s.start)):
            session.detections.append(Detection(
                identifiant=f"{partie}:{i}",
                partie=partie,
                debut=sp.start,
                fin=sp.end,
                texte=sp.text,
                etiquette=sp.label,
                score=round(sp.score, 3),
                origine=sp.origine,
                avant=texte[max(0, sp.start - CONTEXTE):sp.start],
                apres=texte[sp.end:sp.end + CONTEXTE],
                etat=(Etat.A_JUGER.value if est_quasi_identifiant(sp.label)
                      else Etat.RETENUE.value),
            ))
    return session


def appliquer(session: SessionRevue, partie: str) -> list[Span]:
    """Spans a masquer pour une partie, apres decisions.

    Les rejets sont retires, les ajouts integres. Une detection sans decision
    reste masquee : l'inaction ne doit jamais decouvrir un document. Un passage
    a juger sans decision reste lisible, comme il l'etait avant qu'on le
    cherche.
    """
    return [Span(d.debut, d.fin, d.etiquette, d.score, d.texte, d.origine)
            for d in session.detections
            if d.partie == partie and d.masquee]


def zones_retenues(session: SessionRevue, partie: str) -> list[tuple]:
    """Rectangles des zones d'une partie que la relecture laisse noircir."""
    return [tuple(z.boite) for z in session.zones
            if z.partie == partie and z.masquee]


def avec_zones(session: SessionRevue, retenus: dict) -> dict:
    """Complete les spans retenus par les zones retenues, partie par partie.

    Sans zone dans la session, rend ``retenus`` tel quel : une relecture qui
    ignore les zones se comporte comme avant.
    """
    parties = {z.partie for z in session.zones}
    return {**retenus,
            **{CLE_ZONES + p: zones_retenues(session, p) for p in parties}}


def decider(session: SessionRevue, decisions: dict[str, dict]) -> SessionRevue:
    """Applique un lot de decisions a une session.

    Une decision inconnue est ignoree plutot que d'interrompre : une session
    peut avoir ete regeneree entre l'affichage et l'envoi, et perdre les
    decisions valides a cause d'une seule obsolete serait le mauvais arbitrage.
    """
    par_id = {d.identifiant: d for d in session.detections}
    par_id.update({z.identifiant: z for z in session.zones})
    for identifiant, choix in decisions.items():
        d = par_id.get(identifiant)
        if d is None:
            continue
        etat = choix.get("etat")
        if etat in (Etat.RETENUE.value, Etat.REJETEE.value):
            if etat == Etat.REJETEE.value and getattr(d, "a_juger", False):
                # Laisser lisible un passage a juger, c'est le laisser tel
                # qu'il est arrive. En faire un rejet apprendrait a la machine
                # a ne jamais masquer « cycliste », ce que personne n'a decide.
                etat = Etat.A_JUGER.value
            d.etat = etat
            d.motif = str(choix.get("motif", ""))[:200]
    return session


def ajouter(session: SessionRevue, partie: str, texte_partie: str,
            debut: int, fin: int, etiquette: str = "ajout manuel") -> Detection:
    """Enregistre une entite que l'outil avait manquee.

    C'est la moitie utile de la relecture : rejeter un faux positif ameliore le
    confort, rattraper un faux negatif evite une fuite.
    """
    if not 0 <= debut < fin <= len(texte_partie):
        raise ValueError(f"intervalle hors du texte : {debut}:{fin}")
    d = Detection(
        identifiant=f"{partie}:ajout:{uuid.uuid4().hex[:6]}",
        partie=partie, debut=debut, fin=fin,
        texte=texte_partie[debut:fin],
        etiquette=etiquette, score=1.0, origine="humain",
        avant=texte_partie[max(0, debut - CONTEXTE):debut],
        apres=texte_partie[fin:fin + CONTEXTE],
        etat=Etat.AJOUTEE.value,
    )
    session.detections.append(d)
    return d
