"""Les lanceurs : le seul code que l'utilisateur execute avant d'avoir Python.

Un lanceur qui echoue ne laisse aucun recours — ni journal, ni diagnostic, ni
`caviardoc doctor`, puisque rien de tout cela ne tourne encore. C'est le maillon
qu'on ne peut pas rattraper plus tard, donc celui qu'on verrouille ici.

Deux pieges, deja rencontres :

- accepter un Python trop vieux. `command -v python3` reussit sur un macOS
  d'origine, ou python3 vaut 3.9.6 ; l'installation partait, puis echouait
  plus loin sur un message que personne ne peut relier a la version.
- un `.bat` en fins de ligne Unix. cmd.exe cherche ses etiquettes par position
  dans le fichier : un `goto` peut sauter au mauvais endroit, en silence. On ne
  peut pas executer cmd.exe ici, alors on verifie le fichier.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

#: Le dossier du programme, ou vivent les scripts que les lanceurs appellent.
RACINE = Path(__file__).resolve().parents[1]
#: La racine du depot, un cran au-dessus : celle que l'utilisateur ouvre.
DEPOT = RACINE.parent
AIDE = RACINE / "tools" / "trouver-python.sh"
LANCEUR = RACINE / "lancer.sh"
BOOTSTRAP = RACINE / "bootstrap.sh"
#: Le nom que l'utilisateur double-clique. La plateforme y figure : sans
#: elle, le Finder montrait en tete le `.bat`, qu'un Mac ne sait pas lancer.
OUVRIR = "▶ Ouvrir Caviardoc"
MACOS = DEPOT / f"{OUVRIR} (Mac).command"
WINDOWS = DEPOT / f"{OUVRIR} (Windows).bat"

def _bash_fonctionne() -> bool:
    """Un bash qui execute vraiment quelque chose.

    Sous Windows, `bash` est souvent le relais de WSL : present dans le PATH,
    il ne fait qu'annoncer qu'aucune distribution n'est installee. Ces
    scripts ne visent d'ailleurs que macOS et Linux.
    """
    if sys.platform == "win32" or shutil.which("bash") is None:
        return False
    try:
        r = subprocess.run(["bash", "-c", "echo ok"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "ok"


besoin_de_bash = pytest.mark.skipif(not _bash_fonctionne(),
                                    reason="bash absent ou inutilisable")


def _sans_python(tmp_path: Path) -> Path:
    """Un PATH ou il n'y a aucun Python, et l'outillage dont les scripts usent.

    `/usr/bin` ne convient pas : Ubuntu y range un Python 3.12, qui rendait
    sans objet les tests de « Python absent ».
    """
    vide = tmp_path / "bin"
    vide.mkdir(exist_ok=True)
    for outil in ("bash", "sh", "uname", "touch", "cat", "dirname",
                  # installer-uv.sh : archive epinglee, empreinte, extraction.
                  "mktemp", "rm", "cut", "shasum", "sha256sum", "tar", "cp",
                  "mkdir", "chmod"):
        trouve = shutil.which(outil)
        if trouve and not (vide / outil).exists():
            (vide / outil).symlink_to(trouve)
    return vide


def sh(script: str, env: dict | None = None, entree: str = "") -> subprocess.CompletedProcess:
    """Execute un fragment apres avoir source l'aide, depuis la racine."""
    complet = f'cd "{RACINE}"\n. "{AIDE}"\n{script}\n'
    return subprocess.run(["bash", "-c", complet], input=entree,
                          capture_output=True, text=True, env=env)


# --- La version, et non la seule presence ----------------------------------

@besoin_de_bash
def test_accepte_l_interpreteur_courant():
    r = sh(f'python_convient "{sys.executable}" && echo oui')
    assert "oui" in r.stdout


@besoin_de_bash
def test_refuse_un_python_trop_vieux(tmp_path):
    """Le piege du macOS d'origine : python3 existe, mais c'est un 3.9."""
    faux = tmp_path / "python3"
    faux.write_text("#!/bin/sh\nexit 1\n")   # comme un 3.9 face au test de version
    faux.chmod(0o755)
    r = sh(f'python_convient "{faux}" && echo accepte || echo refuse')
    assert "refuse" in r.stdout


@besoin_de_bash
def test_refuse_ce_qui_n_existe_pas():
    r = sh('python_convient "/n/existe/pas" && echo accepte || echo refuse')
    assert "refuse" in r.stdout


@besoin_de_bash
def test_trouve_un_python_sur_cette_machine():
    r = sh('assurer_python && echo "PY=$PY"')
    assert r.returncode == 0
    chemin = [l for l in r.stdout.splitlines() if l.startswith("PY=")][0][3:]
    assert Path(chemin).exists()


# --- Le mur d'avant : on ne renvoie plus l'utilisateur bredouille -----------

@besoin_de_bash
def test_propose_uv_quand_python_manque(tmp_path):
    """Sans Python ni uv, on propose d'installer — on n'abandonne pas."""
    vide = _sans_python(tmp_path)
    env = {"HOME": str(tmp_path), "PATH": str(vide)}
    r = sh('assurer_python', env=env, entree="n\n")
    assert "uv" in r.stdout
    assert "astral-sh/uv" in r.stdout


@besoin_de_bash
def test_le_telechargement_est_annonce_et_soumis_a_accord(tmp_path):
    """Rien ne part sur le reseau avant que l'utilisateur ait dit oui."""
    vide = _sans_python(tmp_path)
    temoin = tmp_path / "curl-appele"
    faux_curl = vide / "curl"
    faux_curl.write_text(f'#!/bin/sh\ntouch "{temoin}"\nexit 1\n')
    faux_curl.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": str(vide)}

    refus = sh('assurer_python', env=env, entree="n\n")
    assert refus.returncode == 1
    assert not temoin.exists(), "curl a ete appele malgre le refus"

    sh('assurer_python', env=env, entree="o\n")
    assert temoin.exists(), "l'accord n'a pas declenche le telechargement"


@besoin_de_bash
def test_le_refus_laisse_une_porte_de_sortie(tmp_path):
    vide = _sans_python(tmp_path)
    env = {"HOME": str(tmp_path), "PATH": str(vide)}
    r = sh('assurer_python', env=env, entree="n\n")
    assert r.returncode == 1
    assert "python.org" in r.stdout


@besoin_de_bash
def test_un_venv_n_est_pas_pris_pour_un_python_de_uv(tmp_path):
    """Sans --system, uv rend le .venv du dossier courant ou de VIRTUAL_ENV :
    le lanceur s'en servait, et le croyait pose par uv."""
    vide = _sans_python(tmp_path)
    venv = tmp_path / ".venv" / "bin" / "python3"
    venv.parent.mkdir(parents=True)
    venv.write_text("#!/bin/sh\nexit 1\n")
    venv.chmod(0o755)
    faux_uv = vide / "uv"
    faux_uv.write_text(f"""#!/bin/sh
case "$*" in
  "python find --system 3.14") echo "{sys.executable}" ;;
  "python find 3.14") echo "{venv}" ;;
  *) exit 1 ;;
esac
""")
    faux_uv.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": str(vide)}

    r = sh('python_de_uv', env=env)
    assert r.stdout.strip() == sys.executable


# --- Une seule copie de la logique -----------------------------------------

def test_les_lanceurs_unix_partagent_la_meme_aide():
    for lanceur in (LANCEUR, BOOTSTRAP):
        assert "tools/trouver-python.sh" in lanceur.read_text(), lanceur.name


def test_le_lanceur_macos_delegue():
    assert "lancer.sh" in MACOS.read_text()


def test_les_lanceurs_entrent_dans_le_dossier_du_programme():
    """Les lanceurs sont a la racine, ce qu'ils appellent est un cran plus bas.

    `lancer.sh` et le `.bat` lisent `src` et `tools` en chemins relatifs : un
    lanceur qui resterait a la racine les chercherait la ou ils ne sont plus.
    """
    assert f'cd "$(dirname "$0")/{RACINE.name}"' in MACOS.read_text()
    assert f'cd /d "%~dp0{RACINE.name}"' in WINDOWS.read_text(encoding="utf-8")


def test_la_racine_ne_montre_que_les_lanceurs():
    """Le code etait range a plat a cote des lanceurs : une quinzaine
    d'entrees, parmi lesquelles il fallait trouver la seule a ouvrir.

    Le README et la licence restent a la racine : c'est la que GitHub les
    cherche pour les afficher.
    """
    if shutil.which("git") is None or not (DEPOT / ".git").exists():
        pytest.skip("pas une copie git du depot")
    suivis = subprocess.run(["git", "ls-files", "-z"], cwd=DEPOT,
                            capture_output=True, encoding="utf-8", check=True)
    racine = {chemin.split("/", 1)[0] for chemin in suivis.stdout.split("\0")
              if chemin and not chemin.startswith(".")}
    assert racine == {MACOS.name, WINDOWS.name, "README.md", "LICENSE",
                      RACINE.name}, racine


def test_aucun_lanceur_ne_dit_seulement_installez_python():
    """La phrase reste, mais comme repli — jamais comme unique reponse."""
    for lanceur in (LANCEUR, BOOTSTRAP, WINDOWS, AIDE):
        texte = lanceur.read_text(encoding="utf-8", errors="replace")
        if "python.org" in texte:
            assert "uv" in texte, f"{lanceur.name} abandonne sans proposer uv"


@besoin_de_bash
@pytest.mark.parametrize("script", [AIDE, LANCEUR, BOOTSTRAP, MACOS])
def test_syntaxe_shell(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- Le .bat, qu'on ne peut pas executer ici -------------------------------

def test_le_bat_est_en_crlf():
    """cmd.exe cherche ses etiquettes par position : les LF seuls le perdent."""
    brut = WINDOWS.read_bytes()
    lf_isoles = brut.replace(b"\r\n", b"").count(b"\n")
    assert lf_isoles == 0, f"{lf_isoles} fins de ligne Unix dans le .bat"


def test_git_garde_le_bat_en_crlf():
    attributs = (DEPOT / ".gitattributes").read_text()
    assert "*.bat text eol=crlf" in attributs


def test_chaque_goto_du_bat_a_son_etiquette():
    lignes = WINDOWS.read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()
    etiquettes = {l.strip()[1:].split()[0].lower()
                  for l in lignes if l.strip().startswith(":") and not l.strip().startswith("::")}
    cibles = set()
    for ligne in lignes:
        for mot in ("goto", "call"):
            marqueur = f"{mot} :"
            if marqueur in ligne.lower():
                debut = ligne.lower().index(marqueur) + len(marqueur)
                cibles.add(ligne[debut:].split()[0].lower())
    manquantes = cibles - etiquettes - {"eof"}
    assert not manquantes, f"goto sans etiquette : {sorted(manquantes)}"


def test_le_bat_verifie_la_version_et_propose_uv():
    texte = WINDOWS.read_text(encoding="utf-8")
    assert "version_info" in texte, "le .bat accepte n'importe quelle version"
    assert "astral-sh/uv" in texte
    assert "python install" in texte


# --- Les deux chemins Windows, qu'on ne peut pas faire partager du code -----

POWERSHELL = RACINE / "bootstrap.ps1"


def test_le_ps1_est_en_crlf():
    brut = POWERSHELL.read_bytes()
    assert brut.replace(b"\r\n", b"").count(b"\n") == 0


def _version(texte: str, *motifs: str) -> str:
    for motif in motifs:
        trouve = re.search(motif, texte)
        if trouve:
            return trouve.group(1)
    raise AssertionError(f"version introuvable, motifs {motifs}")


def test_les_deux_chemins_windows_s_accordent():
    """cmd et PowerShell ne peuvent pas partager de code : ils peuvent au moins
    ne pas se contredire sur ce qui compte."""
    bat = WINDOWS.read_text(encoding="utf-8")
    ps1 = POWERSHELL.read_text(encoding="utf-8")

    installee = (_version(bat, r"python install (\d+\.\d+)"),
                 _version(ps1, r'VersionInstallee\s*=\s*"(\d+\.\d+)"'))
    assert installee[0] == installee[1], f"versions installees differentes : {installee}"

    for accord in ("version_info[:2] >= (3, 11)",   # meme plancher
                   "astral-sh/uv"):                     # meme source pour uv
        assert accord in bat, f"absent du .bat : {accord}"
        assert accord in ps1, f"absent du .ps1 : {accord}"


def test_les_trois_plateformes_installent_la_meme_version():
    """Le shell aussi : trois implementations, une seule decision."""
    attendue = _version(AIDE.read_text(), r'VERSION_INSTALLEE:-(\d+\.\d+)')
    assert attendue == _version(WINDOWS.read_text(encoding="utf-8"),
                                r"python install (\d+\.\d+)")
    assert attendue == _version(POWERSHELL.read_text(encoding="utf-8"),
                                r'VersionInstallee\s*=\s*"(\d+\.\d+)"')


def test_le_ps1_demande_avant_de_telecharger():
    ps1 = POWERSHELL.read_text(encoding="utf-8")
    assert "Read-Host" in ps1, "le .ps1 telecharge sans demander"
    assert "astral-sh/uv" in ps1
    assert "python install" in ps1, "le .ps1 ne va pas jusqu'a installer Python"


# --- Les lanceurs de desinstallation ---------------------------------------

DESINSTALLER = RACINE / "desinstaller.sh"


def test_le_repli_terminal_de_desinstallation_existe():
    """La console s'en charge d'ordinaire ; ce script reste pour le cas ou
    elle ne demarre pas."""
    assert DESINSTALLER.is_file()


@besoin_de_bash
@pytest.mark.parametrize("script", [DESINSTALLER])
def test_syntaxe_des_lanceurs_de_desinstallation(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr




def test_desinstaller_n_installe_jamais_python():
    """Telecharger un interpreteur pour desinstaller serait absurde."""
    for lanceur in (DESINSTALLER,):
        texte = lanceur.read_text(encoding="utf-8")
        assert "assurer_python" not in texte
        assert "astral.sh" not in texte


def test_desinstaller_survit_a_l_absence_de_python():
    """Le cas ou l'on ne peut rien calculer doit quand meme dire quoi faire."""
    for lanceur in (DESINSTALLER,):
        texte = lanceur.read_text(encoding="utf-8")
        assert "caviardoc" in texte, f"{lanceur.name} ne nomme aucun chemin"


def test_les_lanceurs_notent_ce_qu_ils_posent():
    """Sans trace, la desinstallation ne saurait pas si uv est a nous."""
    for lanceur in (AIDE, POWERSHELL):
        assert "noter_installe" in lanceur.read_text(encoding="utf-8"), lanceur.name


@besoin_de_bash
def test_la_trace_n_est_posee_que_si_nous_avons_installe_uv():
    """Un uv deja present ne doit pas etre note comme notre fait."""
    texte = AIDE.read_text()
    assert "UV_POSE_PAR_NOUS" in texte
    # La note est conditionnee, pas systematique : uv et Python separement,
    # car un uv deja la n'empeche pas que Python soit de nous.
    assert ('[ -n "${UV_POSE_PAR_NOUS:-}" ] || '
            '[ -n "${PYTHON_POSE_PAR_NOUS:-}" ] || return 0') in texte
    assert "if os.environ.get('UV_POSE_PAR_NOUS')" in texte
    assert "if os.environ.get('PYTHON_POSE_PAR_NOUS')" in texte


def test_le_lanceur_windows_note_ce_qu_il_pose():
    """Constate sur un Windows : le .bat installait uv et Python sans rien
    noter, et « Desinstaller tout » laissait Python en place."""
    texte = (Path(__file__).resolve().parents[2]
             / "▶ Ouvrir Caviardoc (Windows).bat").read_text(encoding="utf-8")
    assert "noter_installe('uv')" in texte
    assert "noter_installe('python', '3.14')" in texte
    # uv n'est note que s'il est a nous ; Python, que s'il n'etait pas deja la.
    assert 'if defined UV_POSE "%PY%"' in texte
    assert 'if not defined PY_AVANT "%PY%"' in texte
    assert 'set "UV_POSE=1"' in texte


# --- PowerShell, quand on peut l'executer ----------------------------------
# Absent des machines de developpement macOS ; present sur les runners Windows,
# ou ces tests s'executent pour de vrai. Ailleurs ils sautent, et les controles
# statiques ci-dessous prennent le relais.

def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


besoin_de_powershell = pytest.mark.skipif(_powershell() is None,
                                          reason="PowerShell absent")


@besoin_de_powershell
def test_le_ps1_est_syntaxiquement_valide():
    script = (
        '$e=$null;$t=$null;'
        f'[void][System.Management.Automation.Language.Parser]::ParseFile('
        f'"{POWERSHELL}",[ref]$t,[ref]$e);'
        'if($e){$e|%{Write-Output "$($_.Extent.StartLineNumber): $($_.Message)"};exit 1}'
    )
    r = subprocess.run([_powershell(), "-NoProfile", "-Command", script],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@besoin_de_powershell
def test_le_ps1_rend_un_code_non_nul_quand_il_echoue(tmp_path):
    """Sans piege, une erreur terminale affichait une trace .NET puis rendait 0.

    L'appelant croyait a un succes alors que rien n'avait ete fait. Constate
    avec PowerShell 7.6 en supprimant LOCALAPPDATA.
    """
    casse = tmp_path / "casse.ps1"
    texte = POWERSHELL.read_text(encoding="utf-8").replace("\r\n", "\n")
    texte = texte.replace("Set-Location -Path $PSScriptRoot",
                          'Set-Location -Path $PSScriptRoot\nthrow "panne simulee"', 1)
    casse.write_text(texte.replace("\n", "\r\n"), encoding="utf-8")

    r = subprocess.run([_powershell(), "-NoProfile", "-File", str(casse)],
                       capture_output=True, text=True)
    assert r.returncode != 0, "une erreur terminale doit se voir du dehors"


def test_le_ps1_piege_les_erreurs_terminales():
    """Controle statique, pour les machines sans PowerShell."""
    texte = POWERSHELL.read_text(encoding="utf-8")
    assert "trap {" in texte, "sans piege, une erreur terminale rend 0"
    assert "exit 1" in texte


def test_le_ps1_ne_suppose_pas_localappdata_defini():
    """Join-Path leve sur un argument nul, et emportait tout le script."""
    texte = POWERSHELL.read_text(encoding="utf-8")
    bloc = texte[texte.index("PYTHONPYCACHEPREFIX"):]
    bloc = bloc[:bloc.index("}")]
    assert "if (-not $local)" in bloc, "aucun repli si LOCALAPPDATA manque"


# --- Le lanceur unique -----------------------------------------------------

UNIQUE = RACINE / "lancer.sh"
UNIQUE_MACOS = MACOS
UNIQUE_WINDOWS = WINDOWS


def test_le_lanceur_est_le_premier_fichier_du_dossier():
    """Entre `lancer.sh` et le `.command`, on ne savait pas lequel ouvrir.

    Le Finder et l'Explorateur classent les symboles avant les chiffres et les
    lettres : un nom qui commence par « ▶ » passe en tete. Encore faut-il
    qu'aucun autre fichier visible n'en fasse autant.
    """
    assert not OUVRIR[0].isalnum()
    visibles = [p.name for p in DEPOT.iterdir()
                if not p.name.startswith(".") and p.name not in ("__pycache__",)]
    en_tete = sorted(n for n in visibles if not n[0].isalnum())
    assert en_tete == [MACOS.name, WINDOWS.name], en_tete


def test_un_seul_fichier_suffit():
    """L'utilisateur ouvrait un fichier pour installer, un autre pour
    travailler, sans qu'aucun ne dise que l'autre existait."""
    for lanceur in (UNIQUE, UNIQUE_MACOS, UNIQUE_WINDOWS):
        assert lanceur.is_file(), lanceur.name


@besoin_de_bash
@pytest.mark.parametrize("script", [UNIQUE, UNIQUE_MACOS])
def test_syntaxe_du_lanceur_unique(script):
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_le_lanceur_unique_ouvre_la_console():
    """Ni l'installateur ni l'interface seuls : la page qui contient les deux."""
    for lanceur in (UNIQUE, UNIQUE_WINDOWS):
        texte = lanceur.read_text(encoding="utf-8")
        assert "caviardoc console" in texte, lanceur.name


def test_le_lanceur_unique_installe_python_au_besoin():
    """Il doit s'ouvrir sur une machine ou il n'y a rien, y compris Python."""
    assert "assurer_python" in UNIQUE.read_text()
    assert "astral-sh/uv" in UNIQUE_WINDOWS.read_text(encoding="utf-8")


def test_le_bat_unique_est_en_crlf():
    brut = UNIQUE_WINDOWS.read_bytes()
    assert brut.replace(b"\r\n", b"").count(b"\n") == 0


# --- Le Python de l'application, une fois installee ------------------------

@besoin_de_bash
def test_prefere_le_python_de_l_application(tmp_path):
    """Pose par uv, Python n'est pas dans le PATH : sans ce raccourci, chaque
    lancement annoncait « aucun Python » et repassait par l'installation."""
    venv_bin = tmp_path / "donnees" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    vide = _sans_python(tmp_path)
    env = {"HOME": str(tmp_path), "PATH": str(vide),
           "CAVIARDOC_DATA_DIR": str(tmp_path / "donnees")}
    r = sh('assurer_python && echo "PY=$PY"', env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"PY={venv_bin / 'python'}" in r.stdout
    assert "Aucun Python" not in r.stdout


@besoin_de_bash
def test_le_relais_xcode_n_est_jamais_execute_sans_les_outils():
    """Ailleurs que /usr/bin/python3, ou hors macOS, rien n'est ecarte."""
    r = sh('relais_xcode /opt/homebrew/bin/python3 && echo ecarte || echo garde')
    assert "garde" in r.stdout


def test_le_bat_lit_le_chemin_de_uv_en_utf8():
    """Un dossier personnel accentue rendait le Python installe introuvable."""
    texte = WINDOWS.read_text(encoding="utf-8")
    assert "chcp 65001" in texte
    assert "venv\\Scripts\\python.exe" in texte


@besoin_de_bash
def test_installer_uv_refuse_une_empreinte_differente(tmp_path):
    """Le script poseur de uv verifie le SHA-256 avant toute extraction."""
    vide = _sans_python(tmp_path)
    faux_curl = vide / "curl"
    faux_curl.write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do\n'
        '  [ "$1" = "-o" ] && { shift; sortie="$1"; }\n  shift\ndone\n'
        'echo contenu-falsifie > "$sortie"\n')
    faux_curl.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": str(vide)}
    r = subprocess.run(["sh", str(RACINE / "tools" / "installer-uv.sh")],
                       capture_output=True, text=True, env=env)
    if "n'est pas fourni pour cette machine" in r.stderr:
        pytest.skip("plateforme hors table des archives uv")
    assert r.returncode == 6, r.stderr
    assert "SHA-256" in r.stderr and "refuse" in r.stderr
    assert not (tmp_path / ".local" / "bin" / "uv").exists()


def test_le_poseur_de_uv_utilise_l_archive_epinglee_et_non_le_script_astral():
    for lanceur in (UNIQUE_WINDOWS, POWERSHELL, AIDE):
        assert "astral.sh/uv/install" not in lanceur.read_text(encoding="utf-8")
    assert "installer-uv.ps1" in UNIQUE_WINDOWS.read_text(encoding="utf-8")
    assert "installer-uv.ps1" in POWERSHELL.read_text(encoding="utf-8")
    assert "installer-uv.sh" in AIDE.read_text(encoding="utf-8")


def test_le_bat_distingue_l_empreinte_refusee_de_l_echec_generique():
    texte = UNIQUE_WINDOWS.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert texte.index("if errorlevel 6 goto :echec_empreinte") < texte.index(
        "if errorlevel 1 goto :echec_uv")
    bloc = texte.split("\n:echec_empreinte\n", 1)[1].split("\n\n", 1)[0]
    assert "empreinte" in bloc and "pause" in bloc and "exit /b 1" in bloc


def test_le_ps1_force_tls12_et_sort_en_6_sur_empreinte_differente():
    texte = (RACINE / "tools" / "installer-uv.ps1").read_text(encoding="utf-8")
    tls = "[Net.SecurityProtocolType]::Tls12"
    assert tls in texte and texte.index(tls) < texte.index("Invoke-WebRequest")
    assert "exit 6" in texte


def test_le_lanceur_unix_appelle_installer_uv_depuis_son_propre_dossier():
    texte = AIDE.read_text(encoding="utf-8")
    assert '"$(dirname "${BASH_SOURCE[0]:-$0}")/installer-uv.sh"' in texte
