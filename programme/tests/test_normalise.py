"""Normalisation de casse : la longueur doit etre preservee, et les titres epargnes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.normalise import normalise_casse  # noqa: E402


class TestLongueurPreservee:
    """Invariant central : les offsets de la copie valent pour l'original."""

    CAS = [
        "ZORGLUB Jean-Bidule 23.04.1931",
        "TARTEMPION MACHIN\nMédecine interne FMH",
        "ECHO-DOPPLER VEINEUX DU MEMBRE INFERIEUR GAUCHE",
        "Rue du Marché 20 - CP 47",
        "ÉLÉONORE MÜLLER-ZBINDEN",
        "",
        "aucune capitale ici",
    ]

    def test_longueur_identique(self):
        for t in self.CAS:
            assert len(normalise_casse(t)) == len(t), repr(t)

    def test_sauts_de_ligne_intacts(self):
        t = "MERCIER PHILIPPE\nRue des Noisetiers 42\n1510 Moudon"
        out = normalise_casse(t)
        assert out.count("\n") == t.count("\n")
        assert [len(a) for a in out.split("\n")] == [len(a) for a in t.split("\n")]


class TestSelectivite:
    def test_patronyme_en_capitales_normalise(self):
        assert normalise_casse("MERCIER PHILIPPE") == "Mercier Philippe"

    def test_nom_accole_a_un_prenom_normalise(self):
        assert normalise_casse("FONTANNAZ Yannick") == "Fontannaz Yannick"

    def test_titre_de_section_epargne(self):
        """Une longue suite de capitales est un titre, pas un nom."""
        t = "ECHO-DOPPLER VEINEUX DU MEMBRE INFERIEUR GAUCHE"
        assert normalise_casse(t) == t

    def test_medicament_epargne(self):
        """Un mot en capitales suivi de minuscules n'est pas un nom."""
        t = "ATACAND cpr 16 mg 98 pce"
        assert normalise_casse(t) == t

    def test_sigle_epargne(self):
        assert normalise_casse("Médecine interne FMH") == "Médecine interne FMH"

    def test_pas_de_fusion_entre_lignes(self):
        """Deux mots sur des lignes differentes ne forment pas un nom."""
        t = "TOTAL\nRESULTATS"
        assert normalise_casse(t) == t
