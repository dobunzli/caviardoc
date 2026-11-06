"""« Sans relecture » : la sortie demande de confirmer que le document a ete lu.

Le reglage s'appelait « Je fais confiance », juste sous un avertissement
disant que masquer les noms ne rend pas un document anonyme. Il s'appelle
« Sans relecture », et un document que personne n'a vu ne se copie ni ne se
telecharge sans qu'on ait coche « J'ai relu ce document ».
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from caviardoc import interface  # noqa: E402
from caviardoc.engine import Span  # noqa: E402

TEXTE = "Le patient VS est suivi."


@pytest.fixture
def atelier(tmp_path, monkeypatch):
    a = interface.Atelier(racine=tmp_path)
    monkeypatch.setattr(a, "modele", lambda: None)
    return a


def _parties(origine):
    return [("corps", TEXTE, [Span(11, 13, "personne", 0.3, "VS", origine)])]


def _attendre_revue(tache):
    for _ in range(100):
        if tache.etat == "revue":
            return True
        threading.Event().wait(0.05)
    return False


class TestLeBilanLeDit:
    def test_sans_relecture_le_document_n_est_pas_relu(self, atelier):
        from test_docx import AVS, _docx, _p, _t
        octets = _docx(_p(_t("Assure "), _t(AVS)))
        depot = interface.deposer(atelier, "cr.docx", io.BytesIO(octets),
                                  len(octets))
        bilan = interface.traiter(atelier, depot.identifiant,
                                  {"format": "texte", "revue": "aucune"})
        assert bilan["relu"] is False
        assert bilan["produits"], "le document est produit, pas retenu"

    def test_montre_et_valide_il_l_est(self, atelier):
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache)
        fil = threading.Thread(target=lambda: relire(_parties("modele")),
                               daemon=True)
        fil.start()
        assert _attendre_revue(tache)
        assert not tache.relu, "pas avant la validation"
        tache.reprendre.set()
        fil.join(timeout=5)
        assert tache.relu

    def test_valide_sans_etre_montre_il_ne_l_est_pas(self, atelier):
        """Relecture des doutes, document sans doute : personne ne l'a vu."""
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache,
                                                seulement_doutes=True)
        relire(_parties("regle"))
        assert not tache.relu

    def test_interrompu_il_ne_l_est_pas(self, atelier):
        from caviardoc.interruption import Interrompu
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache)
        erreurs = []

        def lancer():
            try:
                relire(_parties("modele"))
            except Interrompu as exc:
                erreurs.append(exc)

        fil = threading.Thread(target=lancer, daemon=True)
        fil.start()
        assert _attendre_revue(tache)
        tache.interrompre()
        fil.join(timeout=5)
        assert erreurs and not tache.relu


class TestLaPage:
    @pytest.fixture
    def page(self):
        return interface.page_console("jeton", "gliner-multi-pii-fp16", "app")

    def test_le_reglage_s_appelle_sans_relecture(self, page):
        assert "Sans relecture" in page
        assert "fais confiance" not in page.lower()

    def test_la_sortie_demande_de_confirmer(self, page):
        assert "function confirmationDeRelecture" in page
        assert "J'ai relu ce document" in page
        assert "relu.accorde()" in page, "copier et telecharger la consultent"
        assert ".confirmer-relu" in page

    def test_l_archive_aussi(self, page):
        assert "pas été relu" in page and "aConfirmer" in page
