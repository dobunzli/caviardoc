"""Couche 2 : le remplacement par offsets ne doit ni fuir ni detruire.

Ces tests n'ont pas besoin du modele : ils portent sur la logique de
remplacement par offsets, ou se logent les erreurs les plus couteuses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import Span, apply_spans, resolve_overlaps  # noqa: E402


def test_remplacement_droite_a_gauche():
    """Un span court ne doit pas corrompre un span long situe apres lui.

    Un pseudonyme pose sur « 12 » avant le traitement du numero complet
    casserait la correspondance de celui-ci, qui resterait lisible en clair.
    D'ou le parcours de droite a gauche.
    """
    texte = "appeler le 12 ou le 032 555 12 34"
    spans = [
        Span(11, 13, "numero", 1.0, "12"),
        Span(20, 33, "telephone", 1.0, "032 555 12 34"),
    ]
    out = apply_spans(texte, spans)
    assert "032 555 12 34" not in out
    assert out == "appeler le [NUMERO] ou le [TELEPHONE]"


def test_ponctuation_preservee():
    """Le texte autour des entites doit rester intact, espaces compris."""
    texte = "Le patient Jean Dubois, ne a Coire, va bien."
    out = apply_spans(texte, [Span(11, 22, "personne", 1.0, "Jean Dubois")])
    assert out == "Le patient [PERSONNE], ne a Coire, va bien."


def test_chevauchement_garde_le_plus_long():
    spans = [
        Span(0, 5, "prenom", 0.9, "Jean-"),
        Span(0, 16, "personne", 0.8, "Jean-Marc Dubois"),
    ]
    kept = resolve_overlaps(spans)
    assert len(kept) == 1
    assert kept[0].text == "Jean-Marc Dubois"


def test_spans_disjoints_tous_conserves():
    spans = [Span(0, 4, "a", 1.0, "aaaa"), Span(10, 14, "b", 1.0, "bbbb")]
    assert len(resolve_overlaps(spans)) == 2


def test_aucun_span_texte_inchange():
    assert apply_spans("rien a masquer", []) == "rien a masquer"
