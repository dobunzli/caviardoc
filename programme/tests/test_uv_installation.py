"""L'etape uv : poser uv quand il manque, au lieu de renvoyer a un terminal.

Cas reel : un Windows qui avait deja un Python. Les lanceurs n'installent uv que
pour obtenir un Python ; l'etape « Bibliotheques » en a pourtant besoin, et la
page disait seulement « winget install astral-sh.uv ».
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import installateur, paths  # noqa: E402


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """Une machine sans uv : ni dans le PATH, ni dans ~/.local/bin."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("shutil.which", lambda nom: None)
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "donnees"))
    return tmp_path / "home"


def _poser_uv(home: Path) -> Path:
    suffixe = ".exe" if sys.platform == "win32" else ""
    exe = home / ".local" / "bin" / f"uv{suffixe}"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


class _Reponse:
    def __init__(self, octets=b"echo installe"):
        self._octets = octets

    def read(self):
        return self._octets

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_uv_hors_du_path_mais_dans_local_bin_est_retrouve(machine):
    """Juste apres l'installation, le PATH du processus ne le contient pas."""
    exe = _poser_uv(machine)
    assert paths.uv_exe() == str(exe)


def test_aucun_uv_nulle_part(machine):
    assert paths.uv_exe() is None


def test_uv_deja_present_ne_telecharge_rien(machine, monkeypatch):
    _poser_uv(machine)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail(
        "ne doit rien telecharger"))
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "ok"


def _archive_tar(octets=b"#!/bin/sh\n"):
    import io
    import tarfile
    tampon = io.BytesIO()
    with tarfile.open(fileobj=tampon, mode="w:gz") as t:
        info = tarfile.TarInfo("uv-x/uv")
        info.size = len(octets)
        t.addfile(info, io.BytesIO(octets))
    return tampon.getvalue()


def _archive_zip():
    import io
    import zipfile
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        z.writestr("uv.exe", b"MZ")
        z.writestr("uvx.exe", b"MZ")
    return tampon.getvalue()


def _servir(monkeypatch, octets, archive="uv-x.tar.gz"):
    import hashlib
    monkeypatch.setattr(installateur, "asset_uv", lambda *a, **k: (
        archive, hashlib.sha256(octets).hexdigest()))
    vus = []

    def ouvrir(requete, **k):
        vus.append(requete.full_url)
        return _Reponse(octets)

    monkeypatch.setattr("urllib.request.urlopen", ouvrir)
    return vus


def test_uv_absent_est_installe_depuis_l_archive_verifiee(machine, monkeypatch):
    vus = _servir(monkeypatch, _archive_tar())
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(
        "aucun script d'installation ne doit etre execute"))
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)

    assert etape.etat == "ok", etape.message
    assert vus == [f"https://github.com/astral-sh/uv/releases/download/"
                   f"{installateur.UV_VERSION}/uv-x.tar.gz"]
    assert (machine / ".local" / "bin" / "uv").read_bytes() == b"#!/bin/sh\n"
    # La desinstallation saura que ce uv est le notre.
    from caviardoc import desinstallation
    assert desinstallation.pose_par_nous().get("uv")


def test_archive_zip_pose_uv_exe(machine, monkeypatch):
    _servir(monkeypatch, _archive_zip(), archive="uv-x.zip")
    monkeypatch.setattr(sys, "platform", "win32")
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "ok", etape.message
    assert (machine / ".local" / "bin" / "uv.exe").exists()


def test_empreinte_differente_refuse_et_n_installe_rien(machine, monkeypatch):
    _servir(monkeypatch, _archive_tar())
    monkeypatch.setattr(installateur, "asset_uv",
                        lambda *a, **k: ("uv-x.tar.gz", "0" * 64))
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "echec"
    assert "SHA-256" in etape.message and "refusé" in etape.message
    assert not (machine / ".local").exists()
    from caviardoc import desinstallation
    assert not desinstallation.pose_par_nous().get("uv")


def test_plateforme_inconnue_n_installe_rien(machine, monkeypatch):
    monkeypatch.setattr(installateur, "asset_uv", lambda *a, **k: None)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail(
        "ne doit rien telecharger"))
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "echec"


def test_archive_sans_uv_n_est_pas_prise_pour_un_succes(machine, monkeypatch):
    _servir(monkeypatch, b"pas une archive")
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "echec"
    assert "échoué" in etape.message


@pytest.mark.parametrize("plateforme, machine_, archive", [
    ("darwin", "arm64", "uv-aarch64-apple-darwin.tar.gz"),
    ("darwin", "x86_64", "uv-x86_64-apple-darwin.tar.gz"),
    ("win32", "AMD64", "uv-x86_64-pc-windows-msvc.zip"),
    ("win32", "ARM64", "uv-aarch64-pc-windows-msvc.zip"),
    ("linux", "x86_64", "uv-x86_64-unknown-linux-gnu.tar.gz"),
    ("linux", "aarch64", "uv-aarch64-unknown-linux-gnu.tar.gz"),
])
def test_correspondance_plateforme_archive(plateforme, machine_, archive):
    nom, empreinte = installateur.asset_uv(plateforme, machine_)
    assert nom == archive
    assert len(empreinte) == 64 and int(empreinte, 16) >= 0


def test_machine_non_prevue_n_a_pas_d_archive():
    assert installateur.asset_uv("win32", "x86") is None
    assert installateur.asset_uv("darwin", "ppc") is None


def test_empreintes_identiques_dans_les_trois_installateurs():
    """Python, shell et PowerShell epinglent la meme version et les memes
    empreintes : une divergence ferait verifier contre une autre valeur."""
    outils = Path(__file__).resolve().parents[1] / "tools"
    sh = (outils / "installer-uv.sh").read_text(encoding="utf-8")
    ps1 = (outils / "installer-uv.ps1").read_text(encoding="utf-8")
    assert f'UV_VERSION="{installateur.UV_VERSION}"' in sh
    assert f'$UvVersion = "{installateur.UV_VERSION}"' in ps1
    for archive, empreinte in installateur.UV_ASSETS.values():
        triplet = archive[len("uv-"):].rsplit(".tar.gz", 1)[0].removesuffix(".zip")
        texte = ps1 if archive.endswith(".zip") else sh
        assert f'"{triplet}"' in texte, archive
        assert f'"{empreinte}"' in texte, archive


def test_uv_deja_present_trop_ancien_est_signale(machine, monkeypatch):
    _poser_uv(machine)
    monkeypatch.setattr(installateur, "_version_uv", lambda uv: (0, 1, 0))
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "ok"
    assert any("plus ancien" in ligne for ligne in etape.journal)


def test_telechargement_impossible_dit_quoi_faire(machine, monkeypatch):
    def refuse(*a, **k):
        raise OSError("pas de reseau")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    etape = installateur.Etape("uv", "uv", "")
    installateur.etape_uv(etape)
    assert etape.etat == "echec"
    assert "connexion" in etape.message
    assert installateur.REMEDE_UV in etape.remede


def test_tout_installer_survit_au_redemarrage_apres_les_bibliotheques():
    """Constate sur un Windows : une fois les bibliotheques installees, la page
    redemarre le serveur puis se recharge, ce qui effacait la boucle de « Tout
    installer ». Modele, Tesseract et mesure restaient « a lancer »."""
    script = installateur._SCRIPT
    # L'intention est gardee pour la session de l'onglet, et reprise au chargement.
    assert "sessionStorage.setItem(MEMOIRE_TOUT" in script
    assert "if (toutEstEnCours()) tout();" in script
    # On ne lance pas la suite sur un serveur qui redemarre.
    assert "if (relance) return;" in script
    # Et si le serveur ne revient pas, on n'attend pas indefiniment.
    assert "retenirTout(false);\n}" in script


def test_l_etape_uv_est_automatique_et_dit_d_ou_elle_telecharge():
    etape = installateur.etapes_par_defaut().par_cle("uv")
    assert etape.automatique
    assert "github.com/astral-sh/uv" in etape.explication


def test_chaque_uv_sync_est_verrouille():
    """`--locked` : une installation suit exactement uv.lock, ou echoue ; elle
    ne re-resout jamais les dependances. Aucun repli sur un sync libre."""
    import re
    racine = Path(__file__).resolve().parents[1]
    depot = racine.parent
    sources = list((racine / "src" / "caviardoc").glob("*.py"))
    for source in sources:
        texte = source.read_text(encoding="utf-8")
        for m in re.finditer(r'\[\s*(?:uv|[a-z_.]*uv\w*)\s*,\s*"(sync|run)"(.*?)\]',
                             texte):
            if m.group(1) == "sync":
                assert '"--locked"' in m.group(2), f"{source.name}: {m.group(0)}"
    ensemble = {
        racine / "lancer.sh", racine / "bootstrap.sh", racine / "bootstrap.ps1",
        racine / "tools" / "trouver-python.sh",
        racine / "tools" / "installer-uv.sh", racine / "tools" / "installer-uv.ps1",
        *depot.glob("▶ Ouvrir*"), *(depot / ".github" / "workflows").glob("*.yml"),
    }
    for fichier in ensemble:
        for n, ligne in enumerate(fichier.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\buv (sync|run)\b", ligne) and not ligne.lstrip().startswith(("#", "rem", "echo")):
                assert "--locked" in ligne or "--no-sync" in ligne, \
                    f"{fichier.name}:{n}: {ligne.strip()}"
    # Et les trois appels reels du code portent bien l'option.
    for nom in ("installateur.py", "maj.py", "cli.py"):
        assert '"sync", "--locked"' in (racine / "src" / "caviardoc" / nom).read_text(encoding="utf-8")

