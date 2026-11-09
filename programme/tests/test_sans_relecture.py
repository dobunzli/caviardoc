"""« Sans relecture » : le reglage porte son vrai nom, et rien ne bloque la sortie.

Le reglage s'appelait « Je fais confiance ». Il s'appelle « Sans relecture ».
Le document se telecharge directement : il faut bien l'avoir pour le lire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from caviardoc import interface  # noqa: E402


class TestLaPage:
    @pytest.fixture
    def page(self):
        return interface.page_console("jeton", "gliner-multi-pii-fp16", "app")

    def test_le_reglage_s_appelle_sans_relecture(self, page):
        assert "Sans relecture" in page
        assert "fais confiance" not in page.lower()

    def test_la_sortie_ne_demande_aucune_confirmation(self, page):
        assert "confirmationDeRelecture" not in page
        assert "J'ai relu ce document" not in page
        assert "confirmer-relu" not in page
        assert "aConfirmer" not in page
