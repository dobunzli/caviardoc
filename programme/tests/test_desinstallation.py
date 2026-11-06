"""Desinstaller : reprendre tout ce qu'on a pose, et rien d'autre.

Ces tests existent a cause d'un degat reel. Un essai a supprime
``~/.local/share/uv`` au motif qu'un lanceur avait installe uv : le dossier
contenait un interpreteur Python pose trois mois plus tot, pour un autre
projet. Avoir installe un outil ne fait pas de nous le proprietaire de tout ce
que cet outil range ensuite.

La regle qui en decoule se verrouille ici : le dossier global de uv et le depot
ne figurent jamais parmi ce qui peut etre supprime, quelle que soit la trace.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import desinstallation, paths  # noqa: E402


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Une machine complete a nous : dossier de donnees, cache, dossier perso."""
    maison = tmp_path / "maison"
    (maison / ".local" / "bin").mkdir(parents=True)
    (maison / ".local" / "share" / "uv" / "python").mkdir(parents=True)
    (maison / ".local" / "share" / "uv" / "python" / "un-python").write_bytes(b"x" * 4096)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: maison))
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "donnees"))
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "pycache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(maison / ".local" / "share"))
    # Sous Windows, uv range ses donnees sous LOCALAPPDATA.
    monkeypatch.setenv("LOCALAPPDATA", str(maison / ".local" / "share"))
    # Un vrai uv du PATH annoncerait le cache de la machine, et le mesurerait.
    monkeypatch.setenv("UV_CACHE_DIR", str(maison / ".cache" / "uv"))

    paths.data_dir().mkdir(parents=True)
    (paths.data_dir() / "state.json").write_text("{}")
    (tmp_path / "pycache").mkdir()
    return maison


def _cles(elements) -> set[str]:
    return {e.cle for e in elements}


# --- La regle, verrouillee -------------------------------------------------

def test_le_dossier_global_de_uv_n_est_jamais_supprimable(machine):
    """Le degat qui a motive ce fichier. Meme trace en main, on n'y touche pas."""
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")

    chemins = [e.chemin for e in desinstallation.inventaire()]
    assert desinstallation._uv_donnees() not in chemins
    assert not any(desinstallation._uv_donnees() in c.parents for c in chemins)


def test_le_dossier_global_de_uv_est_montre_avec_sa_commande(machine):
    """On ne le supprime pas, mais on ne le cache pas non plus."""
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")

    dehors = desinstallation.laisses_a_l_utilisateur()
    uv = [d for d in dehors if d.chemin == desinstallation._uv_donnees()]
    assert len(uv) == 1
    assert uv[0].octets > 0, "le chiffre doit permettre de decider"
    # Ce dossier tient les Pythons de uv, pas son cache : `uv cache clean` n'y
    # change rien.
    assert uv[0].commande.startswith("uv python uninstall")
    # Le Python du lanceur n'est plus a retirer a la main : `inventaire` s'en
    # charge (voir plus bas).
    assert "3.12" not in uv[0].commande


def test_le_depot_n_est_jamais_supprimable(machine):
    chemins = [e.chemin for e in desinstallation.inventaire()]
    assert desinstallation.depot() not in chemins
    assert any(d.chemin == desinstallation.depot()
               for d in desinstallation.laisses_a_l_utilisateur())


# --- Ce qu'on prend, et sous quelle condition ------------------------------

def test_nos_dossiers_sont_pris_par_defaut(machine):
    elements = desinstallation.inventaire()
    assert "donnees" in _cles(elements)
    assert "pycache" in _cles(elements)
    for e in elements:
        if e.cle in ("donnees", "pycache"):
            assert e.par_defaut


def test_uv_n_est_pris_que_si_nous_l_avons_installe(machine):
    binaire = machine / ".local" / "bin" / ("uv.exe" if sys.platform == "win32"
                                           else "uv")
    binaire.write_text("")

    sans_trace = [e for e in desinstallation.inventaire() if e.cle == "uv"]
    assert len(sans_trace) == 1
    assert not sans_trace[0].par_defaut, "un uv trouve n'est pas un uv pose"

    desinstallation.noter_installe("uv")
    avec_trace = [e for e in desinstallation.inventaire() if e.cle == "uv"]
    assert avec_trace[0].par_defaut


def test_un_uv_venu_d_ailleurs_reste_invisible(machine):
    """Un uv d'Homebrew ou de winget n'est pas dans ~/.local/bin : pas a nous."""
    assert "uv" not in _cles(desinstallation.inventaire())


def test_l_inventaire_ignore_ce_qui_n_existe_pas(machine):
    for element in desinstallation.inventaire():
        assert element.chemin.exists()


# --- La trace --------------------------------------------------------------

def test_la_trace_survit_a_plusieurs_ecritures(machine):
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")
    assert desinstallation.pose_par_nous() == {"uv": True, "python": "3.12"}


def test_une_trace_illisible_ne_fait_pas_echouer(machine):
    (paths.data_dir() / desinstallation.TRACE).write_text("ceci n'est pas du json")
    assert desinstallation.pose_par_nous() == {}
    assert desinstallation.inventaire(), "l'inventaire doit rester utilisable"


def test_la_trace_disparait_avec_le_dossier_qu_elle_decrit(machine):
    """Elle vit dans le dossier de donnees : la reprise ne laisse pas de fiche."""
    desinstallation.noter_installe("uv")
    assert paths.data_dir() in (paths.data_dir() / desinstallation.TRACE).parents


# --- La suppression --------------------------------------------------------

def test_supprimer_ne_touche_qu_aux_elements_donnes(machine):
    voisin = paths.data_dir().parent / "voisin"
    voisin.mkdir()
    (voisin / "fichier").write_text("a garder")

    a_prendre = [e for e in desinstallation.inventaire() if e.cle == "donnees"]
    assert desinstallation.supprimer(a_prendre) == []
    assert not paths.data_dir().exists()
    assert (voisin / "fichier").exists()


def test_supprimer_rend_compte_de_sa_progression(machine):
    a_prendre = [e for e in desinstallation.inventaire() if e.cle == "donnees"]
    vus = []
    assert desinstallation.supprimer(
        a_prendre, progression=lambda *a: vus.append(a)) == []
    assert vus == [(0, 1, a_prendre[0].titre), (1, 1, "")]


def test_un_echec_est_rapporte_en_clair(machine, monkeypatch):
    def refuse(*_a, **_k):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(desinstallation.shutil, "rmtree", refuse)

    echecs = desinstallation.supprimer(
        [e for e in desinstallation.inventaire() if e.cle == "donnees"])
    assert len(echecs) == 1
    assert "Permission denied" in echecs[0]
    assert paths.data_dir().exists()


def test_on_refuse_de_scier_la_branche(machine, monkeypatch):
    """Supprimer le venv depuis lequel on s'execute echoue a moitie sous Windows."""
    venv = paths.venv_dir()
    venv.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(venv))

    donnees = [e for e in desinstallation.inventaire() if e.cle == "donnees"][0]
    assert desinstallation.s_execute_depuis(donnees)

    pycache = [e for e in desinstallation.inventaire() if e.cle == "pycache"][0]
    assert not desinstallation.s_execute_depuis(pycache)


# --- Le Python que nous avons installe -------------------------------------
# Constate sur un Windows : « Desinstaller tout » laissait le Python que le
# lanceur avait installe. Il se retire par uv, jamais en effacant le dossier de
# uv ; et jamais s'il etait la avant nous.

besoin_de_posix = pytest.mark.skipif(sys.platform == "win32",
                                     reason="faux uv ecrit en shell")


@pytest.fixture
def faux_uv(machine, monkeypatch):
    """Un uv qui sait `python find` et `python uninstall`, et note ses appels."""
    monkeypatch.setattr("shutil.which", lambda nom: None)
    racine = machine / ".local" / "share" / "uv" / "python"
    notre = racine / "cpython-3.12.5-test" / "bin"
    autre = racine / "cpython-3.9.1-ancien" / "bin"
    for dossier in (notre, autre):
        dossier.mkdir(parents=True)
        (dossier / "python3").write_bytes(b"x" * 1024)
    journal = machine / "appels-uv.txt"
    cache = machine / ".cache" / "uv"
    cache.mkdir(parents=True)
    (cache / "archive").write_bytes(b"x" * 2048)
    uv = machine / ".local" / "bin" / "uv"
    uv.write_text(f"""#!/bin/sh
echo "$@" >> "{journal}"
case "$*" in
  # Sans --system, uv rend le .venv du dossier courant, pas notre Python.
  "python find --managed-python --system 3.12") echo "{notre / 'python3'}" ;;
  "python find --managed-python 3.12") echo "{machine / '.venv' / 'bin' / 'python3'}" ;;
  "python uninstall 3.12") rm -rf "{notre.parent}" ;;
  # Comme uv sous FORCE_COLOR : sans NO_COLOR, le chemin sort en couleurs.
  "cache dir") if [ -n "$NO_COLOR" ]; then echo "{cache}"
               else printf '\\033[36m%s\\033[39m\\n' "{cache}"; fi ;;
  "cache clean") rm -rf "{cache}"/* ;;
  *) exit 1 ;;
esac
""")
    uv.chmod(0o755)
    return notre.parent, autre.parent, journal


@besoin_de_posix
def test_le_python_pose_par_nous_est_dans_l_inventaire(faux_uv):
    notre, _, _ = faux_uv
    desinstallation.noter_installe("python", "3.12")
    python = [e for e in desinstallation.inventaire() if e.cle == "python"]
    assert len(python) == 1
    assert python[0].chemin == notre
    assert python[0].par_defaut
    assert python[0].commande[1:] == ("python", "uninstall", "3.12")


@besoin_de_posix
def test_un_python_deja_present_n_est_pas_repris(faux_uv):
    """Sans trace, ce Python n'est pas a nous : on n'y touche pas."""
    assert "python" not in _cles(desinstallation.inventaire())


@besoin_de_posix
def test_le_python_est_retire_avant_uv(faux_uv):
    """La commande a besoin du binaire uv : il doit etre encore la."""
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")
    cles = [e.cle for e in desinstallation.inventaire()]
    assert cles.index("python") < cles.index("uv")


@besoin_de_posix
def test_supprimer_retire_notre_python_et_lui_seul(faux_uv, machine):
    notre, autre, journal = faux_uv
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")
    retenus = [e for e in desinstallation.inventaire()
               if e.cle in ("python", "uv")]

    assert desinstallation.supprimer(retenus) == []

    assert not notre.exists(), "le Python du lanceur doit disparaitre"
    assert autre.exists(), "un autre Python de uv ne nous appartient pas"
    assert desinstallation._uv_donnees().exists(), "le dossier de uv reste"
    assert "python uninstall 3.12" in journal.read_text()
    assert not (machine / ".local" / "bin" / "uv").exists(), "uv part apres"


@besoin_de_posix
def test_le_cache_de_uv_n_est_vide_que_si_uv_est_a_nous(faux_uv, machine):
    """Repartir de zero demande de vider ce que notre uv a telecharge — mais
    pas le cache d'un uv qui servait deja a autre chose."""
    cache = machine / ".cache" / "uv"
    assert "cache-uv" not in _cles(desinstallation.inventaire())

    desinstallation.noter_installe("uv")
    elements = desinstallation.inventaire()
    (el,) = [e for e in elements if e.cle == "cache-uv"]
    assert el.commande[1:] == ("cache", "clean")
    assert [e.cle for e in elements].index("cache-uv") < \
        [e.cle for e in elements].index("uv"), "avant le binaire dont il a besoin"

    assert desinstallation.supprimer([el]) == []
    assert cache.exists() and not list(cache.iterdir()), \
        "vide par uv, le dossier lui-meme n'est pas efface"


@besoin_de_posix
def test_le_cache_d_un_uv_deja_la_est_montre_avec_son_chemin(faux_uv, machine):
    """Montre la ou il est vraiment — pas le dossier des Pythons de uv."""
    cache = machine / ".cache" / "uv"
    (montre,) = [d for d in desinstallation.laisses_a_l_utilisateur()
                 if d.commande == "uv cache clean"]
    assert montre.chemin == cache
    assert montre.octets > 0

    desinstallation.noter_installe("uv")
    assert not any(d.chemin == cache
                   for d in desinstallation.laisses_a_l_utilisateur()), \
        "uv a nous : son cache est deja dans l'inventaire"


@besoin_de_posix
def test_un_echec_de_uv_est_rapporte(faux_uv, machine):
    desinstallation.noter_installe("python", "3.12")
    python = [e for e in desinstallation.inventaire() if e.cle == "python"]
    (machine / ".local" / "bin" / "uv").write_text(
        "#!/bin/sh\necho boum >&2\nexit 2\n")
    echecs = desinstallation.supprimer(python)
    assert len(echecs) == 1 and "boum" in echecs[0]


@besoin_de_posix
def test_sous_windows_le_retrait_est_confie_a_un_processus_detache(
        faux_uv, monkeypatch):
    """On ne peut pas effacer le Python qui execute la desinstallation : le
    retrait de Python puis de uv attend, dans un cmd detache, qu'elle finisse."""
    notre, _, _ = faux_uv
    desinstallation.noter_installe("uv")
    desinstallation.noter_installe("python", "3.12")
    retenus = [e for e in desinstallation.inventaire()
               if e.cle in ("python", "uv")]
    binaires = [e.chemin for e in retenus if e.cle == "uv"]

    lances = []
    monkeypatch.setattr(desinstallation.subprocess, "Popen",
                        lambda cmd, **kw: lances.append(cmd))
    faux_sys = type("S", (), {"platform": "win32", "prefix": "/venv",
                              "base_prefix": str(notre), "executable": "x"})
    monkeypatch.setattr(desinstallation, "sys", faux_sys)

    assert desinstallation.supprimer(retenus) == []

    assert notre.exists() and all(p.exists() for p in binaires), \
        "rien n'est efface tout de suite : le processus courant en depend"
    (ligne,) = lances
    assert "uninstall" in ligne and "del /f /q" in ligne
    assert ligne.index("uninstall") < ligne.index("del /f /q"), \
        "uv ne doit etre efface qu'apres avoir retire Python"
