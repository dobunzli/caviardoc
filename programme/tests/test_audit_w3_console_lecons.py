"""Console : ce que la relecture apprend, et les noms de fichiers du lot."""

from __future__ import annotations

import sys
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import interface  # noqa: E402
from caviardoc.engine import Span  # noqa: E402
from caviardoc.exceptions import Repertoire  # noqa: E402
from caviardoc.interface import Atelier  # noqa: E402
from caviardoc.revue import ajouter, decider  # noqa: E402

TEXTE = "Madame Fontannaz Yannick, consulte, puis le Dr Mercier Alain."


@pytest.fixture(autouse=True)
def _donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))


def _relire(tmp_path, spans, agir):
    atelier = Atelier(racine=tmp_path / "atelier")
    tache = interface.Tache("t", "d", "doc.docx")
    relire = interface.crochet_de_relecture(atelier, tache)
    fil = threading.Thread(
        target=lambda: relire([("corps", TEXTE, spans)]), daemon=True)
    fil.start()
    for _ in range(100):
        if tache.etat == "revue":
            break
        threading.Event().wait(0.05)
    assert tache.etat == "revue"
    agir(tache.session)
    tache.reprendre.set()
    fil.join(timeout=5)
    return atelier


def _jamais():
    return [e.chaine for e in Repertoire.charger().jamais]


def test_un_nom_de_personne_laisse_lisible_n_est_pas_appris_en_clair(tmp_path):
    spans = [Span(7, 24, "personne", 0.4, "Fontannaz Yannick", "modele")]

    def agir(session):
        decider(session, {session.detections[0].identifiant:
                          {"etat": "rejetee"}})

    _relire(tmp_path, spans, agir)
    assert _jamais() == []
    assert "Fontannaz" not in Repertoire.chemin().read_text(encoding="utf-8")


def test_une_adresse_rejetee_reste_apprise(tmp_path):
    spans = [Span(7, 24, "adresse", 0.4, "Fontannaz Yannick", "modele")]

    def agir(session):
        decider(session, {session.detections[0].identifiant:
                          {"etat": "rejetee"}})

    _relire(tmp_path, spans, agir)
    assert _jamais() == ["Fontannaz Yannick"]


def test_annuler_un_masque_manuel_ne_l_apprend_pas_comme_jamais(tmp_path):
    def agir(session):
        d = ajouter(session, "corps", TEXTE, 7, 24)
        decider(session, {d.identifiant: {"etat": "rejetee"}})

    _relire(tmp_path, [], agir)
    assert _jamais() == []


def test_un_ajout_colle_a_sa_ponctuation_est_appris_sans_elle(tmp_path):
    def agir(session):
        ajouter(session, "corps", TEXTE, 7, 25)   # « Fontannaz Yannick, »

    atelier = _relire(tmp_path, [], agir)
    r = Repertoire.charger()
    assert r.empreinte("Fontannaz Yannick") in [e.empreinte for e in r.toujours]
    assert atelier.clairs[r.empreinte("Fontannaz Yannick")] == "Fontannaz Yannick"


def test_le_nom_du_zip_suit_les_correspondances_de_fin_de_lot(tmp_path):
    atelier = Atelier(racine=tmp_path / "atelier")
    atelier.sorties().mkdir(parents=True, exist_ok=True)
    source = atelier.sorties() / "x.pdf"
    source.write_bytes(b"%PDF-1.4")
    # Le premier document ne revelait rien ; un suivant revele le patient.
    produit = interface.Production("p1", "Brechbaum_Ordonnance.pdf", source, "")
    atelier.productions["p1"] = produit
    atelier.table().enregistrer(
        [Span(0, 10, "personne", 0.9, "Brechbaum Jean", "modele")])

    archive = interface.archiver(atelier, ["p1"], a_plat=True)
    with zipfile.ZipFile(archive.chemin) as z:
        assert not any("Brechbaum" in n for n in z.namelist())
    assert "Brechbaum" not in interface.nom_final(atelier, produit.nom)


def test_nom_court_inverse_dans_un_nom_de_fichier():
    from caviardoc.noms import anonymiser_nom
    corr = {"Li Wu": "PERSONNE_1"}
    out = anonymiser_nom("Wu_Li_ordonnance.pdf", corr, personnes={"Li Wu"})
    assert out == "PERSONNE_1_ordonnance.pdf"


def test_nom_court_hors_document_reste_intact():
    from caviardoc.noms import anonymiser_nom
    # « Li Wu » n'est pas une personne du document : seuil habituel.
    assert anonymiser_nom("Wu_Li_ordonnance.pdf",
                          {"Li Wu": "PERSONNE_1"}) == "Wu_Li_ordonnance.pdf"
    # Un mot court qui n'est pas dans la table n'est jamais touche.
    assert anonymiser_nom("Wu_Li_ordonnance.pdf", {"Ng": "PERSONNE_2"},
                          personnes={"Ng"}) == "Wu_Li_ordonnance.pdf"


def test_nom_court_dans_un_mot_plus_long_reste_intact():
    from caviardoc.noms import anonymiser_nom
    out = anonymiser_nom("Wuhan_Wu-rapport.pdf", {"Li Wu": "PERSONNE_1"},
                         personnes={"Li Wu"})
    assert out == "Wuhan_PERSONNE_1-rapport.pdf"


def test_table_signale_les_personnes_et_nom_final_les_remplace():
    from caviardoc.noms import anonymiser_nom
    from caviardoc.pseudonymes import TablePseudonymes
    t = TablePseudonymes()
    t.pseudonyme("Li Wu", "personne")
    t.pseudonyme("Rue", "adresse")
    out = anonymiser_nom("Rue_Wu_Li.pdf", t.correspondances,
                         personnes=t.formes_personnes)
    assert out.startswith("Rue_PERSONNE_") and "Wu" not in out and "Li" not in out


def test_niveau_patient_garde_masquee_une_personne_non_classee():
    from caviardoc.roles import Niveau, filtrer_par_role
    texte = "Patient : Brechbaum Jean.\n\n" + "x" * 800 + "\nSa soeur Mercier Alice."
    debut = texte.index("Mercier")
    sp = Span(debut, debut + len("Mercier Alice"), "personne", 0.9,
              "Mercier Alice", "modele")
    assert sp in filtrer_par_role(texte, [sp], Niveau.PATIENT, {"Brechbaum Jean"})


def test_nom_court_colle_a_des_chiffres_est_remplace():
    from caviardoc.noms import anonymiser_nom
    corr, pers = {"Zq": "PERSONNE_1"}, {"Zq"}
    assert anonymiser_nom("Zq2024.pdf", corr, personnes=pers) == "PERSONNE_1_2024.pdf"
    assert anonymiser_nom("12Zq_rapport.pdf", corr,
                          personnes=pers) == "12_PERSONNE_1_rapport.pdf"


def test_chiffres_ne_coupent_pas_un_mot_plus_long():
    from caviardoc.noms import anonymiser_nom
    assert anonymiser_nom("Zqhan_rapport.pdf", {"Zq": "PERSONNE_1"},
                          personnes={"Zq"}) == "Zqhan_rapport.pdf"


def test_non_personne_collee_a_des_chiffres_garde_l_ancienne_regle():
    from caviardoc.noms import anonymiser_nom
    corr = {"Zorglubine": "ADRESSE_1"}
    assert anonymiser_nom("Zorglubine2024.pdf", corr) == "Zorglubine2024.pdf"
    assert anonymiser_nom("Zorglubine2024.pdf", corr,
                          personnes={"Zorglubine"}) == "ADRESSE_1_2024.pdf"
