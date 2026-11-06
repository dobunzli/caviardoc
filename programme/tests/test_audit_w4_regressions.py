"""Audit vague 4 : regressions relevees a la relecture des correctifs."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from caviardoc import cli, interface, maj, paths  # noqa: E402
from caviardoc.documents.docx import lire_partie  # noqa: E402
from test_docx import DOCUMENT, _p  # noqa: E402


# --- 1. mesure : pas d'outil n'est pas un echec -----------------------------

def _suite(monkeypatch, tmp_path):
    from caviardoc import models
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    monkeypatch.setattr(maj.paths, "uv_exe", lambda: None)
    monkeypatch.setattr(models, "check",
                        lambda spec: SimpleNamespace(complete=True))


def test_sans_outil_de_mesure_la_suite_s_acheve(monkeypatch, tmp_path):
    _suite(monkeypatch, tmp_path)
    monkeypatch.setattr(maj.paths, "project_root", lambda: tmp_path / "vide")
    paths.data_dir().mkdir(parents=True, exist_ok=True)
    marqueur = paths.data_dir() / maj.FICHIER_SUITE
    marqueur.write_text("{}", encoding="utf-8")
    compte = maj.appliquer_suite({"applique": True, "rappel_avant": 95.0},
                                 remesurer=True)
    assert compte["rappel_apres"] is None
    assert not marqueur.exists()



# --- 2. c() imbrique ne produit plus de « ? » -------------------------------

def test_c_imbrique_garde_ses_codes(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda: True)
    sortie = cli.c(f"a {cli.c('--tout', 'cyan')} b", "dim")
    assert "?[" not in sortie and "\x1b[36m" in sortie


def test_chaine_de_document_reste_nettoyee(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_color_enabled", lambda: True)
    assert "\x1b" not in cli._propre("x\x1b[2Ay")


# --- 3. insertion pure : pas d'espace ---------------------------------------

def test_nom_coupe_par_une_insertion_reste_entier():
    xml = DOCUMENT.format(corps=_p(
        "<w:r><w:t>Dupon</w:t></w:r><w:ins><w:r><w:t>t</w:t></w:r></w:ins>"))
    texte, _ = lire_partie(xml)
    assert "Dupont" in texte and "Dupon t" not in texte


# --- 4. assainir_nom ---------------------------------------------------------

@pytest.mark.parametrize("brut,attendu", [
    ("rapport..pdf", "rapport..pdf"),
    ("..\\..\\x.pdf", "x.pdf"),
    ("../../etc/x.pdf", "x.pdf"),
    ("a/b\\c.docx", "c.docx"),
])
def test_assainir_nom(brut, attendu):
    assert interface.assainir_nom(brut) == attendu


def test_assainir_nom_point_seul():
    assert interface.assainir_nom("..") == "document"
    assert interface.assainir_nom("a/..") == "document"


# === Deuxieme passe =========================================================

def test_deux_suppressions_de_suite_se_joignent():
    xml = DOCUMENT.format(corps=_p(
        "<w:del><w:r><w:delText>Dup</w:delText></w:r></w:del>"
        "<w:del><w:r><w:delText>ont</w:delText></w:r></w:del>"))
    texte, _ = lire_partie(xml)
    assert "Dupont" in texte


def test_deux_insertions_de_suite_se_joignent():
    xml = DOCUMENT.format(corps=_p(
        "<w:ins><w:r><w:t>Dup</w:t></w:r></w:ins>"
        "<w:ins><w:r><w:t>ont</w:t></w:r></w:ins>"))
    texte, _ = lire_partie(xml)
    assert "Dupont" in texte


def test_insertion_puis_suppression_se_separent():
    xml = DOCUMENT.format(corps=_p(
        "<w:ins><w:r><w:t>13.03.1974</w:t></w:r></w:ins>"
        "<w:del><w:r><w:delText>12.03.1974</w:delText></w:r></w:del>"))
    texte, _ = lire_partie(xml)
    assert "13.03.197412.03.1974" not in texte


def test_mesure_muette_est_un_echec(monkeypatch, tmp_path):
    _suite(monkeypatch, tmp_path)
    racine = tmp_path / "p"
    (racine / "tools").mkdir(parents=True)
    (racine / "tools" / "mesurer.py").write_text("")
    py = tmp_path / "py"
    py.write_text("")
    monkeypatch.setattr(maj.paths, "project_root", lambda: racine)
    from caviardoc import doctor
    monkeypatch.setattr(doctor, "_venv_python", lambda d: py)
    monkeypatch.setattr(maj.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="rien a lire", stderr=""))
    with pytest.raises(maj.MajImpossible):
        maj.appliquer_suite({"applique": True}, remesurer=True)


def test_cli_nettoie_les_chaines_de_document(tmp_path, monkeypatch, capsys):
    from caviardoc.documents import docx as mod_docx
    from test_cli_caviarder import _args, _sans_modele
    from test_docx import _docx, _t
    _sans_modele(monkeypatch)
    mal = "\x1b[2J"
    rapport = SimpleNamespace(
        parties=[], noms_patient=set(), repli_complet=False, corrigees=[],
        corrigees_texte=[], fuites_texte=[], fuites=[],
        erreur_verification="verif" + mal, verifie=False,
        proprietes=0, metadonnees=["m" + mal], images_metadonnees=[],
        images_lues=[], objets=0, zones_ignorees=["z" + mal])
    monkeypatch.setattr(mod_docx, "caviarder_docx", lambda *a, **kw: rapport)
    source = tmp_path / "d.docx"
    source.write_bytes(_docx(_p(_t("x"))))
    cli.cmd_caviarder(_args([source, tmp_path / ("absent" + mal + ".docx")],
                            tmp_path / "out"))
    assert "\x1b[2J" not in capsys.readouterr().out


@pytest.mark.parametrize("brut,attendu", [
    ('a<b>c:d"e|f?g*h.pdf', "a_b_c_d_e_f_g_h.pdf"),
    ("a\x00b\x1f.pdf", "a_b_.pdf"),
    ("rapport.pdf. ", "rapport.pdf"),
    ("CON.pdf", "_CON.pdf"),
    ("nul.docx", "_nul.docx"),
    ("com1.pdf", "_com1.pdf"),
    ("LPT9", "_LPT9"),
    ("console.pdf", "console.pdf"),
])
def test_assainir_nom_windows(brut, attendu):
    assert interface.assainir_nom(brut) == attendu


def test_cli_pdf_nettoie_reserve_et_erreur_de_verification(
        tmp_path, monkeypatch, capsys):
    from caviardoc.documents import caviardage, qualite
    from test_cli_caviarder import _args, _sans_modele
    _sans_modele(monkeypatch)
    page = SimpleNamespace(numero=1, mots_reconnus=3, source="ocr",
                           entites=[], boites=0, zones=0, duree=0.1,
                           reserve="reserve\x1b[2Jpiegee\rx")
    rapport = SimpleNamespace(
        pages=[page], noms_patient=set(), repli_complet=False, corrigees=[],
        corrigees_texte=[], fuites_texte=[], fuites=[], verifie=False,
        erreur_verification="verif\x1b[31mpiegee\rx")
    monkeypatch.setattr(qualite, "diagnostiquer_pdf",
                        lambda chemin: SimpleNamespace(explication="ok"))
    monkeypatch.setattr(caviardage, "caviarder_pdf", lambda *a, **kw: rapport)
    source = tmp_path / "d.pdf"
    source.write_bytes(b"%PDF-1.4")

    cli.cmd_caviarder(_args([source], tmp_path / "out"))

    sortie = capsys.readouterr().out
    assert "reserve" in sortie and "verif" in sortie
    assert "\x1b" not in sortie and "\r" not in sortie
