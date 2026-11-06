"""Le diagnostic doit couvrir les deux couches, pas seulement la detection.

Une machine ou seul l'extra `engine` etait installe affichait un `doctor` tout
vert et un `selftest` reussi, alors que `caviardoc caviarder` s'arretait sur un
ImportError des le premier fichier : rien ne controlait pymupdf, python-docx,
Pillow ni pytesseract. Ces tests verrouillent le controle qui manquait.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caviardoc import doctor
from caviardoc.doctor import Level


@pytest.fixture
def sans_venv(tmp_path, monkeypatch):
    """Isole le diagnostic du venv reel de la machine."""
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    return tmp_path


def test_le_diagnostic_controle_la_couche_document():
    noms = [ch.name for ch in doctor.run_all()]
    assert "Dependances documents" in noms


def test_couche_document_absente_est_bloquante(sans_venv, monkeypatch):
    """FAIL et non WARN : sans elle, la commande principale ne demarre pas."""
    monkeypatch.setattr(doctor, "_can_import",
                        lambda module, python=None: module != "pymupdf")

    ch = doctor.check_documents_deps()
    assert ch.level is Level.FAIL
    assert "pymupdf" in ch.detail
    assert ch.auto_fixable


def test_les_deux_couches_sont_distinguees(sans_venv, monkeypatch):
    """Le moteur peut etre la sans la couche document, et l'inverse."""
    monkeypatch.setattr(doctor, "_can_import",
                        lambda module, python=None: module in ("onnxruntime", "gliner"))

    assert doctor.check_engine_deps().level is Level.OK
    assert doctor.check_documents_deps().level is Level.FAIL


def test_tesseract_sans_francais_nomme_la_consequence(monkeypatch):
    """Le message doit dire ce qui casse, et ce qu'on va faire pour y remedier.

    Le remede a change de nature : il ne dit plus « tapez brew install », il
    annonce qu'on installera une copie a nous. Le test suit ce changement — et
    accroche desormais `tesseract.langues`, seul point que `check_tesseract`
    consulte reellement.
    """
    monkeypatch.setattr(doctor.tesseract, "binaire",
                        lambda: Path("/usr/bin/tesseract"))
    monkeypatch.setattr(doctor.tesseract, "binaire_local", lambda: None)
    monkeypatch.setattr(doctor.tesseract, "langues", lambda: ["eng", "osd"])

    ch = doctor.check_tesseract()
    # Decision : Tesseract est obligatoire pour les PDF, le francais compris.
    assert ch.level is Level.FAIL
    assert "aucun PDF" in ch.detail
    # Et ce qu'on fera : poser notre propre copie, sans toucher a la sienne.
    assert "sans toucher" in ch.fix
    assert ch.auto_fixable, "l'installateur sait le reparer seul"


def test_tesseract_absent_ne_renvoie_plus_a_une_commande(monkeypatch):
    """Le mur d'avant : « installez-le vous-meme » etait la seule reponse."""
    monkeypatch.setattr(doctor.tesseract, "binaire", lambda: None)

    ch = doctor.check_tesseract()
    assert ch.level is Level.FAIL
    assert "aucun PDF" in ch.detail
    assert ch.auto_fixable
    assert not any(mot in ch.fix for mot in ("brew install", "apt install",
                                             "winget install"))
    assert "setup" in ch.fix or "installateur" in ch.fix


def test_tesseract_dit_d_ou_vient_le_binaire(monkeypatch):
    """Le notre ou celui du systeme : la distinction change le remede."""
    monkeypatch.setattr(doctor.tesseract, "langues", lambda: ["fra", "eng"])

    monkeypatch.setattr(doctor.tesseract, "binaire", lambda: Path("/usr/bin/tesseract"))
    monkeypatch.setattr(doctor.tesseract, "binaire_local", lambda: None)
    assert "du systeme" in doctor.check_tesseract().detail

    monkeypatch.setattr(doctor.tesseract, "binaire_local",
                        lambda: Path("/a/nous/tesseract"))
    assert "installe par nous" in doctor.check_tesseract().detail


def test_l_aide_de_deep_annonce_la_taille_du_modele_retenu():
    """L'aide annoncait « lit 350 Mo », taille d'int8, apres le passage a fp16.

    Un chiffre ecrit en dur cesse d'etre vrai des que le defaut change. Celui-ci
    vient desormais du registre.
    """
    import io
    import contextlib

    from caviardoc import cli, models

    sortie = io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.suppress(SystemExit):
        cli.main(["doctor", "--help"])
    aide = sortie.getvalue()

    attendue = models.human(models.REGISTRY[models.modele_actif()].total_size)
    assert attendue in aide, f"l'aide doit annoncer {attendue}"
    assert "350 Mo" not in aide, "taille d'int8, ecrite en dur"


def test_doctor_propose_d_inspecter_chaque_modele_du_registre():
    import io
    import contextlib

    from caviardoc import cli, models

    sortie = io.StringIO()
    with contextlib.redirect_stdout(sortie), contextlib.suppress(SystemExit):
        cli.main(["doctor", "--help"])
    aide = sortie.getvalue()

    for cle in models.REGISTRY:
        assert cle in aide, f"{cle} doit etre proposable a --modele"


class TestEssaiImport:
    """L'installateur annoncait « introuvable » un paquet installe dont le
    premier chargement depassait la minute. L'essai dit desormais pourquoi
    il echoue, et laisse le temps au chargement."""

    def test_un_module_qui_se_charge(self):
        import sys
        assert doctor.essai_import(("json", "zipfile"), Path(sys.executable)) == (True, "")

    def test_l_echec_dit_sa_cause(self):
        import sys
        reussi, cause = doctor.essai_import(("module_qui_n_existe_pas",),
                                            Path(sys.executable))
        assert not reussi
        assert "ModuleNotFoundError" in cause and "module_qui_n_existe_pas" in cause

    def test_le_delai_depasse_se_dit_comme_tel(self, monkeypatch):
        import subprocess
        import sys

        def trop_long(*a, **kw):
            raise subprocess.TimeoutExpired(a[0], kw.get("timeout"))

        monkeypatch.setattr(doctor.subprocess, "run", trop_long)
        reussi, cause = doctor.essai_import(("gliner",), Path(sys.executable),
                                            delai=900)
        assert not reussi
        assert "15 min" in cause

    def test_un_interpreteur_absent(self, tmp_path):
        reussi, cause = doctor.essai_import(("json",), tmp_path / "python")
        assert not reussi and "absent" in cause


def test_le_diagnostic_signale_chaque_ancien_repertoire(tmp_path, monkeypatch):
    donnees = tmp_path / "caviardoc"
    donnees.mkdir()
    monkeypatch.delenv("CAVIARDOC_DATA_DIR", raising=False)
    monkeypatch.setattr(doctor.paths, "data_dir", lambda: donnees)
    for nom in ("doc-anonymizer", "pii-anonymizer"):
        (tmp_path / nom).mkdir()
        (tmp_path / nom / "x.txt").write_text("synthetique")
    controles = doctor.check_migration()
    assert len(controles) == 2
    details = " ".join(ch.detail for ch in controles)
    assert "doc-anonymizer" in details and "pii-anonymizer" in details
    assert all(ch.level is Level.WARN for ch in controles)
