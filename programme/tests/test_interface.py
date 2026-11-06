"""Interface d'accueil : depot, traitement, telechargement — et leurs refus.

La page recoit des documents de patients et les rend caviardes. Ce qui compte
ici n'est pas qu'elle fonctionne quand tout va bien, mais qu'elle refuse ce
qu'elle doit refuser : un format qu'elle ne sait pas traiter, un fichier
demesure, une requete sans jeton, un identifiant invente.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import interface  # noqa: E402
from caviardoc.interface import Atelier, deposer  # noqa: E402
from caviardoc.web_commun import ENTETE_JETON  # noqa: E402


@pytest.fixture
def atelier(tmp_path):
    return Atelier(racine=tmp_path)


class TestDepot:
    def test_un_pdf_est_accepte(self, atelier):
        octets = b"%PDF-1.4 ..."
        d = deposer(atelier, "rapport.pdf", io.BytesIO(octets), len(octets))
        assert d.nom == "rapport.pdf"
        assert d.chemin.suffix == ".pdf"
        assert d.chemin.read_bytes() == b"%PDF-1.4 ..."

    def test_le_nom_du_client_ne_devient_pas_un_chemin(self, atelier):
        """Le navigateur fournit le nom : il vient du document, donc de dehors."""
        d = deposer(atelier, "../../../etc/passwd.pdf", io.BytesIO(b"x" * 5), 5)
        assert d.chemin.parent == atelier.entrees()
        assert ".." not in str(d.chemin)
        # Le nom d'origine est conserve pour l'affichage, ampute de son chemin.
        assert d.nom == "passwd.pdf"

    def test_le_fichier_sur_disque_ne_porte_pas_le_nom_fourni(self, atelier):
        d = deposer(atelier, "Fontannaz Yannick.pdf", io.BytesIO(b"x" * 5), 5)
        assert "Fontannaz" not in d.chemin.name

    @pytest.mark.parametrize("nom", ["notes.txt", "photo.png", "archive.zip",
                                     "sans_extension"])
    def test_les_formats_non_geres_sont_refuses(self, atelier, nom):
        with pytest.raises(ValueError, match="non pris en charge"):
            deposer(atelier, nom, io.BytesIO(b"x" * 5), 5)

    def test_le_doc_ancien_est_refuse_avec_la_marche_a_suivre(self, atelier):
        with pytest.raises(ValueError, match="enregistrez le document en .docx"):
            deposer(atelier, "vieux.doc", io.BytesIO(b"x" * 5), 5)

    def test_un_fichier_vide_est_refuse(self, atelier):
        with pytest.raises(ValueError, match="vide"):
            deposer(atelier, "vide.pdf", io.BytesIO(b""), 0)

    def test_un_fichier_demesure_est_refuse(self, atelier):
        with pytest.raises(ValueError, match="trop volumineux"):
            deposer(atelier, "enorme.pdf", io.BytesIO(b""),
                    interface.TAILLE_MAX + 1)

    def test_un_televersement_tronque_ne_laisse_rien(self, atelier):
        """Annonce 100 octets, en envoie 10 : rien ne doit rester sur le disque."""
        with pytest.raises(ValueError, match="interrompu"):
            deposer(atelier, "coupe.pdf", io.BytesIO(b"x" * 10), 100)
        assert list(atelier.entrees().iterdir()) == []


class TestServeur:
    """Le serveur reprend les controles d'acces de la revue."""

    def _lancer(self):
        adresse: dict = {}
        prete = threading.Event()
        arret = threading.Event()

        def pret(url):
            adresse["url"] = url
            prete.set()

        fil = threading.Thread(
            target=lambda: interface.servir(ouvrir=False, pret=pret,
                                            arret=arret),
            daemon=True)
        fil.start()
        assert prete.wait(5), "le serveur n'a pas demarre"
        return adresse["url"], arret, fil

    def _base(self, url):
        a = urllib.parse.urlparse(url)
        return f"{a.scheme}://{a.netloc}", urllib.parse.parse_qs(a.query)["jeton"][0]

    def test_la_page_saffiche_avec_le_jeton(self):
        url, arret, fil = self._lancer()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                corps = r.read().decode("utf-8")
            assert r.status == 200
            assert 'id="zone"' in corps
            assert "Caviardoc" in corps
        finally:
            arret.set(); fil.join(timeout=5)

    def test_sans_jeton_la_page_est_refusee(self):
        url, arret, fil = self._lancer()
        try:
            base, _ = self._base(url)
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/", timeout=5)
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

    def test_televerser_sans_jeton_est_refuse(self):
        url, arret, fil = self._lancer()
        try:
            base, _ = self._base(url)
            req = urllib.request.Request(
                base + "/televerser", data=b"%PDF-1.4",
                headers={interface.ENTETE_NOM: "x.pdf"}, method="POST")
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 403
        finally:
            arret.set(); fil.join(timeout=5)

    def test_le_cycle_de_depot_rend_un_identifiant(self):
        url, arret, fil = self._lancer()
        try:
            base, jeton = self._base(url)
            req = urllib.request.Request(
                base + "/televerser", data=b"%PDF-1.4 essai",
                headers={ENTETE_JETON: jeton,
                         interface.ENTETE_NOM: urllib.parse.quote("essai.pdf")},
                method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                charge = json.loads(r.read())
            assert charge["nom"] == "essai.pdf"
            assert len(charge["id"]) == 16
        finally:
            arret.set(); fil.join(timeout=5)

    def test_un_format_refuse_le_dit(self):
        url, arret, fil = self._lancer()
        try:
            base, jeton = self._base(url)
            req = urllib.request.Request(
                base + "/televerser", data=b"du texte",
                headers={ENTETE_JETON: jeton,
                         interface.ENTETE_NOM: urllib.parse.quote("notes.txt")},
                method="POST")
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 400
            assert "non pris en charge" in e.value.read().decode("utf-8")
        finally:
            arret.set(); fil.join(timeout=5)

    def test_un_identifiant_invente_ne_telecharge_rien(self):
        """L'identifiant est une clef, jamais un chemin."""
        url, arret, fil = self._lancer()
        try:
            base, jeton = self._base(url)
            for voulu in ("../../../etc/passwd", "inconnu", ""):
                cible = (f"{base}/telecharger?jeton={urllib.parse.quote(jeton)}"
                         f"&id={urllib.parse.quote(voulu, safe='')}")
                with pytest.raises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(cible, timeout=5)
                assert e.value.code == 404
        finally:
            arret.set(); fil.join(timeout=5)

    def test_le_repertoire_de_travail_est_efface_a_larret(self, tmp_path,
                                                          monkeypatch):
        """Les originaux portent des donnees de patients.

        Dans un dossier de donnees isole : sinon le test cherchait `interface-*`
        dans celui de l'utilisateur, y trouvait l'atelier de sa console ouverte
        et s'etonnait qu'il existe encore apres l'arret du serveur de test. Il
        ne passait que si aucune console reelle n'etait ouverte.
        """
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        url, arret, fil = self._lancer()
        base, jeton = self._base(url)
        req = urllib.request.Request(
            base + "/televerser", data=b"%PDF-1.4 essai",
            headers={ENTETE_JETON: jeton,
                     interface.ENTETE_NOM: urllib.parse.quote("essai.pdf")},
            method="POST")
        urllib.request.urlopen(req, timeout=5).close()

        from caviardoc import paths
        restes = list(paths.cache_dir().glob("interface-*"))
        assert restes, "le repertoire de travail devrait exister pendant la session"

        arret.set(); fil.join(timeout=5)
        assert not any(d.exists() for d in restes)


class TestNettoyage:
    """Les originaux televerses sont des documents de patients : rien ne doit
    leur survivre, meme quand la session se termine mal."""

    def test_le_balayage_efface_les_restes(self, tmp_path, monkeypatch):
        """Un `kill`, un plantage ou une coupure de courant laissent le
        repertoire de travail en place : le demarrage suivant le balaie."""
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        from caviardoc import paths
        paths.ensure_dirs()

        abandonne = paths.cache_dir() / f"{interface.PREFIXE_ATELIER}abcdef"
        (abandonne / "entrees").mkdir(parents=True)
        (abandonne / "entrees" / "dossier.pdf").write_bytes(b"%PDF patient")

        assert interface.balayer_ateliers_abandonnes() == [abandonne]
        assert not abandonne.exists()

    def test_le_balayage_ne_touche_pas_au_reste_du_cache(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        from caviardoc import paths
        paths.ensure_dirs()

        garde = paths.cache_dir() / "telechargements"
        garde.mkdir()
        (garde / "partiel.bin").write_bytes(b"x")

        interface.balayer_ateliers_abandonnes()
        assert (garde / "partiel.bin").exists()

    def test_le_balayage_epargne_une_session_vivante(self, tmp_path,
                                                     monkeypatch):
        """Un second lancement ne doit pas effacer les documents que la
        premiere fenetre, toujours ouverte, est en train de traiter. Une
        session morte, elle, a lache son verrou et se balaie."""
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        from caviardoc import paths
        paths.ensure_dirs()

        vivante = paths.cache_dir() / f"{interface.PREFIXE_ATELIER}vivante"
        morte = paths.cache_dir() / f"{interface.PREFIXE_ATELIER}morte"
        for atelier in (vivante, morte):
            (atelier / "entrees").mkdir(parents=True)
            (atelier / "entrees" / "dossier.pdf").write_bytes(b"%PDF patient")
        verrou = interface.tenir_atelier(vivante)
        interface.tenir_atelier(morte).close()
        try:
            assert interface.balayer_ateliers_abandonnes() == [morte]
            assert (vivante / "entrees" / "dossier.pdf").exists()
        finally:
            verrou.close()
        assert interface.balayer_ateliers_abandonnes() == [vivante]


class TestRelecture:
    """La relecture suspend le traitement avant qu'un pixel soit noirci.

    C'est la seule etape qui rattrape ce que la mesure annonce comme manquant.
    Elle doit donc suspendre pour de bon, et obeir : un masque retire ne doit
    pas etre pose.
    """

    def _lancer(self):
        return TestServeur._lancer(TestServeur())

    def _client(self, url):
        a = urllib.parse.urlparse(url)
        return f"{a.scheme}://{a.netloc}", urllib.parse.parse_qs(a.query)["jeton"][0]

    def _post(self, base, jeton, chemin, charge):
        req = urllib.request.Request(
            base + chemin, data=json.dumps(charge).encode(), method="POST",
            headers={ENTETE_JETON: jeton, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")

    def _get(self, base, jeton, chemin):
        req = urllib.request.Request(base + chemin,
                                     headers={ENTETE_JETON: jeton})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _deposer(self, base, jeton, nom, octets):
        req = urllib.request.Request(
            base + "/televerser", data=octets, method="POST",
            headers={ENTETE_JETON: jeton,
                     interface.ENTETE_NOM: urllib.parse.quote(nom),
                     "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _attendre(self, base, jeton, idt, etats, delai=60):
        import time
        limite = time.time() + delai
        while time.time() < limite:
            t = self._get(base, jeton, "/tache?id=" + idt)
            if t["etat"] in etats:
                return t
            time.sleep(0.05)
        raise AssertionError(f"delai depasse, etat {t['etat']}")

    def _docx_avs(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_docx import AVS, _docx, _p, _t
        return AVS, _docx(_p(_t("Assure "), _t(AVS), _t(" ici.")))

    def test_le_traitement_sarrete_pour_la_relecture(self, monkeypatch):
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            _, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": True})

            t = self._attendre(base, jeton, tache["id"],
                               ("revue", "fini", "echec"))
            assert t["etat"] == "revue", t
            assert 0 < t["progression"] < 1

            vue = self._get(base, jeton, "/revue?id=" + tache["id"])
            assert vue["parties"], "la relecture doit montrer les parties"
            assert "<mark" in vue["parties"][0]["html"]
        finally:
            arret.set(); fil.join(timeout=10)

    def test_un_masque_retire_nest_pas_pose(self, monkeypatch):
        import re
        import zipfile, io
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            AVS, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": True})
            self._attendre(base, jeton, tache["id"], ("revue",))

            vue = self._get(base, jeton, "/revue?id=" + tache["id"])
            ids = re.findall(r'data-id="([^"]+)"', vue["parties"][0]["html"])
            assert ids, "il doit y avoir au moins une detection"

            self._post(base, jeton, "/revue/valider",
                       {"tache": tache["id"],
                        "decisions": {i: {"etat": "rejetee", "motif": "essai"}
                                      for i in ids}})
            t = self._attendre(base, jeton, tache["id"], ("fini", "echec"))
            assert t["etat"] == "fini", t

            produit = t["resultat"]["produits"][0]
            cible = (f"{base}/telecharger?jeton={urllib.parse.quote(jeton)}"
                     f"&id={produit['id']}")
            with urllib.request.urlopen(cible, timeout=30) as r:
                octets_produits = r.read()
            with zipfile.ZipFile(io.BytesIO(octets_produits)) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            assert AVS in xml, "le masque retire ne devait pas etre pose"
            assert "[NUMERO_AVS_1]" not in xml
        finally:
            arret.set(); fil.join(timeout=10)

    def test_doutes_seulement_ne_demande_rien_sans_doute(self, monkeypatch):
        """Une detection par regle n'est pas un doute : le document passe seul."""
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            _, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": "doutes"})
            t = self._attendre(base, jeton, tache["id"], ("revue", "fini", "echec"))
            assert t["etat"] == "fini", t
        finally:
            arret.set(); fil.join(timeout=10)

    def test_sans_relecture_le_masque_est_pose(self, monkeypatch):
        import zipfile, io
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            AVS, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": False})
            t = self._attendre(base, jeton, tache["id"], ("fini", "echec"))
            assert t["etat"] == "fini", t

            produit = t["resultat"]["produits"][0]
            cible = (f"{base}/telecharger?jeton={urllib.parse.quote(jeton)}"
                     f"&id={produit['id']}"
                     "&confirme=1")   # non relu : confirmation explicite
            with urllib.request.urlopen(cible, timeout=30) as r:
                octets_produits = r.read()
            with zipfile.ZipFile(io.BytesIO(octets_produits)) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            assert AVS not in xml
        finally:
            arret.set(); fil.join(timeout=10)

    def test_valider_deux_fois_est_refuse(self, monkeypatch):
        """La seconde validation arrive apres la reprise : elle n'a plus de sens."""
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            _, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": True})
            self._attendre(base, jeton, tache["id"], ("revue",))
            self._post(base, jeton, "/revue/valider",
                       {"tache": tache["id"], "decisions": {}})
            self._attendre(base, jeton, tache["id"], ("fini", "echec"))

            with pytest.raises(urllib.error.HTTPError) as e:
                self._post(base, jeton, "/revue/valider",
                           {"tache": tache["id"], "decisions": {}})
            assert e.value.code == 409
        finally:
            arret.set(); fil.join(timeout=10)

    def test_la_duree_exclut_lattente_du_relecteur(self, monkeypatch):
        """Sinon la page affiche le temps de reflexion comme du calcul."""
        import time
        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)

        url, arret, fil = self._lancer()
        try:
            base, jeton = self._client(url)
            _, octets = self._docx_avs()
            depot = self._deposer(base, jeton, "rapport.docx", octets)
            tache = self._post(base, jeton, "/traiter",
                               {"id": depot["id"], "revue": True})
            self._attendre(base, jeton, tache["id"], ("revue",))

            time.sleep(1.2)          # le relecteur reflechit
            self._post(base, jeton, "/revue/valider",
                       {"tache": tache["id"], "decisions": {}})
            t = self._attendre(base, jeton, tache["id"], ("fini", "echec"))

            assert t["etat"] == "fini", t
            assert t["resultat"]["duree"] < 1.0, \
                f"la duree {t['resultat']['duree']}s inclut l'attente"
        finally:
            arret.set(); fil.join(timeout=10)


class TestFormeDeLaPage:
    """La page d'accueil suit-elle le geste, ou l'inverse ?

    Mesure avant : 1485 px pour une fenetre de 720, le bouton « Caviarder » a
    663 px — pile sur la ligne de flottaison —, et « Ce que cette machine a
    retenu » occupant 42 % de la hauteur alors qu'on l'ouvre rarement. Le
    travail est pourtant cyclique : deposer, caviarder, relire, recommencer.

    Ces tests verrouillent la forme obtenue : ce qu'on repete est visible, ce
    qu'on consulte est repliable, et un repli dit ce qu'il cache.
    """

    def page(self) -> str:
        from caviardoc.interface import page_html
        return page_html("jeton-de-test", "gliner-multi-pii-fp16")

    def test_l_action_precede_ce_qu_on_consulte_rarement(self):
        page = self.page()
        assert page.index('id="lancer"') < page.index('id="memoire-repli"')

    def test_les_reglages_sont_replies(self):
        """Trois menus et quatre paragraphes s'intercalaient entre deposer et
        lancer, alors qu'on n'y touche qu'exceptionnellement."""
        page = self.page()
        assert '<details id="reglages">' in page
        assert "open" not in page[page.index('<details id="reglages"'):
                                  page.index("</summary>")]

    def test_la_memoire_est_repliee(self):
        page = self.page()
        assert '<details class="repli" id="memoire-repli">' in page

    def test_chaque_repli_annonce_ce_qu_il_cache(self):
        """Replier sans resumer reviendrait a cacher : on ne saurait plus avec
        quels reglages on s'apprete a caviarder."""
        page = self.page()
        assert 'id="resume"' in page
        assert 'id="resumeMemoire"' in page

    def test_le_resume_couvre_tous_les_reglages(self):
        """Un reglage absent du resume est un reglage qu'on modifie a l'aveugle."""
        from caviardoc.interface import _SCRIPT
        for cle in ("niveau", "dates", "format", "relecture"):
            assert cle in _SCRIPT[_SCRIPT.index("function majResume"):
                                  _SCRIPT.index("function majResume") + 900], cle


class TestConsole:
    """Une seule page, deux onglets, et l'utilisateur atterrit ou il faut.

    Avant : un fichier a double-cliquer pour installer, un autre pour
    travailler, et aucun des deux ne mentionnait l'existence de l'autre. Sur une
    machine neuve on ouvrait l'application, qui refusait de demarrer ; sur une
    machine prete on ouvrait l'installateur, qui ne servait plus a rien.
    """

    def console(self, initial: str = "app") -> str:
        from caviardoc.interface import page_console
        return page_console("jeton-de-test", "gliner-multi-pii-fp16", initial)

    def test_les_deux_onglets_sont_la(self):
        page = self.console()
        assert 'id="panneau-app"' in page
        assert 'id="panneau-inst"' in page
        assert 'data-cible="app"' in page and 'data-cible="inst"' in page
        # L'onglet dit ce que fait l'outil, comme le bouton : il caviarde.
        assert '<button data-cible="app">Caviarder</button>' in page
        assert ">Anonymiser<" not in page

    def test_l_onglet_installation_porte_les_trois_sujets(self):
        """« Installation », mais aussi la reprise et la mise a jour : les deux
        n'existaient qu'en ligne de commande, donc pas pour qui n'en ouvre pas."""
        page = self.console()
        panneau = page[page.index('id="panneau-inst"'):]
        assert 'id="etapes"' in panneau
        assert 'id="maj"' in panneau
        assert 'id="desinstallation"' in panneau

    def test_la_desinstallation_n_affiche_que_le_total_en_haut(self):
        page = self.console()
        i = page.index("async function chargerDesinstallation")
        corps = page[i:page.index("async function supprimer", i)]
        assert "a-supprimer'" not in corps.split("d.dehors")[0]
        assert corps.index('"Tout supprimer"') < corps.index('"Libère "')
        assert "Libère " in corps

    def test_la_memoire_explique_les_textes_anonymes_et_se_vide(self):
        page = self.console()
        assert "lors de sessions précédentes" in page
        assert "ne garde jamais en clair" in page
        assert "oublier({anonymes: true})" in page
        assert "oublier({tout: true})" in page

    def test_l_onglet_initial_est_impose_par_le_serveur(self):
        assert '"app"' in self.console("app")[
            self.console("app").index("ONGLET_INITIAL"):][:40]
        assert '"inst"' in self.console("inst")[
            self.console("inst").index("ONGLET_INITIAL"):][:40]

    def test_les_deux_scripts_ne_se_marchent_pas_dessus(self):
        """Les deux definissent `entetes` et `lancer` : sans portee separee, la
        derniere definition gagnerait en silence."""
        from caviardoc import installateur
        page = self.console()
        i = page.index("(function(){")
        assert page.index(installateur._SCRIPT.strip()[:60]) > i

    def test_un_seul_bouton_pour_passer_d_un_onglet_a_l_autre(self):
        """L'installateur a le sien ; dans la console la barre d'onglets suffit."""
        assert "#panneau-inst #ouvrir{display:none}" in self.console()

    def test_les_styles_des_etapes_sont_confines(self):
        """`.jauge` existe des deux cotes, avec des valeurs differentes."""
        from caviardoc import installateur
        confines = installateur.styles_confines("#panneau-inst")
        assert "#panneau-inst .jauge{" in confines
        # Aucune regle ne doit s'appliquer hors du panneau : chaque selecteur
        # commence par la portee.
        for regle in confines.split("}"):
            if "{" in regle:
                for selecteur in regle.split("{")[0].split(","):
                    selecteur = selecteur.strip()
                    if selecteur:
                        assert selecteur.startswith("#panneau-inst "), selecteur

    def test_la_page_seule_reste_servable(self):
        """`caviardoc interface` sert toujours la surface de travail seule."""
        from caviardoc.interface import page_html
        page = page_html("jeton", "modele")
        assert 'id="panneau-inst"' not in page
        assert 'id="zone"' in page


class TestRelectureDesDoutes:
    """Orange = estimation du modele. « Doutes seulement » ne montre que cela,
    et ne laisse jamais un document moins masque qu'une validation sans decision."""

    def parties(self, origine):
        from caviardoc.engine import Span
        texte = "Le patient VS est suivi."
        return [("corps", texte, [Span(11, 13, "personne", 0.3, "VS", origine)])]

    @pytest.mark.parametrize("valeur,attendu", [
        (True, "tout"), ("tout", "tout"), ("doutes", "doutes"),
        (False, None), (None, None), ("", None), ("aucune", None),
        ("inconnu", "tout")])
    def test_mode_relecture(self, valeur, attendu):
        assert interface.mode_relecture(valeur) == attendu

    def test_un_document_sans_doute_n_est_pas_soumis(self, atelier):
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache, seulement_doutes=True)
        spans = relire(self.parties("regle"))   # ne doit pas bloquer
        assert [(s.start, s.end) for s in spans["corps"]] == [(11, 13)]
        assert tache.etat != "revue"

    def test_un_doute_arrete_le_traitement(self, atelier):
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache, seulement_doutes=True)
        sortie = {}
        fil = threading.Thread(
            target=lambda: sortie.update(relire(self.parties("modele"))), daemon=True)
        fil.start()
        for _ in range(100):
            if tache.etat == "revue":
                break
            threading.Event().wait(0.05)
        assert tache.etat == "revue"
        assert tache.session.doutes
        tache.reprendre.set()
        fil.join(timeout=5)
        assert len(sortie["corps"]) == 1

    def test_la_vue_compte_les_doutes(self, atelier):
        from caviardoc.revue import preparer
        tache = interface.Tache("t", "d", "doc.docx")
        parties = self.parties("modele")
        tache.session = preparer("doc.docx", parties)
        tache.textes = {i: t for i, t, _ in parties}
        vue = interface.revue_en_json(tache)
        assert vue["doutes"] == 1 and vue["parties"][0]["doutes"] == 1

    def test_la_page_propose_les_trois_relectures_et_l_archive(self):
        from caviardoc.interface import page_console
        page = page_console("jeton", "gliner-multi-pii-fp16", "app")
        for valeur in ("tout", "doutes", "aucune"):
            assert f'name="relecture" value="{valeur}"' in page \
                or f'name="relecture"\n          value="{valeur}"' in page
        assert 'id="soloDoutes"' in page and 'id="douteSuivant"' in page
        assert 'id="validerConfiance"' in page and 'id="validerDoutes"' in page
        assert 'id="toutTelecharger"' in page

    def test_un_texte_ajoute_se_lit_pendant_la_session(self, atelier, tmp_path,
                                                       monkeypatch):
        """Ajoute a la main, il apparait en clair dans la memoire — en memoire
        vive : le fichier des corrections n'en garde que l'empreinte."""
        from caviardoc.exceptions import Repertoire
        from caviardoc.revue import ajouter
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))

        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache)
        texte = "Madame Fontannaz Yannick est suivie."
        fil = threading.Thread(
            target=lambda: relire([("corps", texte, [])]), daemon=True)
        fil.start()
        for _ in range(100):
            if tache.etat == "revue":
                break
            threading.Event().wait(0.05)
        ajouter(tache.session, "corps", texte, 7, 24)
        tache.reprendre.set()
        fil.join(timeout=5)

        vue = Repertoire.charger().vue(atelier.clairs)
        assert vue["toujours"][0]["chaine"] == "Fontannaz Yannick"
        assert "Fontannaz" not in Repertoire.chemin().read_text(encoding="utf-8")


class TestInterruption:
    """Le bouton « Interrompre » : arret propre, sans rien produire."""

    def test_interrompre_reveille_la_relecture_qui_s_arrete(self, atelier):
        from caviardoc.engine import Span
        from caviardoc.interruption import Interrompu
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache)
        texte = "Le patient VS est suivi."
        erreurs = []

        def courir():
            try:
                relire([("corps", texte, [Span(11, 13, "personne", 0.3, "VS", "modele")])])
            except Interrompu as exc:
                erreurs.append(exc)

        fil = threading.Thread(target=courir, daemon=True)
        fil.start()
        for _ in range(100):
            if tache.etat == "revue":
                break
            threading.Event().wait(0.05)
        assert tache.etat == "revue"
        tache.interrompre()
        fil.join(timeout=5)
        assert len(erreurs) == 1

    def test_la_tache_passe_a_interrompu_et_ne_laisse_aucune_sortie(
            self, atelier, monkeypatch):
        depot = interface.deposer(atelier, "a.docx", io.BytesIO(b"x"), 1, "")
        sortie = atelier.sorties() / depot.identifiant
        sortie.mkdir(parents=True)
        (sortie / "reste.pdf").write_bytes(b"partiel")

        def traiter(atelier_, identifiant, options, tache=None):
            tache.interrompre()
            tache.verifier_arret()

        monkeypatch.setattr(interface, "traiter", traiter)
        monkeypatch.setattr(type(atelier), "modele", lambda self: None)
        tache = interface.lancer(atelier, depot.identifiant, {})
        for _ in range(100):
            if tache.etat != "en_cours" and tache.etat != "en_attente":
                break
            threading.Event().wait(0.05)
        assert tache.etat == "interrompu"
        assert not sortie.exists()
        # Un document interrompu peut etre relance : il n'est pas perdu.
        assert depot.identifiant in atelier.depots

    def test_le_pdf_s_arrete_entre_deux_pages(self):
        from caviardoc.documents import caviardage
        from caviardoc.interruption import Interrompu
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        for _ in range(3):
            # Assez de mots pour que la couche texte soit lue sans Tesseract.
            page = doc.new_page()
            page.insert_text((72, 72), "Le controle annuel est sans "
                             "particularite, la tension arterielle est normale.")
            page.insert_text((72, 90), "Le traitement est poursuivi sans "
                             "changement et un nouveau controle est prevu.")
        octets = doc.tobytes()

        appels = []

        def arret():
            appels.append(1)
            if len(appels) == 2:
                raise Interrompu()

        import tempfile
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "a.pdf"
            source.write_bytes(octets)
            with pytest.raises(Interrompu):
                caviardage.caviarder_pdf(None, source, Path(d) / "s.pdf",
                                         arret=arret)
            assert not (Path(d) / "s.pdf").exists()

    def test_la_page_porte_les_deux_boutons_et_la_route(self):
        page = interface.page_html("jeton", "modele")
        assert 'id="interrompre"' in page
        assert 'id="interrompreRelecture"' in page
        assert "/interrompre" in page
