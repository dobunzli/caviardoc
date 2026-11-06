"""Mise a jour : ce qu'elle refuse, et ce qu'elle verifie apres coup.

Le depot etant public, le canal de mise a jour est un chemin d'execution de
code vers des postes qui detiennent des dossiers medicaux. La verification est
automatique, l'application ne l'est pas, et rien ne s'ecrase en silence.

Et parce qu'une mise a jour remplace le code, les dependances et possiblement
le modele, le rappel mesure avant elle ne decrit plus la machine : ces tests
verrouillent la remesure et l'alerte de regression.

Les depots sont fabriques ici, en local : aucun test ne touche au reseau.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import maj  # noqa: E402
from caviardoc.maj import MajImpossible  # noqa: E402

def _git_utilisable() -> str | None:
    """Le git que la mise a jour emploierait, ou None.

    Les depots d'essai se fabriquent avec lui, et non avec le premier `git`
    du PATH : sur un Mac dont la licence d'Xcode n'est pas acceptee, celui-la
    refuse tout, et chaque test echouait des sa preparation.
    """
    try:
        return maj.executable_git()
    except MajImpossible:
        return None


GIT = _git_utilisable()

pytestmark = pytest.mark.skipif(GIT is None, reason="aucun git utilisable")


def _git(racine, *arguments):
    return subprocess.run([GIT, *arguments], cwd=str(racine),
                          capture_output=True, text=True, check=True)


def _depot(racine: Path) -> Path:
    racine.mkdir(parents=True, exist_ok=True)
    _git(racine, "init", "--quiet", "--initial-branch", maj.BRANCHE)
    _git(racine, "config", "user.email", "essai@example.invalid")
    _git(racine, "config", "user.name", "Essai")
    (racine / "fichier.txt").write_text("premier\n")
    _git(racine, "add", "-A")
    _git(racine, "commit", "--quiet", "-m", "Premier commit")
    return racine


@pytest.fixture
def installe(tmp_path, monkeypatch):
    """Un depot « distant », et un clone qui joue l'installation locale."""
    distant = _depot(tmp_path / "distant")
    local = tmp_path / "local"
    subprocess.run([GIT, "clone", "--quiet", str(distant), str(local)],
                   check=True, capture_output=True)
    _git(local, "config", "user.email", "essai@example.invalid")
    _git(local, "config", "user.name", "Essai")

    monkeypatch.setattr(maj.paths, "repo_root", lambda: local)
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    return distant, local


def _sans_uv(monkeypatch):
    """Neutralise les etapes « dependances » et « modele », sans masquer git.

    Deux precautions, pour deux pieges distincts.

    Masquer `shutil.which` en bloc cacherait aussi git, et la mise a jour se
    croirait hors d'un depot : le test ne mesurerait plus rien.

    Et le repertoire de donnees etant temporaire, le modele y est toujours
    absent : sans ce leurre, chaque test telechargerait 572 Mo pour de vrai.
    La suite est passee de cinq minutes a quelques secondes en le posant.
    """
    from caviardoc import models

    vrai = shutil.which
    monkeypatch.setattr(maj.shutil, "which",
                        lambda nom: None if nom == "uv" else vrai(nom))

    class _Complet:
        complete = True
        missing_bytes = 0

    monkeypatch.setattr(models, "check", lambda spec, deep=False: _Complet())
    monkeypatch.setattr(models, "download",
                        lambda *a, **kw: pytest.fail(
                            "aucun test ne doit telecharger le modele"))


def _nouveau_commit(distant, message="Deuxieme commit"):
    (distant / "fichier.txt").write_text("deuxieme\n")
    _git(distant, "add", "-A")
    _git(distant, "commit", "--quiet", "-m", message)


class TestVerifier:
    def test_hors_dun_depot_git_la_maj_est_impossible(self, tmp_path,
                                                      monkeypatch):
        """Ni git, ni version inscrite par l'archive de GitHub : une copie
        faite a la main ne peut pas savoir ce qu'elle est."""
        nu = tmp_path / "nu"
        (nu / "programme").mkdir(parents=True)
        monkeypatch.setattr(maj.paths, "repo_root", lambda: nu)
        monkeypatch.setattr(maj.paths, "project_root", lambda: nu / "programme")
        with pytest.raises(MajImpossible, match="ni avec git"):
            maj.verifier(reseau=False)

    def test_a_jour(self, installe):
        etat = maj.verifier()
        assert etat.retard == 0
        assert not etat.disponible
        assert etat.propre

    def test_une_version_disponible_est_vue_et_detaillee(self, installe):
        distant, _ = installe
        _nouveau_commit(distant, "Corriger la propagation")

        etat = maj.verifier()
        assert etat.retard == 1
        assert etat.disponible
        assert any("Corriger la propagation" in l for l in etat.changements), \
            "l'utilisateur doit voir ce qui change avant d'accepter"

    def test_un_arbre_sale_empeche_la_maj(self, installe):
        """Une mise a jour ecraserait le travail local : elle s'y refuse."""
        _, local = installe
        (local / "fichier.txt").write_text("modifie a la main\n")

        etat = maj.verifier()
        assert not etat.propre
        assert "écraserait" in etat.empeche
        assert not etat.disponible

    def test_verifier_ne_modifie_rien(self, installe):
        distant, local = installe
        _nouveau_commit(distant)
        avant = (local / "fichier.txt").read_text()

        maj.verifier()

        assert (local / "fichier.txt").read_text() == avant
        assert maj._revision("HEAD").resume == "Premier commit"


class TestAppliquer:
    def test_refuse_sur_un_arbre_sale(self, installe):
        distant, local = installe
        _nouveau_commit(distant)
        (local / "fichier.txt").write_text("travail en cours\n")

        with pytest.raises(MajImpossible, match="écraserait"):
            maj.appliquer(remesurer=False)
        assert (local / "fichier.txt").read_text() == "travail en cours\n"

    def test_deja_a_jour_ne_fait_rien(self, installe):
        compte = maj.appliquer(remesurer=False)
        assert compte["applique"] is False
        assert compte["motif"] == "deja a jour"

    def test_une_divergence_arrete_tout(self, installe):
        """Sans fusion automatique : on ne fabrique pas un etat non teste."""
        distant, local = installe
        _nouveau_commit(distant, "Cote distant")
        (local / "autre.txt").write_text("cote local\n")
        _git(local, "add", "-A")
        _git(local, "commit", "--quiet", "-m", "Cote local")

        with pytest.raises(MajImpossible, match="divergé"):
            maj.appliquer(remesurer=False)

    def test_applique_et_avance(self, installe, monkeypatch):
        distant, local = installe
        _nouveau_commit(distant, "Corriger un faux positif")
        _sans_uv(monkeypatch)

        lignes = []
        compte = maj.appliquer(journal=lignes.append, remesurer=False)

        assert compte["applique"] is True
        assert compte["commits"] == 1
        assert (local / "fichier.txt").read_text() == "deuxieme\n"
        assert any("git merge --ff-only" in l for l in lignes)


class TestRappel:
    def test_le_rappel_se_note_et_se_relit(self, installe):
        assert maj.dernier_rappel() is None
        maj.noter_rappel(97.3)
        assert maj.dernier_rappel() == 97.3

    def test_le_rappel_nefface_pas_le_modele_retenu(self, installe):
        """Les deux vivent dans le meme state.json."""
        from caviardoc import models
        models.definir_modele_actif(models.DEFAULT_MODEL)
        maj.noter_rappel(95.0)

        assert models.modele_actif() == models.DEFAULT_MODEL
        assert maj.dernier_rappel() == 95.0

    def test_un_etat_abime_ne_bloque_pas(self, installe):
        from caviardoc import paths
        paths.ensure_dirs()
        paths.state_file().write_text("{pas du json", encoding="utf-8")
        assert maj.dernier_rappel() is None

    def test_une_baisse_de_rappel_est_signalee(self, installe, monkeypatch):
        """C'est la raison d'etre de la remesure."""
        distant, _ = installe
        _nouveau_commit(distant)
        _sans_uv(monkeypatch)
        maj.noter_rappel(97.3)

        def mesure_degradee(journal=None):
            # Le vrai `mesurer` enregistre lui-meme, par `--enregistrer` :
            # le leurre doit en faire autant, sans quoi le test verifierait
            # une chaine que la realite ne suit plus.
            maj.noter_rappel(62.2)
            return 62.2

        monkeypatch.setattr(maj, "mesurer", mesure_degradee)

        compte = maj.appliquer(remesurer=True)

        assert compte["rappel_avant"] == 97.3
        assert compte["rappel_apres"] == 62.2
        assert compte["regression"] is True
        assert maj.dernier_rappel() == 62.2

    def test_un_rappel_stable_ne_declenche_pas_dalerte(self, installe,
                                                       monkeypatch):
        distant, _ = installe
        _nouveau_commit(distant)
        _sans_uv(monkeypatch)
        maj.noter_rappel(97.3)
        monkeypatch.setattr(maj, "mesurer", lambda journal=None: 97.3)

        compte = maj.appliquer(remesurer=True)
        assert compte["regression"] is False

    def test_sans_mesure_precedente_aucune_regression(self, installe,
                                                      monkeypatch):
        distant, _ = installe
        _nouveau_commit(distant)
        _sans_uv(monkeypatch)
        monkeypatch.setattr(maj, "mesurer", lambda journal=None: 62.2)

        compte = maj.appliquer(remesurer=True)
        assert compte["rappel_avant"] is None
        assert compte["regression"] is False


class TestCli:
    def test_la_commande_existe_et_nappliquee_rien_par_defaut(self):
        from caviardoc.cli import build_parser
        args = build_parser().parse_args(["maj"])
        assert args.appliquer is False

    def test_appliquer_est_explicite(self):
        from caviardoc.cli import build_parser
        args = build_parser().parse_args(["maj", "--appliquer"])
        assert args.appliquer is True


# --------------------------------------------------------------------------- #
# Ce que la mise a jour preserve
# --------------------------------------------------------------------------- #

class TestExtrasPreserves:
    """`uv sync` elague : ce qui n'est pas demande est desinstalle.

    Une mise a jour retirait ainsi pytest, et la suite de tests devenait
    injouable apres chaque `maj`. Sur un projet dont la discipline est de
    mesurer apres chaque changement, retirer en silence de quoi verifier est
    le contraire de ce qu'on veut.
    """

    def test_le_moteur_et_les_documents_sont_toujours_demandes(self, monkeypatch):
        monkeypatch.setattr(maj, "_dev_installe", lambda: False)
        extras = maj.extras_sync()
        assert extras.count("--extra") == 2
        assert "engine" in extras and "documents" in extras

    def test_dev_est_preserve_s_il_etait_la(self, monkeypatch):
        monkeypatch.setattr(maj, "_dev_installe", lambda: True)
        assert "dev" in maj.extras_sync()

    def test_dev_n_est_pas_impose(self, monkeypatch):
        """Une machine qui ne teste pas garde un venv leger."""
        monkeypatch.setattr(maj, "_dev_installe", lambda: False)
        assert "dev" not in maj.extras_sync()

    def test_la_detection_cherche_pytest_dans_le_venv(self, tmp_path, monkeypatch):
        from caviardoc import paths

        venv = tmp_path / "venv"
        (venv / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
        monkeypatch.setattr(paths, "venv_dir", lambda: venv)
        assert not maj._dev_installe()

        (venv / "lib" / "python3.11" / "site-packages" / "pytest").mkdir()
        assert maj._dev_installe()


@pytest.mark.skipif(sys.platform == "win32", reason="faux git en script shell")
class TestGitQuiRefuse:
    """Sur macOS, `/usr/bin/git` refuse tout tant que la licence d'Xcode n'est
    pas acceptee. La mise a jour en concluait « pas une copie du depot » et
    conseillait de tout retelecharger — alors qu'un autre git, a cote,
    fonctionnait."""

    REFUS = ("You have not agreed to the Xcode license agreements. Please run "
             "'sudo xcodebuild -license' from within a Terminal window.")

    @pytest.fixture(autouse=True)
    def _cache(self):
        maj.executable_git.cache_clear()
        yield
        maj.executable_git.cache_clear()

    def _faux_git(self, tmp_path):
        faux = tmp_path / "faux" / "git"
        faux.parent.mkdir()
        faux.write_text(f"#!/bin/sh\necho \"{self.REFUS}\" >&2\nexit 69\n")
        faux.chmod(0o755)
        return str(faux)

    def _which(self, monkeypatch, faux):
        vrai = shutil.which
        monkeypatch.setattr(maj.shutil, "which",
                            lambda nom: faux if nom == "git" else vrai(nom))

    def test_un_git_de_secours_prend_le_relais(self, installe, tmp_path,
                                               monkeypatch):
        vrai = GIT
        self._which(monkeypatch, self._faux_git(tmp_path))
        monkeypatch.setattr(maj, "GIT_DE_SECOURS", (vrai,))
        assert maj.executable_git() == vrai
        assert maj.verifier(reseau=False).retard == 0

    def test_sans_secours_la_vraie_raison_est_donnee(self, installe, tmp_path,
                                                     monkeypatch):
        self._which(monkeypatch, self._faux_git(tmp_path))
        monkeypatch.setattr(maj, "GIT_DE_SECOURS", ())
        with pytest.raises(MajImpossible, match="licence") as e:
            maj.verifier(reseau=False)
        assert "pas une copie" not in str(e.value)
        assert "xcodebuild -license" in str(e.value)



# --- Sans git : l'archive ZIP de GitHub --------------------------------------

ANCIEN = "a" * 40
NOUVEAU = "b" * 40


def _zip(fichiers: dict[str, str], prefixe="caviardoc-" + NOUVEAU) -> bytes:
    import io
    import zipfile
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as z:
        for nom, contenu in fichiers.items():
            z.writestr(f"{prefixe}/{nom}", contenu)
    return tampon.getvalue()


def _version(commit: str) -> str:
    return f'COMMIT = "{commit}"\nDATE = "2026-10-09"\n'


@pytest.fixture
def copie_zip(tmp_path, monkeypatch):
    """Une copie decompressee depuis GitHub : pas de .git, version inscrite."""
    racine = tmp_path / "copie"
    paquet = racine / "programme" / "src" / "caviardoc"
    paquet.mkdir(parents=True)
    (paquet / "version_archive.py").write_text(_version(ANCIEN))
    (paquet / "__init__.py").write_text("")
    (paquet / "retire.py").write_text("ancien module")
    (racine / "programme" / "pyproject.toml").write_text("[project]")
    (racine / "notes-perso.txt").write_text("a moi")
    monkeypatch.setattr(maj.paths, "repo_root", lambda: racine)
    monkeypatch.setattr(maj.paths, "project_root", lambda: racine / "programme")
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    return racine


def _commit(sha):
    return {"sha": sha, "commit": {"message": f"Commit {sha[:3]}\n\ndetail",
                                   "committer": {"date": "2026-10-10T08:00:00Z"}}}


def _github_simule(*commits, archive=b"", statut=None, demandes=None):
    """GitHub, vu de l'API : la tete de la branche, la comparaison, l'archive.

    La tete est le dernier des ``commits``, ou la version installee s'il n'y
    en a pas.
    """
    tete = commits[-1] if commits else ANCIEN

    def github(chemin, delai=30, introuvable=""):
        if demandes is not None:
            demandes.append(chemin)
        if chemin.startswith("https://codeload"):
            return archive
        if chemin == f"commits/{maj.BRANCHE}":
            return json.dumps(_commit(tete)).encode()
        assert chemin == f"compare/{ANCIEN}...{tete}", chemin
        return json.dumps({
            "status": statut or ("ahead" if commits else "identical"),
            "ahead_by": len(commits),
            "commits": [_commit(c) for c in commits]}).encode()
    return github


class TestArchive:
    def test_la_version_inscrite_par_git_archive_est_lue(self, copie_zip):
        assert maj.commit_archive() == (ANCIEN, "2026-10-09")

    def test_une_copie_git_n_a_pas_de_version_d_archive(self):
        """Dans ce depot, `export-subst` n'a pas joue : rien d'inscrit."""
        assert maj.commit_archive() is None

    def test_gitattributes_demande_la_substitution(self):
        attributs = (Path(__file__).resolve().parents[2] / ".gitattributes").read_text()
        assert "programme/src/caviardoc/version_archive.py export-subst" in attributs

    def test_a_jour(self, copie_zip, monkeypatch):
        monkeypatch.setattr(maj, "_github", _github_simule())
        etat = maj.verifier()
        assert etat.retard == 0 and not etat.disponible and not etat.empeche

    def test_une_version_disponible_est_detaillee(self, copie_zip, monkeypatch):
        demandes = []
        monkeypatch.setattr(maj, "_github", _github_simule(
            "c" * 40, NOUVEAU, demandes=demandes))
        etat = maj.verifier()
        # La tete est demandee a part : la liste d'une comparaison est
        # tronquee au-dela de 250 commits.
        assert demandes == [f"commits/{maj.BRANCHE}",
                            f"compare/{ANCIEN}...{NOUVEAU}"]
        assert etat.disponible and etat.retard == 2
        assert etat.cible == NOUVEAU
        assert etat.changements[0] == "bbbbbbb  Commit bbb"

    def test_une_copie_qui_a_diverge_est_refusee(self, copie_zip, monkeypatch):
        monkeypatch.setattr(maj, "_github", _github_simule(
            NOUVEAU, statut="diverged"))
        assert "ne correspond pas" in maj.verifier().empeche

    def test_sans_reseau_la_verification_le_dit(self, copie_zip, monkeypatch):
        def injoignable(chemin, delai=30, introuvable=""):
            raise MajImpossible("Vérification impossible : le serveur ne répond pas.")
        monkeypatch.setattr(maj, "_github", injoignable)
        assert "impossible" in maj.verifier().empeche

    def test_un_404_distingue_le_depot_de_la_version(self, copie_zip, monkeypatch):
        import urllib.error
        import urllib.request

        def urlopen(requete, timeout=0):
            raise urllib.error.HTTPError(requete.full_url, 404, "x", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        # La tete de la branche absente : le depot lui-meme.
        assert "privé ou renommé" in maj.verifier().empeche

        # La tete trouvee, mais pas la comparaison : la version.
        vrai = maj._github

        def tete_seule(chemin, delai=30, introuvable=""):
            if chemin == f"commits/{maj.BRANCHE}":
                return json.dumps(_commit(NOUVEAU)).encode()
            return vrai(chemin, delai, introuvable)
        monkeypatch.setattr(maj, "_github", tete_seule)
        assert "versions publiées" in maj.verifier().empeche

    def test_applique_l_archive_du_commit_annonce(self, copie_zip, monkeypatch):
        nouvelle = _zip({
            "programme/pyproject.toml": "[project] # nouveau",
            "programme/src/caviardoc/__init__.py": "",
            "programme/src/caviardoc/version_archive.py": _version(NOUVEAU),
            "programme/src/caviardoc/ajoute.py": "nouveau module",
            "README.md": "nouveau readme",
        })
        demandes = []
        monkeypatch.setattr(maj, "_github", _github_simule(
            NOUVEAU, archive=nouvelle, demandes=demandes))
        compte = maj.appliquer_code()
        assert compte["applique"] and compte["vers"] == NOUVEAU[:7]
        assert demandes[-1].endswith(f"/zip/{NOUVEAU}")
        paquet = copie_zip / "programme" / "src" / "caviardoc"
        assert (paquet / "ajoute.py").read_text() == "nouveau module"
        assert not (paquet / "retire.py").exists(), "un module retire a survecu"
        assert (copie_zip / "notes-perso.txt").read_text() == "a moi"
        assert maj.commit_archive() == (NOUVEAU, "2026-10-09")

    @pytest.mark.parametrize("nom", ["../evade.txt", "programme/../../evade.txt"])
    def test_une_archive_qui_sort_du_dossier_est_refusee(self, copie_zip,
                                                         monkeypatch, nom):
        piegee = _zip({"programme/pyproject.toml": "",
                       "programme/src/caviardoc/__init__.py": "",
                       "programme/src/caviardoc/version_archive.py": "",
                       nom: "x"})
        monkeypatch.setattr(maj, "_github", _github_simule(NOUVEAU, archive=piegee))
        with pytest.raises(MajImpossible, match="suspecte"):
            maj.appliquer_code()
        assert not (copie_zip.parent / "evade.txt").exists()
        assert maj.commit_archive() == (ANCIEN, "2026-10-09"), "rien ne devait changer"

    def test_une_archive_etrangere_est_refusee(self, copie_zip, monkeypatch):
        autre = _zip({"README.md": "autre projet"})
        monkeypatch.setattr(maj, "_github", _github_simule(NOUVEAU, archive=autre))
        with pytest.raises(MajImpossible, match="pas celle de"):
            maj.appliquer_code()


    def test_plus_de_250_commits_de_retard(self, copie_zip, monkeypatch):
        """La comparaison ne liste que les 250 premiers : la cible reste la
        tete de la branche, et le retard celui qu'annonce GitHub."""
        tronques = [f"{i:040x}" for i in range(1, 251)]
        simule = _github_simule(*tronques, NOUVEAU)

        def github(chemin, delai=30, introuvable=""):
            corps = simule(chemin, delai)
            if chemin.startswith("compare/"):
                donnees = json.loads(corps)
                donnees.update(ahead_by=300, commits=donnees["commits"][:250])
                corps = json.dumps(donnees).encode()
            return corps

        monkeypatch.setattr(maj, "_github", github)
        etat = maj.verifier()
        assert etat.cible == NOUVEAU and etat.retard == 300
        assert etat.distante.reference == NOUVEAU[:7]
