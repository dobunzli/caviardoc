"""Une fuite n'est pas l'autre.

L'interface annoncait « 1 fuite(s) : AT 50% » dans le meme rouge que si le nom
du patient avait survecu. L'un est vraisemblablement un faux positif du
detecteur — un arret de travail a 50 % pris pour une personne —, l'autre une
re-identification. Confondre les deux a un cout dans les deux sens : on
s'alarme pour rien, et l'on finit par ne plus s'alarmer du tout.

L'information manquait a l'affichage mais existait a la detection : l'entite
portait une etiquette. Ces tests verrouillent qu'elle survit jusqu'au bout, et
que le classement se trompe du bon cote.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.caviardage import etiquettes_a_masquer  # noqa: E402
from caviardoc.documents.fuites import Fuite, depuis_chaines  # noqa: E402
from caviardoc.engine import Span  # noqa: E402


# --- Le classement ---------------------------------------------------------

@pytest.mark.parametrize("etiquette", [
    "personne", "date de naissance", "numero AVS", "IBAN", "email",
    "numero de telephone", "rue", "adresse postale", "numero de patient",
])
def test_ces_categories_identifient_a_elles_seules(etiquette):
    assert Fuite("x", etiquette).identifiante


@pytest.mark.parametrize("etiquette", ["ville", "code postal et localite",
                                       "date", "employeur"])
def test_celles_la_situent_sans_designer(etiquette):
    assert not Fuite("x", etiquette).identifiante
    assert Fuite("x", etiquette).contextuelle


@pytest.mark.parametrize("etiquette", ["", "etiquette que personne n'a prevue"])
def test_une_etiquette_inconnue_est_traitee_comme_grave(etiquette):
    """Le modele peut rendre n'importe quelle etiquette.

    Un outil de confidentialite doit surestimer le risque, jamais l'inverse :
    annoncer « simple contexte » sur une chaine qu'on ne sait pas classer
    serait exactement la mauvaise erreur.
    """
    assert Fuite("x", etiquette).identifiante


def test_une_date_de_naissance_n_est_pas_une_date_comme_les_autres():
    assert Fuite("x", "date de naissance").identifiante
    assert not Fuite("x", "date").identifiante


# --- Ce qui remonte a l'affichage -----------------------------------------

def test_la_fuite_porte_l_etiquette_et_les_pages():
    fuites = depuis_chaines(["Kunzli"], {"Kunzli": "personne"},
                            {"Kunzli": [2, 5]})
    assert fuites[0].texte == "Kunzli"
    assert fuites[0].etiquette == "personne"
    assert fuites[0].pages == (2, 5)


def test_la_vue_donne_tout_ce_qu_il_faut_pour_decider():
    vue = Fuite("Kunzli", "personne", (3,)).vue()
    assert vue == {"texte": "Kunzli", "etiquette": "personne",
                   "pages": [3], "identifiante": True, "raison": ""}


def test_une_chaine_sans_etiquette_connue_reste_affichable():
    """La chaine seule vaut mieux que rien : on n'en perd aucune."""
    fuites = depuis_chaines(["quelque chose"])
    assert fuites[0].texte == "quelque chose"
    assert fuites[0].pages == ()
    assert fuites[0].identifiante


# --- L'arbitrage entre etiquettes -----------------------------------------

@dataclass
class FausseP:
    entites: list


def _span(texte, label):
    return Span(0, len(texte), label, 0.9, texte)


def test_la_plus_grave_etiquette_l_emporte():
    """« Saint-Prex » est une commune ici et un patronyme la.

    Si l'occurrence anodine ecrasait l'autre, une fuite identifiante
    s'afficherait comme un simple element de contexte.
    """
    pages = [FausseP([_span("Saint-Prex", "ville")]),
             FausseP([_span("Saint-Prex", "personne")])]
    assert etiquettes_a_masquer(pages)["Saint-Prex"] == "personne"

    # Et dans l'autre ordre : le resultat ne depend pas de la rencontre.
    pages.reverse()
    assert etiquettes_a_masquer(pages)["Saint-Prex"] == "personne"


def test_ce_qui_ne_ressemble_pas_a_une_donnee_n_est_pas_verifie():
    """Verifier « patient » ou « medecin » signalerait une fuite a chaque page.

    Le filtre est celui de la propagation : il faut un chiffre, une espace ou
    une majuscule interne. Un mot courant en minuscules reste masque la ou il
    a ete vu, mais n'entre pas dans la verification.
    """
    pages = [FausseP([_span("patient", "personne"),
                      _span("Elisabeth Kunzli", "personne")])]
    retenues = etiquettes_a_masquer(pages)
    assert "Elisabeth Kunzli" in retenues
    assert "patient" not in retenues
