"""Plusieurs documents a la fois, sans partager ce qui ne se partage pas.

Un lot de plusieurs dizaines de documents passait un document apres l'autre.
Or la moitie du temps se passe dans Tesseract, un processus a part, qui laisse
la machine libre pour le document suivant : traiter plusieurs documents a la
fois divise la duree d'un lot par deux environ (campagne de mesure du
2026-10-07).

Deux composants ne se partagent pas entre fils d'execution, et passent donc
un document a la fois :

- **PyMuPDF**, qui rend les pages et reconstruit le PDF. MuPDF ne garantit rien
  en multi-fils ; un rendu corrompu serait une fuite silencieuse. Le verrou
  `pdf` est tenu pendant tout le traitement d'un PDF, et relache seulement le
  temps d'un travail qui n'y touche pas — l'OCR d'une image deja rendue,
  l'inference du modele sur du texte.
- **Le modele**, dont le decoupeur de texte n'est pas fait pour deux appels
  simultanes. Le verrou `modele` ne laisse passer qu'une inference a la fois ;
  onnxruntime occupe de toute facon tous les coeurs pendant chacune.

Ce qui reste concurrent : Tesseract d'un document pendant le rendu ou la
detection d'un autre. C'est la que le temps se passait.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import Counter
from contextlib import contextmanager

_pdf = threading.Lock()
_modele = threading.Lock()
#: Profondeur de detention du verrou PyMuPDF par le fil courant : `pdf` peut
#: s'imbriquer — `caviarder_pdf` appelle `verifier_octets`, qui le prend aussi.
_local = threading.local()


_journal = logging.getLogger(__name__)
_muet = False


def couper_affichage_mupdf() -> None:
    """Coupe, une fois, ce que MuPDF ecrit de lui-meme dans le terminal.

    Les PDF mal formes y font repeter « cannot find XObject resource » a chaque
    lecture de page. Les messages restent accumules par MuPDF : voir
    `journaliser_messages_mupdf`.
    """
    global _muet
    if _muet:
        return
    import pymupdf
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    _muet = True


def journaliser_messages_mupdf() -> dict[str, int]:
    """Vide la pile des messages MuPDF et les journalise, dedoublonnes.

    Journal : `logging`, niveau DEBUG, sans nom de fichier (le texte MuPDF ne
    contient pas de donnee patient, mais le journal peut etre persistant).
    Retourne {message: nombre de repetitions}.
    """
    import pymupdf
    texte = pymupdf.TOOLS.mupdf_warnings(reset=True) or ""
    comptes = Counter(l.strip() for l in texte.splitlines() if l.strip())
    for message, n in comptes.items():
        _journal.debug("MuPDF (x%d) : %s", n, message)
    return dict(comptes)


def _profondeur() -> int:
    return getattr(_local, "profondeur", 0)


@contextmanager
def pdf():
    """Tenu pendant un travail PyMuPDF. Reentrant pour le fil qui le tient."""
    if _profondeur() == 0:
        _pdf.acquire()
        try:
            couper_affichage_mupdf()
        except BaseException:
            _pdf.release()
            raise
    _local.profondeur = _profondeur() + 1
    try:
        yield
    finally:
        _local.profondeur -= 1
        if _local.profondeur == 0:
            _pdf.release()


def sous_pdf(fonction):
    """Execute ``fonction`` sous le verrou PyMuPDF."""
    @functools.wraps(fonction)
    def enveloppe(*args, **kwargs):
        with pdf():
            return fonction(*args, **kwargs)
    return enveloppe


@contextmanager
def hors_pdf():
    """Relache le verrou PyMuPDF, s'il est tenu, le temps d'un travail qui
    n'y touche pas ; le reprend ensuite au meme niveau d'imbrication."""
    tenu = _profondeur()
    if tenu:
        _local.profondeur = 0
        _pdf.release()
    try:
        yield
    finally:
        if tenu:
            _pdf.acquire()
            _local.profondeur = tenu


@contextmanager
def modele():
    """Une inference a la fois, verrou PyMuPDF relache pendant l'attente."""
    with hors_pdf(), _modele:
        yield


def documents_simultanes(coeurs: int | None = None,
                         memoire: int | None = None) -> int:
    """Combien de documents traiter a la fois sur cette machine.

    Mesure sur un M4 Pro (14 coeurs) : x1,6 a deux documents, x2,1 a quatre,
    puis plus rien. Le modele est charge une seule fois et partage ; chaque
    document en cours ajoute ses pages rendues a 300 dpi, une centaine de
    megaoctets pour un long document. D'ou deux documents sous 12 Go, ou le
    modele occupe deja pres de 2 Go, et un seul sur une machine a deux coeurs.
    """
    import os

    from .doctor import total_ram_bytes

    coeurs = coeurs if coeurs is not None else (os.cpu_count() or 1)
    if memoire is None:
        memoire = total_ram_bytes() or 0
    if coeurs <= 2:
        return 1
    plafond = 2 if memoire and memoire < 12 * 1024 ** 3 else 4
    return max(1, min(plafond, coeurs // 2))
