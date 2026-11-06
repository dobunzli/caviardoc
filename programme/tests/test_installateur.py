"""Installation guidee : ce qui echoue doit se relancer, et se dire.

Un poste de cabinet perd le reseau au milieu d'un telechargement de 570 Mo.
C'est le cas normal, pas l'exception. Une etape qui echoue doit donc le dire
clairement, proposer un remede, et pouvoir etre relancee seule — sans
recommencer ce qui a deja reussi.

Ce module doit aussi fonctionner sur une machine ou rien n'est installe :
c'est sa raison d'etre. Il ne peut donc importer que la bibliotheque standard
et les modules du projet qui s'en contentent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import installateur  # noqa: E402
from caviardoc.installateur import Etape, Installation, executer  # noqa: E402
from caviardoc.web_commun import ENTETE_JETON  # noqa: E402


def _attendre(installation, cle, delai=10):
    limite = time.time() + delai
    while time.time() < limite:
        etape = installation.par_cle(cle)
        if etape.etat not in ("en_attente", "en_cours"):
            return etape
        time.sleep(0.02)
    raise AssertionError(f"{cle} n'a pas abouti : {etape.etat}")


class TestSocle:
    def test_le_module_ne_depend_que_de_la_bibliotheque_standard(self):
        """Il doit s'ouvrir sur une machine ou rien n'est encore installe.

        Le test importe le module dans un interpreteur ou les dependances
        lourdes sont rendues introuvables. Si `installateur` en tirait une,
        l'import echouerait — et l'utilisateur se retrouverait sans page pour
        les installer, ce qui est exactement la situation a eviter.

        Un blocage explicite, et non un environnement vide : effacer les
        variables d'environnement empeche Python de demarrer sous Windows, ou
        il lui faut `SystemRoot` pour initialiser sa randomisation de hachage.
        Et `-I` aurait ignore `PYTHONPATH`, donc n'aurait rien importe du tout.
        """
        racine = Path(__file__).resolve().parents[1]
        code = (
            "import sys\n"
            "INTERDITS = {'pymupdf', 'fitz', 'gliner', 'onnxruntime', 'torch',\n"
            "             'transformers', 'docx', 'PIL', 'pytesseract',\n"
            "             'openpyxl', 'numpy'}\n"
            "class Barrage:\n"
            "    def find_module(self, nom, chemin=None):\n"
            "        return self.find_spec(nom, chemin)\n"
            "    def find_spec(self, nom, chemin=None, cible=None):\n"
            "        if nom.split('.')[0] in INTERDITS:\n"
            "            raise ImportError('dependance interdite : ' + nom)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Barrage())\n"
            "import caviardoc.installateur as i\n"
            "print(len(i.etapes_par_defaut().etapes))\n")
        env = dict(os.environ, PYTHONPATH=str(racine / "src"))
        res = subprocess.run([sys.executable, "-c", code], cwd=racine,
                             capture_output=True, text=True, env=env)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "6"

    def test_le_barrage_du_test_precedent_mord_vraiment(self):
        """Un test de non-dependance qui ne bloque rien ne prouve rien."""
        racine = Path(__file__).resolve().parents[1]
        code = (
            "import sys\n"
            "class Barrage:\n"
            "    def find_spec(self, nom, chemin=None, cible=None):\n"
            "        if nom.split('.')[0] == 'json':\n"
            "            raise ImportError('interdit')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Barrage())\n"
            "import caviardoc.installateur\n")
        env = dict(os.environ, PYTHONPATH=str(racine / "src"))
        res = subprocess.run([sys.executable, "-c", code], cwd=racine,
                             capture_output=True, text=True, env=env)
        assert res.returncode != 0, "le barrage devrait empecher cet import"
        assert "interdit" in res.stderr

    def test_les_etapes_indispensables_existent(self):
        cles = {e.cle for e in installateur.etapes_par_defaut().etapes}
        assert set(installateur.ETAPES_INDISPENSABLES) <= cles

    def test_chaque_etape_a_une_action(self):
        for e in installateur.etapes_par_defaut().etapes:
            assert e.cle in installateur.ACTIONS, f"{e.cle} sans action"

    def test_chaque_etape_sexplique(self):
        """L'utilisateur doit pouvoir refuser en connaissance de cause."""
        for e in installateur.etapes_par_defaut().etapes:
            assert len(e.explication) > 40, e.cle

    def test_les_explications_tiennent_en_deux_lignes(self):
        """Cette page se lit debout, pendant que ca telecharge.

        Le detail appartient au journal. Un paragraphe par etape rendait la
        liste si longue que le bouton principal tombait sous la ligne de
        flottaison.
        """
        for e in installateur.etapes_par_defaut().etapes:
            assert len(e.explication) < 175, f"{e.cle} : {len(e.explication)}"

    def test_les_libelles_affiches_sont_accentues(self):
        """Ce sont des textes d'interface, pas des commentaires de code."""
        attendus = ("Bibliothèques", "Modèle de détection",
                    "Lecture des documents scannés")
        titres = {e.titre for e in installateur.etapes_par_defaut().etapes}
        for titre in attendus:
            assert titre in titres, f"{titre} absent ou sans accents"


class TestEchecEtRelance:
    def _installation(self, action):
        etape = Etape("essai", "Essai", "Une etape d'essai pour les tests.")
        installation = Installation(etapes=[etape])
        installateur.ACTIONS["essai"] = action
        return installation

    def teardown_method(self):
        installateur.ACTIONS.pop("essai", None)

    def test_une_exception_devient_un_echec_lisible(self):
        def casse(etape):
            raise RuntimeError("le reseau a coupe")

        installation = self._installation(casse)
        executer(installation, "essai")
        etape = _attendre(installation, "essai")

        assert etape.etat == "echec"
        assert "le reseau a coupe" in etape.message
        assert any("RuntimeError" in l for l in etape.journal), \
            "la trace doit aller au journal"

    def test_une_etape_se_relance_apres_echec(self):
        tentatives = {"n": 0}

        def capricieuse(etape):
            tentatives["n"] += 1
            if tentatives["n"] == 1:
                raise RuntimeError("premiere tentative perdue")
            etape.etat = "ok"
            etape.message = "reussi a la seconde"

        installation = self._installation(capricieuse)
        executer(installation, "essai")
        assert _attendre(installation, "essai").etat == "echec"

        executer(installation, "essai")
        etape = _attendre(installation, "essai")
        assert etape.etat == "ok"
        assert etape.message == "reussi a la seconde"
        assert tentatives["n"] == 2

    def test_la_relance_efface_le_message_precedent(self):
        """Sinon un remede perime reste affiche a cote d'un succes."""
        etats = iter([("echec", "casse", "faites ceci"), ("ok", "repare", "")])

        def variable(etape):
            etat, message, remede = next(etats)
            etape.etat, etape.message, etape.remede = etat, message, remede

        installation = self._installation(variable)
        executer(installation, "essai")
        _attendre(installation, "essai")
        executer(installation, "essai")
        etape = _attendre(installation, "essai")

        assert etape.etat == "ok"
        assert etape.remede == ""

    def test_le_journal_sacumule_entre_les_tentatives(self):
        """C'est ce qu'on joint pour demander de l'aide : il doit tout garder."""
        def bruyante(etape):
            etape.noter("une ligne")
            etape.etat = "ok"

        installation = self._installation(bruyante)
        executer(installation, "essai"); _attendre(installation, "essai")
        executer(installation, "essai"); etape = _attendre(installation, "essai")

        assert etape.journal.count("une ligne") == 0, "les lignes sont horodatees"
        assert sum("une ligne" in l for l in etape.journal) == 2

    def test_une_etape_inconnue_est_refusee(self):
        installation = installateur.etapes_par_defaut()
        with pytest.raises(KeyError):
            executer(installation, "inexistante")


class TestServeur:
    def _lancer(self):
        adresse: dict = {}
        prete = threading.Event()
        arret = threading.Event()
        installation = installateur.etapes_par_defaut()

        fil = threading.Thread(
            target=lambda: installateur.servir(
                ouvrir=False, arret=arret, installation=installation,
                pret=lambda u: (adresse.update(url=u), prete.set())),
            daemon=True)
        fil.start()
        assert prete.wait(5), "le serveur n'a pas demarre"
        return adresse["url"], arret, fil

    def _parts(self, url):
        a = urllib.parse.urlparse(url)
        return f"{a.scheme}://{a.netloc}", urllib.parse.parse_qs(a.query)["jeton"][0]

    def test_la_page_saffiche(self):
        url, arret, fil = self._lancer()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                corps = r.read().decode("utf-8")
            assert 'id="etapes"' in corps
            assert "Installation de Caviardoc" in corps
        finally:
            arret.set(); fil.join(timeout=5)

    def test_les_actions_precedent_la_liste_des_etapes(self):
        """« Tout installer » se trouvait sous la ligne de flottaison.

        Il fallait faire defiler la liste entiere pour trouver le bouton qui
        lance l'installation — sur la page dont c'est l'unique objet.
        """
        url, arret, fil = self._lancer()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                corps = r.read().decode("utf-8")
            for cle in ('id="tout"', 'id="ouvrir"', "Journal détaillé"):
                assert corps.index(cle) < corps.index('id="etapes"'), cle
        finally:
            arret.set(); fil.join(timeout=5)

    def test_la_page_dit_ce_que_le_niveau_patient_change(self):
        """La separation des roles depend du reglage, et retombe en securite.

        La page la presentait comme une etape de la chaine, toujours active.
        Elle ne l'est qu'au niveau « patient », qui n'est pas le defaut.
        """
        url, arret, fil = self._lancer()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                corps = r.read().decode("utf-8")
            assert "par défaut" in corps
            assert "revient au niveau complet" in corps
        finally:
            arret.set(); fil.join(timeout=5)

    def test_la_page_dit_que_le_modele_napprend_pas(self):
        """Promettre un apprentissage qui n'existe pas serait un mensonge."""
        url, arret, fil = self._lancer()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                corps = r.read().decode("utf-8")
            assert "n'apprend pas de vos corrections" in corps
        finally:
            arret.set(); fil.join(timeout=5)

    def test_sans_jeton_tout_est_refuse(self):
        url, arret, fil = self._lancer()
        try:
            base, _ = self._parts(url)
            for chemin in ("/", "/etapes", "/journal"):
                with pytest.raises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(base + chemin, timeout=5)
                assert e.value.code == 403
        finally:
            arret.set(); fil.join(timeout=5)

    def test_lancer_sans_jeton_est_refuse(self):
        """Cette page execute des commandes : elle ne s'ouvre pas a tout venant."""
        url, arret, fil = self._lancer()
        try:
            base, _ = self._parts(url)
            req = urllib.request.Request(
                base + "/lancer", data=b'{"cle":"python"}', method="POST",
                headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 403
        finally:
            arret.set(); fil.join(timeout=5)

    def test_un_hote_etranger_est_refuse(self):
        url, arret, fil = self._lancer()
        try:
            req = urllib.request.Request(url)
            req.add_header("Host", "attaquant.example")
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 403
        finally:
            arret.set(); fil.join(timeout=5)

    def test_une_etape_se_lance_et_se_lit(self):
        url, arret, fil = self._lancer()
        try:
            base, jeton = self._parts(url)
            req = urllib.request.Request(
                base + "/lancer", data=json.dumps({"cle": "python"}).encode(),
                method="POST",
                headers={ENTETE_JETON: jeton,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                assert r.status == 200

            limite = time.time() + 10
            while time.time() < limite:
                req = urllib.request.Request(base + "/etapes",
                                             headers={ENTETE_JETON: jeton})
                with urllib.request.urlopen(req, timeout=5) as r:
                    vue = json.loads(r.read())
                python = next(e for e in vue["etapes"] if e["cle"] == "python")
                if python["etat"] == "ok":
                    break
                time.sleep(0.05)
            assert python["etat"] == "ok", python
            assert python["message"].startswith("Python 3.")
        finally:
            arret.set(); fil.join(timeout=5)

    def test_le_journal_est_servi_en_texte(self):
        url, arret, fil = self._lancer()
        try:
            base, jeton = self._parts(url)
            cible = f"{base}/journal?jeton={urllib.parse.quote(jeton)}"
            with urllib.request.urlopen(cible, timeout=5) as r:
                corps = r.read().decode("utf-8")
                assert r.headers.get_content_type() == "text/plain"
            assert "Python" in corps
        finally:
            arret.set(); fil.join(timeout=5)
