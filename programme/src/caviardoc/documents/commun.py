"""Traitements communs a toutes les chaines documentaires.

La chaine image (PDF scanne) et la chaine bureautique (.docx) n'ont presque
rien en commun dans leur mecanique : l'une noircit des pixels, l'autre reecrit
des noeuds de texte. Elles partagent en revanche tout le raisonnement qui
precede ce geste — decider quelles chaines meritent d'etre cherchees partout,
les y chercher, et assembler la sortie texte pseudonymisee.

Ces fonctions vivaient dans ``caviardage.py``, ou elles etaient nees. Les
laisser dans le module de la chaine image aurait oblige la chaine .docx a
importer un module dont le nom annonce le contraire de ce qu'elle fait.
"""

from __future__ import annotations

import re

from ..engine import Origine, Span, apply_spans, est_personne
from ..pipeline import (PROPAGATION_MIN, compacter, composants,
                        entre_frontieres, fin_de_correspondance,
                        occurrences_approchees, occurrences_du_mot, replier)
from ..pseudonymes import TablePseudonymes


def assembler_texte(pages: list[tuple[str, list[Span]]],
                    table: TablePseudonymes,
                    intitules: list[str] | None = None) -> str:
    """Assemble le texte anonymise, morceau par morceau.

    Les entites sont remplacees par des pseudonymes stables plutot que par des
    etiquettes generiques : un document ou toutes les personnes s'appellent
    `[PERSONNE]` est inexploitable par un modele de langage, qui ne peut plus
    distinguer le patient du medecin ni suivre un raisonnement.

    ``intitules`` nomme les morceaux quand « page » ne veut rien dire : un
    .docx n'a pas de pages, mais un corps, des en-tetes et des notes.
    """
    morceaux: list[str] = []
    for numero, (texte, spans) in enumerate(pages, start=1):
        if len(pages) > 1:
            titre = intitules[numero - 1] if intitules else f"page {numero}"
            morceaux.append(f"--- {titre} ---")
        morceaux.append(apply_spans(texte, spans, render=table.rendre))
    return "\n\n".join(morceaux).strip() + "\n"


_INTITULE = re.compile(r"^--- .+ ---$", re.MULTILINE)


def sans_intitules(texte: str) -> str:
    """Le texte assemble, sans les lignes « --- page 2 --- » que l'export ajoute.

    Ces intitules ne viennent pas du document. Les compter dans la verification
    du texte exporte denonce comme fuite toute chaine detectee qui ressemble a
    l'un d'eux : « page 2 », lu en tete d'une page de suite et masque comme
    numero de dossier, restait signale pour toujours, sans que la correction
    puisse y remedier.
    """
    return _INTITULE.sub("", texte)


def propageable(chaine: str, etiquette: str | None = None) -> bool:
    """La chaine merite-t-elle d'etre cherchee partout dans le document ?

    Propager sans filtre revient a masquer tout le vocabulaire courant : sur le
    corpus reel, des detections erronees comme « autres », « soins »,
    « medecin » ou « Anormal » se propageaient dans tout le document, et la
    verification finale les signalait ensuite comme autant de fuites — noyant
    les vraies alertes.

    On ne propage donc que ce qui a la forme d'un identifiant : assez long, et
    portant un chiffre, une espace ou une majuscule interne. Un mot unique en
    minuscules reste masque la ou il a ete detecte ; il n'est simplement pas
    generalise au reste du document.

    Exception : une chaine etiquetee comme personne, ecrite avec une capitale
    initiale et en lettres seules. Un nom de famille seul (« Zorglub ») n'a
    aucune des trois marques, et le laisser hors de la propagation comme de la
    verification revenait a le masquer la ou le modele l'avait vu et a annoncer
    « verifie » partout ailleurs. Les mots courants en minuscules, eux, restent
    exclus.
    """
    if len(chaine) < PROPAGATION_MIN + 1:
        return False
    if (etiquette is not None and est_personne(etiquette)
            and chaine[0].isupper() and chaine.isalpha()):
        return True
    if any(c.isdigit() for c in chaine):
        return True
    if any(c.isspace() for c in chaine):
        return True
    return any(c.isupper() for c in chaine[1:])


def chaines_lisibles(texte: str, chaines) -> set[str]:
    """Les chaines que l'on peut encore lire dans le texte assemble.

    Meme critere que la verification du PDF et que `occurrences_relues` :
    sans blancs, sans casse, traits d'union et caracteres invisibles ignores.
    Un controle plus strict que la correction laisserait passer, en texte seul,
    ce que le PDF aurait denonce. Chaque morceau se lit a part : la fin d'une
    page et le debut de la suivante ne forment pas une occurrence.
    """
    morceaux = [replier(m) for m in _INTITULE.split(texte)]
    return {c for c in chaines
            if (a := replier(c)) and any(a in m for m in morceaux)}


def propager_chaines(texte: str, spans: list[Span],
                     connues: dict[str, str]) -> list[Span]:
    """Ajoute un span pour chaque occurrence, dans ``texte``, des chaines connues.

    La recherche ignore les blancs et la casse. Ce n'est pas un confort : les
    pieds de page composes en lettres espacees — « C a b i n e t d ' i m a g
    e r i e » — sont frequents dans les en-tetes professionnels. Une recherche
    litterale n'y trouve rien, alors que la verification finale, elle, compare
    en ignorant les blancs et signalerait une fuite que la propagation etait
    structurellement incapable d'atteindre.

    Une occurrence prise au milieu d'un mot est ecartee (`entre_frontieres`) ;
    si la verification la relit malgre tout, la correction automatique s'en
    charge.

    L'etiquette d'origine est conservee : une occurrence propagee de
    « ZORGLUB Jean-Bidule » reste une personne, ce qui permet a la table de
    pseudonymes d'en deriver les composants du nom — indispensable pour
    anonymiser un fichier appele « Jean-Bidule_Zorglub_… ».
    """
    compact, positions = compacter(texte)
    ajouts: list[Span] = []

    for chaine, etiquette in connues.items():
        aiguille = replier(chaine)
        if not aiguille:
            continue
        depart = 0
        while True:
            i = compact.find(aiguille, depart)
            if i == -1:
                break
            debut = positions[i]
            fin = fin_de_correspondance(texte, positions[i + len(aiguille) - 1])
            if entre_frontieres(texte, debut, fin):
                ajouts.append(Span(debut, fin, etiquette, 1.0,
                                   texte[debut:fin], Origine.PROPAGE.value))
            depart = i + 1

    # Le prenom seul, a la page suivante, comme le patronyme seul : voir
    # `pipeline.propager_composants`.
    for chaine, etiquette in connues.items():
        if not est_personne(etiquette):
            continue
        for mot in composants(chaine):
            # Mal lu par l'OCR, le nom se retrouve encore : voir
            # `pipeline.occurrences_approchees`.
            places = set(occurrences_du_mot(texte, mot))
            places.update(occurrences_approchees(texte, mot))
            ajouts.extend(Span(d, f, etiquette, 1.0, texte[d:f],
                               Origine.PROPAGE.value)
                          for d, f in sorted(places))
    return spans + ajouts


def chaines_connues(spans) -> dict[str, str]:
    """Chaines propageables d'un lot de spans, associees a leur etiquette.

    Les plus longues sont enregistrees en premier : si « ZORGLUB Jean-Bidule »
    et « Zorglub » sont toutes deux detectees, c'est l'etiquette de la forme
    complete qui doit primer.
    """
    connues: dict[str, str] = {}
    for sp in sorted(spans, key=lambda s: -len(s.text.strip())):
        chaine = sp.text.strip()
        if propageable(chaine, sp.label):
            connues.setdefault(chaine, sp.label)
    return connues


def occurrences_relues(texte: str, chaines: dict[str, str]) -> list[Span]:
    """Spans couvrant chaque occurrence des chaines, au critere de la verification.

    La verification cherche sans blancs, sans casse, et sans frontiere de mot.
    La correction doit chercher exactement de la meme facon : si elle etait
    plus stricte, elle laisserait en place ce que la verification denonce, et
    l'alerte reviendrait a chaque passe.
    """
    compact, positions = compacter(texte)
    spans: list[Span] = []
    for chaine, etiquette in chaines.items():
        aiguille = replier(chaine)
        if not aiguille:
            continue
        i = compact.find(aiguille)
        while i != -1:
            debut = positions[i]
            fin = fin_de_correspondance(texte, positions[i + len(aiguille) - 1])
            spans.append(Span(debut, fin, etiquette, 1.0, texte[debut:fin],
                              Origine.PROPAGE.value))
            i = compact.find(aiguille, i + 1)
    return spans
