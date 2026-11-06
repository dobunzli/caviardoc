"""Le cumul : un meme pseudonyme d'un document a l'autre a un revers.

Il aide une IA a suivre le patient, et lui permet du meme coup d'additionner
ce que chaque document dit de lui. La page le dit la ou le format est choisi,
et « Nouvelle demande » remet les pseudonymes a zero.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import interface  # noqa: E402


def test_la_page_le_dit_sous_le_choix_du_format():
    page = interface.page_console("jeton", "gliner-multi-pii-fp16", "app")
    assert 'id="aide-format"' in page
    assert "additionne aussi ce que chacun dit de lui" in page
    assert "faites une nouvelle demande" in page
    assert "AIDES.format[valeur('format')]" in page


def test_une_nouvelle_demande_remet_les_pseudonymes_a_zero(tmp_path):
    atelier = interface.Atelier(racine=tmp_path)
    avant = atelier.table()
    assert avant.pseudonyme("Zorglub", "personne") == "PERSONNE_1"
    assert atelier.table().pseudonyme("Zorglub", "personne") == "PERSONNE_1"
    interface.nouvelle_demande(atelier)
    apres = atelier.table()
    assert apres is not avant
    assert apres.pseudonyme("Mercier", "personne") == "PERSONNE_1"
