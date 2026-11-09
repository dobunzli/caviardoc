"""Couche 1 : les sommes de controle doivent accepter le vrai et rejeter le faux."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.detect import rules  # noqa: E402


class TestChecksums:
    def test_avs_valide(self):
        assert rules.valid_avs("756.1234.5678.97")
        assert rules.valid_avs("7561234567897")

    def test_avs_cle_fausse(self):
        assert not rules.valid_avs("756.1234.5678.98")

    def test_avs_mauvais_prefixe(self):
        assert not rules.valid_avs("757.1234.5678.97")

    def test_iban_ch(self):
        assert rules.valid_iban("CH93 0076 2011 6238 5295 7")
        assert not rules.valid_iban("CH93 0076 2011 6238 5295 8")

    def test_luhn(self):
        assert rules.valid_luhn("4539148803436467")
        assert not rules.valid_luhn("4539148803436468")

    def test_telephone_ch(self):
        assert rules.valid_ch_phone("032 555 12 34")
        assert rules.valid_ch_phone("+41 32 555 12 34")
        assert not rules.valid_ch_phone("032 555 12")


class TestDetection:
    TEXTE = (
        "AVS 756.1234.5678.97, IBAN CH93 0076 2011 6238 5295 7, "
        "tel 032 555 12 34, jm.dubois@example.com, 1040 Echallens, "
        "facture 4512345678901, carte 4539148803436467."
    )

    def test_trouve_les_identifiants(self):
        labels = {m.label for m in rules.find_all(self.TEXTE)}
        assert {"numero AVS", "IBAN", "numero de telephone", "email",
                "numero de carte"} <= labels

    def test_rejette_le_faux_numero(self):
        """Un numero a 13 chiffres sans cle valide ne doit pas etre retenu."""
        textes = [m.text for m in rules.find_all(self.TEXTE)]
        assert "4512345678901" not in textes

    def test_offsets_exacts(self):
        """Les offsets doivent decouper exactement la sous-chaine annoncee."""
        for m in rules.find_all(self.TEXTE):
            assert self.TEXTE[m.start:m.end] == m.text

    def test_pas_de_chevauchement(self):
        found = rules.find_all(self.TEXTE)
        for a, b in zip(found, found[1:]):
            assert a.end <= b.start


class TestDateEcrite:
    """« Examen du 5 avril 2014 » restait lisible sous « toutes les dates » :
    seules les dates en chiffres etaient reconnues."""

    def dates(self, texte):
        return [m.text for m in rules.find_all(texte) if m.label == "date"]

    def test_jour_mois_annee(self):
        assert self.dates("Examen du 5 avril 2014.") == ["5 avril 2014"]
        assert self.dates("Vu le 3 Décembre 2023") == ["3 Décembre 2023"]

    def test_abreviation_et_premier(self):
        assert self.dates("le 12 janv. 2024") == ["12 janv. 2024"]
        assert self.dates("depuis le 1er mars, puis") == ["1er mars"]

    def test_mois_annee(self):
        assert self.dates("IT à 100 % depuis mars 2025") == ["mars 2025"]

    def test_un_mois_seul_n_est_pas_une_date(self):
        assert self.dates("Revu en mars.") == []
        assert self.dates("Depuis 3 ans") == []

    def test_un_jour_impossible_n_est_pas_retenu(self):
        assert self.dates("45 mars") == []


class TestDateCompacte:
    """Un pied de page scanne : l'OCR lit « 13122021 » la ou il y a
    « 13.12.2021 », et la date restait lisible a cette page seulement."""

    def dates(self, texte):
        return [m.text for m in rules.find_all(texte) if m.label == "date"]

    def test_date_sans_points(self):
        assert self.dates("Généré le 13122021 à 10:49") == ["13122021"]

    def test_date_impossible_ignoree(self):
        assert self.dates("dossier 40448051") == []
        assert self.dates("ref 31022021") == []

    def test_nombre_plus_long_ignore(self):
        assert self.dates("ref 1312202155") == []


class TestSiteWeb:
    """Le site de l'hopital le designe comme son nom : « www.hlac.ch » etait
    masque aux pages ou le modele le voyait, lisible aux autres."""

    def sites(self, texte):
        return [m.text for m in rules.find_all(texte) if m.label == "employeur"]

    def test_adresses(self):
        assert self.sites("www.hlac.ch") == ["www.hlac.ch"]
        assert self.sites("voir https://www.h-lac.ch/medecine ici") == [
            "https://www.h-lac.ch/medecine"]
        assert self.sites("www.hlac.fr") == ["www.hlac.fr"]

    def test_lectures_de_l_ocr(self):
        assert self.sites("_Www.hlac.ch") == ["Www.hlac.ch"]
        assert self.sites("hlac.ch") == ["hlac.ch"]

    def test_le_domaine_d_un_email_n_est_pas_un_site(self):
        assert self.sites("Lucia.Ferrandi@h-lac.ch") == []

    def test_texte_ordinaire(self):
        assert self.sites("méd.-chef de service") == []
        assert self.sites("10 mg.de paracétamol") == []
        assert self.sites("membre sup gauche.Globe urinaire") == []

    def test_point_lu_comme_une_espace_derriere_www(self):
        assert self.sites("www.hlac ch") == ["www.hlac ch"]
        assert self.sites("Voir la hlac ch") == []


def test_noms_d_assureurs_masques_par_regle():
    from caviardoc.detect.rules import find_all
    for nom in ("Assura", "assura", "CSS", "Helsana", "Swica", "Sanitas", "CONCORDIA",
                "Visana", "Groupe Mutuel", "KPT", "Atupri", "ÖKK", "OKK", "Sympany",
                "EGK", "Agrisano", "SUVA", "Sodalis", "Vivao", "Philos", "Avenir Assurances",
                "Mutuel Assurance", "Easy Sana", "Supra Krankenkasse", "Assura-Basis SA",
                "CSS Assurance", "Helsana Assurances SA"):
        texte = f"Assure chez {nom}, merci."
        m = [x for x in find_all(texte) if x.label == "assureur"]
        assert m and texte[m[0].start:m[0].end] == nom, nom


def test_mots_courants_et_adresses_non_pris_pour_assureurs():
    from caviardoc.detect.rules import find_all
    assert not [x for x in find_all("Il pense a l'avenir et au supra.") if x.label == "assureur"]
    for t in ("ecrire a info@assura.ch", "voir www.assura.ch"):
        assert len([x for x in find_all(t)]) == 1


# --- Assureurs suisses (listes OFSP LAMal 1.10.2026 et LAA 2025) -------------

@pytest.mark.parametrize("texte", [
    "CSS", "CSS Kranken-Versicherung AG", "Assura-Basis SA", "Helsana",
    "Helsana Versicherungen AG", "Visana", "SWICA", "Sanitas", "KPT", "EGK",
    "ÖKK", "Sympany", "Vivao Sympany", "Atupri Gesundheitsversicherung AG",
    "Agrisano", "Groupe Mutuel", "Mutuel Assurance", "Philos", "Easy Sana",
    "Aquilana", "Suva", "Rhenusana", "SLKK", "Sodalis", "sana24",
    "Zurich Assurances", "Zürich Versicherungs-Gesellschaft AG",
    "Avenir Krankenversicherung AG", "Assurance Avenir", "CONCORDIA",
    "Concordia Versicherung", "Progrès Assurances", "Emmental Versicherung",
    "Krankenkasse Birchmeier", "Genossenschaft Krankenkasse Steffisburg",
    "Caisse-maladie Supra", "Einsiedler Krankenkasse", "Smile Assurances",
    "Solida Versicherungen AG", "PAX Versicherung",
])
def test_est_assureur_reconnu(texte):
    assert rules.est_assureur(texte), texte


@pytest.mark.parametrize("texte", [
    "Zurich", "Zürich", "Avenir", "Concordia", "Progrès", "Emmental", "Supra",
    "Simplon", "Glarner", "Wädenswil", "Visperterminen", "Steffisburg",
    "Solida", "Smile", "PAX", "Birchmeier", "Cervino", "Lumneziana", "Galenos",
    "css", "Dr Martin",
])
def test_est_assureur_ambigu_seul_refuse(texte):
    assert not rules.est_assureur(texte), texte
