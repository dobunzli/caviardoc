"""Tesseract pose par nos soins : ou on le trouve, et ce qu'on garde.

L'enjeu n'est pas l'OCR mais le perimetre. Ce module telecharge 200 Mo depuis
un canal tiers et les installe sur la machine de l'utilisateur : ce qui compte
est qu'il n'ecrive que dans le dossier de l'application, qu'il verifie ce qu'il
telecharge, et qu'il ne prenne jamais la place d'un Tesseract systeme sans le
dire.

Aucun test ici ne touche au reseau.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import paths, tesseract  # noqa: E402
from caviardoc.models import IntegrityError  # noqa: E402


@pytest.fixture
def dossier(tmp_path, monkeypatch):
    """Un dossier de donnees a nous, pour ne rien ecrire chez l'utilisateur."""
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "donnees"))
    return tmp_path


def _poser_faux_tesseract(base: Path, langues=("fra", "eng", "osd")) -> Path:
    """Fabrique un prefixe qui a la forme d'une installation conda."""
    exe = base / "bin" / "tesseract"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    donnees = base / "share" / "tessdata"
    donnees.mkdir(parents=True, exist_ok=True)
    for langue in langues:
        (donnees / f"{langue}.traineddata").write_bytes(b"x" * 1024)
    return exe


# --- La plateforme ---------------------------------------------------------

def test_chaque_plateforme_visee_a_son_empreinte():
    """Une plateforme sans empreinte epinglee ne peut rien installer."""
    for cle in ("osx-arm64", "osx-64", "win-64", "linux-64", "linux-aarch64"):
        assert cle in tesseract.MICROMAMBA_SHA256
        assert len(tesseract.MICROMAMBA_SHA256[cle]) == 64


def test_la_plateforme_courante_est_reconnue():
    assert tesseract.plateforme_conda() in tesseract.MICROMAMBA_SHA256


def test_une_plateforme_inconnue_leve_plutot_que_de_deviner(monkeypatch):
    monkeypatch.setattr(sys, "platform", "sunos5")
    with pytest.raises(tesseract.InstallationImpossible):
        tesseract.plateforme_conda()


# --- Ou l'on ecrit ---------------------------------------------------------

def test_tout_vit_dans_le_dossier_de_l_application(dossier):
    """Rien hors du dossier de donnees : c'est ce qui rend la reprise possible."""
    racine = paths.data_dir()
    assert racine in tesseract.prefixe().parents
    assert racine in tesseract.chemin_micromamba().parents


def test_le_cache_de_micromamba_ne_va_pas_chez_l_utilisateur(dossier):
    """Sans --root-prefix, micromamba ecrit dans ~/.local/share/mamba."""
    assert paths.data_dir() in tesseract._racine_micromamba().parents


# --- Quel binaire, et avec quelles donnees ---------------------------------

def test_notre_binaire_passe_devant_celui_du_systeme(dossier, monkeypatch):
    """Le notre a le francais ; celui du systeme, on n'en sait rien."""
    monkeypatch.setattr(tesseract.shutil, "which", lambda _: "/usr/bin/tesseract")
    assert tesseract.binaire() == Path("/usr/bin/tesseract")

    attendu = _poser_faux_tesseract(tesseract.prefixe())
    assert tesseract.binaire() == attendu
    assert tesseract.installe()


def test_sans_installation_locale_on_prend_celle_du_systeme(dossier, monkeypatch):
    monkeypatch.setattr(tesseract.shutil, "which", lambda _: "/usr/bin/tesseract")
    assert tesseract.binaire_local() is None
    assert tesseract.binaire() == Path("/usr/bin/tesseract")


def test_sans_rien_du_tout_on_rend_none(dossier, monkeypatch):
    monkeypatch.setattr(tesseract.shutil, "which", lambda _: None)
    assert tesseract.binaire() is None
    assert tesseract.langues() == []


def test_on_n_impose_tessdata_qu_a_notre_propre_binaire(dossier, monkeypatch):
    """Ecraser TESSDATA_PREFIX d'un Tesseract systeme casserait ce qui marche."""
    monkeypatch.setattr(tesseract.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    assert "TESSDATA_PREFIX" not in tesseract.environnement()

    _poser_faux_tesseract(tesseract.prefixe())
    env = tesseract.environnement()
    assert env["TESSDATA_PREFIX"] == str(tesseract.prefixe() / "share" / "tessdata")


def test_la_disposition_windows_est_reconnue(dossier):
    """conda-forge pose les binaires Windows sous Library/bin."""
    base = tesseract.prefixe()
    exe = base / "Library" / "bin" / "tesseract.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (base / "Library" / "share" / "tessdata").mkdir(parents=True)
    assert tesseract.binaire_local() == exe
    assert tesseract.tessdata() == base / "Library" / "share" / "tessdata"


# --- Ce qu'on garde --------------------------------------------------------

def test_l_elagage_ne_garde_que_les_langues_utilisees(dossier):
    _poser_faux_tesseract(tesseract.prefixe(),
                          langues=("fra", "eng", "osd", "deu", "jpn", "rus"))
    libere = tesseract._elaguer_langues()
    assert libere == 3 * 1024

    restantes = {f.stem for f in
                 (tesseract.prefixe() / "share" / "tessdata").glob("*.traineddata")}
    assert restantes == set(tesseract.LANGUES_GARDEES)


def test_le_francais_est_garde(dossier):
    assert "fra" in tesseract.LANGUES_GARDEES


def test_la_taille_ne_compte_pas_deux_fois_les_liens(dossier):
    """conda pose beaucoup de liens ; les suivre triplait le chiffre annonce."""
    base = tesseract.prefixe()
    lib = base / "lib"
    lib.mkdir(parents=True)
    reel = lib / "libicu.78.3.dylib"
    reel.write_bytes(b"x" * 100_000)
    (lib / "libicu.78.dylib").symlink_to(reel)
    (lib / "libicu.dylib").symlink_to(reel)
    assert tesseract.taille_installee() == 100_000


# --- Ce qu'on verifie ------------------------------------------------------

def test_un_fichier_qui_ne_correspond_pas_est_refuse(dossier, tmp_path):
    """L'empreinte epinglee est la seule garantie sur un binaire telecharge."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"contenu quelconque")
    dest = tmp_path / "dest.bin"

    with pytest.raises(IntegrityError):
        tesseract._telecharger_verifie(source.as_uri(), dest, "0" * 64,
                                       "essai", None)
    assert not dest.exists(), "un fichier refuse ne doit rien laisser"
    assert not dest.with_suffix(".bin.part").exists()


def test_un_fichier_conforme_est_mis_en_place(dossier, tmp_path):
    import hashlib
    source = tmp_path / "source.bin"
    source.write_bytes(b"contenu quelconque")
    empreinte = hashlib.sha256(source.read_bytes()).hexdigest()
    dest = tmp_path / "dest.bin"

    vu = []
    tesseract._telecharger_verifie(source.as_uri(), dest, empreinte, "essai",
                                   lambda n, lus, total: vu.append(lus))
    assert dest.read_bytes() == b"contenu quelconque"
    assert vu, "la progression doit etre rapportee"


def test_la_version_de_tesseract_est_epinglee():
    """Une version d'OCR qui bouge change le texte lu, donc le rappel mesure."""
    assert tesseract.TESSERACT_VERSION.count(".") == 2
    assert tesseract.CANAL == "conda-forge"


def test_desinstaller_ne_touche_qu_a_nos_dossiers(dossier):
    _poser_faux_tesseract(tesseract.prefixe())
    tesseract.chemin_micromamba().parent.mkdir(parents=True, exist_ok=True)
    tesseract.chemin_micromamba().write_text("")
    temoin = paths.data_dir() / "state.json"
    temoin.write_text("{}")

    tesseract.desinstaller()

    assert not tesseract.prefixe().exists()
    assert not tesseract._racine_micromamba().exists()
    assert temoin.exists(), "desinstaller Tesseract ne doit pas vider le reste"
