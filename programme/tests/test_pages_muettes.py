"""Une page lisible ou rien n'a ete detecte merite un coup d'oeil.

Elle peut n'avoir legitimement rien de personnel — un tableau de valeurs, une
page de references. Mais quand le reste du document en est plein, ce silence
peut aussi venir d'une mise en page inhabituelle, d'une lecture qui s'est mal
passee, ou d'une detection qui a manque. On signale sans bloquer : c'est un
doute a lever par un humain.

Trois conditions, et chacune a sa raison d'etre — c'est ce que verrouillent ces
tests. Sans la premiere, tout verso de scan declencherait une alerte ; sans la
troisieme, un document sans aucune donnee personnelle en declencherait autant
qu'il a de pages, et l'alerte ne voudrait plus rien dire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.caviardage import (RapportPage,  # noqa: E402
                                          signaler_pages_muettes)
from caviardoc.engine import Span  # noqa: E402

#: Assez de mots pour que la page soit jugee lisible (le seuil est de 20).
LISIBLE = ("Compte rendu de laboratoire du service de medecine interne, "
           "valeurs usuelles, hemoglobine leucocytes plaquettes creatinine "
           "sodium potassium calcium phosphate proteines albumine.")


def _page(numero: int, texte: str = LISIBLE, entites: int = 0) -> RapportPage:
    return RapportPage(
        numero=numero, texte_lu=texte,
        entites=[Span(0, 5, "personne", 0.9, "Kunzli")] * entites)


def test_une_page_muette_est_signalee():
    pages = [_page(1, entites=12), _page(2)]
    signaler_pages_muettes(pages)
    assert not pages[0].reserve
    assert "aucune donnee detectee" in pages[1].reserve


def test_une_page_blanche_ne_dit_rien():
    """Un verso de scan, un separateur : il n'y a rien a y verifier."""
    pages = [_page(1, entites=12), _page(2, texte="")]
    signaler_pages_muettes(pages)
    assert not pages[1].reserve


def test_une_page_presque_vide_ne_dit_rien_non_plus():
    """Quelques mots isoles ne suffisent pas a parler de page lisible."""
    pages = [_page(1, entites=12), _page(2, texte="page 2 sur 19")]
    signaler_pages_muettes(pages)
    assert not pages[1].reserve


def test_un_document_sans_donnees_ne_declenche_rien():
    """Sinon chaque page d'un document anodin serait signalee, et l'alerte
    perdrait tout sens."""
    pages = [_page(1), _page(2), _page(3)]
    signaler_pages_muettes(pages)
    assert not any(p.reserve for p in pages)


def test_une_page_couverte_par_la_propagation_est_tranquille():
    """Le signalement se lit apres propagation : une page dont les entites
    viennent d'ailleurs est deja traitee."""
    pages = [_page(1, entites=12), _page(2, entites=1)]
    signaler_pages_muettes(pages)
    assert not pages[1].reserve


def test_plusieurs_pages_muettes_sont_toutes_signalees():
    pages = [_page(1, entites=8), _page(2), _page(3), _page(4, entites=2)]
    signaler_pages_muettes(pages)
    assert [p.numero for p in pages if p.reserve] == [2, 3]


def test_le_signalement_ne_bloque_rien():
    """C'est un doute, pas un verdict : les entites restent intactes."""
    pages = [_page(1, entites=3), _page(2)]
    avant = len(pages[0].entites)
    signaler_pages_muettes(pages)
    assert len(pages[0].entites) == avant
