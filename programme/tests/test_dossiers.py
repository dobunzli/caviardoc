"""Deposer un dossier entier : arborescence, retrait, archive.

Un dossier depose doit revenir comme il est parti — memes sous-dossiers,
memes fichiers, caviardes. Deux pieges en particulier : deux fichiers du meme
nom dans deux sous-dossiers ne doivent pas s'ecraser, et le chemin fourni par
le navigateur, comme le nom de fichier, ne doit jamais pouvoir sortir du
dossier d'extraction.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import interface  # noqa: E402
from caviardoc.interface import (Atelier, Production, archiver,  # noqa: E402
                               assainir_dossier, deposer, retirer)
from caviardoc.web_commun import ENTETE_JETON  # noqa: E402


@pytest.fixture
def atelier(tmp_path):
    return Atelier(racine=tmp_path)


class TestChemin:
    @pytest.mark.parametrize("brut, attendu", [
        ("Patients/Dupont Jean", "Patients/Dupont Jean"),
        ("../../etc", "etc"),
        ("/racine/absolue", "racine/absolue"),
        ("a\\b", "a/b"),
        ("a/./b/../c", "a/b/c"),
        ("C:/Users", "C_/Users"),
        ("x\x00y/z", "xy/z"),
        (" . / .. ", ""),
        ("", ""),
    ])
    def test_le_chemin_est_assaini(self, brut, attendu):
        assert assainir_dossier(brut) == attendu

    def test_le_depot_retient_son_dossier(self, atelier):
        d = deposer(atelier, "rapport.pdf", io.BytesIO(b"x" * 5), 5,
                    "Patients/../Dupont")
        assert d.dossier == "Patients/Dupont"
        # Le fichier sur le disque n'en depend pas.
        assert d.chemin.parent == atelier.entrees()

    def test_sans_dossier_rien_ne_change(self, atelier):
        d = deposer(atelier, "rapport.pdf", io.BytesIO(b"x" * 5), 5)
        assert d.dossier == ""


class TestRetrait:
    def test_retirer_efface_l_original(self, atelier):
        d = deposer(atelier, "rapport.pdf", io.BytesIO(b"x" * 5), 5)
        assert d.chemin.exists()
        retirer(atelier, d.identifiant)
        assert not d.chemin.exists()
        assert d.identifiant not in atelier.depots

    def test_un_identifiant_invente_est_refuse(self, atelier):
        with pytest.raises(KeyError):
            retirer(atelier, "invente")

    def test_un_document_en_cours_n_est_pas_retire(self, atelier):
        d = deposer(atelier, "rapport.pdf", io.BytesIO(b"x" * 5), 5)
        atelier.taches["t"] = interface.Tache(identifiant="t",
                                              depot=d.identifiant,
                                              nom=d.nom, etat="en_cours")
        with pytest.raises(RuntimeError):
            retirer(atelier, d.identifiant)
        assert d.chemin.exists()


class TestArchive:
    def _produire(self, atelier, nom, dossier, contenu=b"contenu",
                  a_verifier=False):
        chemin = atelier.sorties() / f"{len(atelier.productions)}" / nom
        chemin.parent.mkdir(parents=True)
        chemin.write_bytes(contenu)
        p = Production(f"id{len(atelier.productions)}", nom, chemin, dossier,
                       a_verifier=a_verifier)
        atelier.productions[p.identifiant] = p
        return p

    def _entrees(self, production):
        with zipfile.ZipFile(production.chemin) as z:
            return sorted(z.namelist())

    def test_l_arborescence_est_reconstruite(self, atelier):
        ids = [self._produire(atelier, "rapport.pdf", "Patients/A").identifiant,
               self._produire(atelier, "rapport.pdf", "Patients/B").identifiant,
               self._produire(atelier, "lettre.docx", "Patients").identifiant]
        archive = archiver(atelier, ids)
        assert self._entrees(archive) == ["Patients/A/rapport.pdf",
                                          "Patients/B/rapport.pdf",
                                          "Patients/lettre.docx"]
        assert archive.nom == "Patients - anonymisé.zip"

    def test_les_documents_douteux_vont_dans_un_dossier_en_tete(self, atelier):
        ids = [self._produire(atelier, "bon.pdf", "Patients").identifiant,
               self._produire(atelier, "muet.pdf", "Patients/A",
                              a_verifier=True).identifiant,
               self._produire(atelier, "autre.pdf", "", a_verifier=True).identifiant]
        archive = archiver(atelier, ids)
        entrees = self._entrees(archive)
        assert entrees == ["0_A_Verifier/Patients/A/muet.pdf",
                           "0_A_Verifier/autre.pdf",
                           "Patients/bon.pdf"]
        assert entrees[0].startswith("0_A_Verifier/")

    @pytest.mark.parametrize("alerte", [
        {"fuites": [{"identifiante": True}]},
        {"fuites": [{"identifiante": False}]},
        {"verifie": False},
        {"reserves": [{"page": 2, "reserve": "x"}]},
        {"manuscrit": [{"page": 1, "nombre": 2}]},
        {"images_opaques": [{"nom": "a.png", "raison": "illisible"}]},
        {"images_metadonnees": ["a.png"]},
        {"repli_complet": True}])
    def test_chaque_alerte_rend_le_document_a_verifier(self, alerte):
        propre = {"fuites": [], "verifie": True, "reserves": [],
                  "images_opaques": [], "images_metadonnees": [],
                  "repli_complet": False}
        assert interface.a_verifier(propre) is False
        assert interface.a_verifier({**propre, **alerte}) is True

    def test_sans_document_douteux_pas_de_dossier_a_verifier(self, atelier):
        p = self._produire(atelier, "bon.pdf", "")
        assert self._entrees(archiver(atelier, [p.identifiant])) == ["bon.pdf"]

    def test_les_noms_de_dossiers_sont_anonymises(self, atelier):
        atelier._table = SimpleNamespace(
            correspondances={"Dupont Jean": "PERSONNE_1"},
            formes_personnes=frozenset({"Dupont Jean"}))
        p = self._produire(atelier, "rapport.pdf", "Dupont Jean/Radiologie")
        archive = archiver(atelier, [p.identifiant])
        assert self._entrees(archive) == ["PERSONNE_1/Radiologie/rapport.pdf"]
        assert "Dupont" not in archive.nom

    def test_deux_homonymes_ne_s_ecrasent_pas(self, atelier):
        ids = [self._produire(atelier, "rapport.pdf", "", b"un").identifiant,
               self._produire(atelier, "rapport.pdf", "", b"deux").identifiant]
        archive = archiver(atelier, ids)
        assert self._entrees(archive) == ["rapport (2).pdf", "rapport.pdf"]
        assert archive.nom == "documents anonymisés.zip"

    def test_a_plat_tout_est_au_meme_niveau(self, atelier):
        ids = [self._produire(atelier, "rapport.pdf", "Patients/A", b"un").identifiant,
               self._produire(atelier, "rapport.pdf", "Patients/B", b"deux").identifiant,
               self._produire(atelier, "muet.pdf", "Patients/A",
                              a_verifier=True).identifiant]
        archive = archiver(atelier, ids, a_plat=True)
        assert self._entrees(archive) == ["0_A_Verifier_muet.pdf",
                                          "rapport (2).pdf", "rapport.pdf"]
        assert archive.nom == "Patients - anonymisé.zip"

    def test_nouvelle_demande_efface_tout(self, atelier):
        p = self._produire(atelier, "rapport.pdf", "Patients")
        depot = atelier.entrees() / "original.pdf"
        depot.write_bytes(b"secret")
        atelier.depots["d1"] = interface.Depot("d1", "original.pdf", depot, 6)
        atelier.clairs["x"] = "Dupont"
        atelier._table = SimpleNamespace(correspondances={"Dupont": "PERSONNE_1"},
                                       formes_personnes=frozenset({"Dupont"}))
        archive = archiver(atelier, [p.identifiant])
        interface.nouvelle_demande(atelier)
        assert not depot.exists() and not p.chemin.exists()
        assert not archive.chemin.exists()
        assert not atelier.depots and not atelier.productions
        assert not atelier.clairs and atelier._table is None

    def test_nouvelle_demande_refusee_pendant_un_traitement(self, atelier):
        atelier.taches["t"] = interface.Tache("t", "d", "x.pdf", etat="en_cours")
        with pytest.raises(RuntimeError):
            interface.nouvelle_demande(atelier)

    def test_rien_a_archiver_est_refuse(self, atelier):
        with pytest.raises(KeyError):
            archiver(atelier, ["invente"])


class TestDeBoutEnBout:
    """Deux fichiers du meme nom, dans deux sous-dossiers, par le serveur."""

    def _lancer(self):
        import threading
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
        a = urllib.parse.urlparse(adresse["url"])
        base = f"{a.scheme}://{a.netloc}"
        return base, urllib.parse.parse_qs(a.query)["jeton"][0], arret, fil

    def _requete(self, base, jeton, chemin, data=None, entetes=None):
        req = urllib.request.Request(
            base + chemin, data=data, method="POST" if data is not None else "GET",
            headers={ENTETE_JETON: jeton, **(entetes or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def _json(self, *a, **kw):
        return json.loads(self._requete(*a, **kw) or b"{}")

    def test_un_dossier_revient_avec_son_arborescence(self, monkeypatch):
        import time

        from caviardoc import engine
        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)
        # Machine mesuree et conforme : sans cela, tout document porte le
        # drapeau « sous le seuil » et part dans le dossier A verifier, ce
        # qui n'est pas l'objet de ce test (l'arborescence).
        from caviardoc import models
        models.noter_mesure(models.modele_actif(),
                            {"rappel_complet": 0.97, "rappel_patient": 0.97},
                            True)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_docx import AVS, _docx, _p, _t
        octets = _docx(_p(_t("Assure "), _t(AVS), _t(" ici.")))

        base, jeton, arret, fil = self._lancer()
        try:
            produits = []
            for dossier in ("Patients/A", "Patients/B"):
                depot = self._json(base, jeton, "/televerser", data=octets,
                                   entetes={interface.ENTETE_NOM: "rapport.docx",
                                            interface.ENTETE_DOSSIER:
                                            urllib.parse.quote(dossier),
                                            "Content-Type":
                                            "application/octet-stream"})
                assert depot["dossier"] == dossier
                tache = self._json(base, jeton, "/traiter",
                                   data=json.dumps({"id": depot["id"]}).encode(),
                                   entetes={"Content-Type": "application/json"})
                limite = time.time() + 60
                while time.time() < limite:
                    t = self._json(base, jeton, "/tache?id=" + tache["id"])
                    if t["etat"] in ("fini", "echec"):
                        break
                    time.sleep(0.05)
                assert t["etat"] == "fini", t
                assert t["resultat"]["dossier"] == dossier
                produits += [p["id"] for p in t["resultat"]["produits"]]

            archive = self._json(base, jeton, "/archive",
                                 data=json.dumps({"ids": produits,
                                                  "confirme": True}).encode(),
                                 entetes={"Content-Type": "application/json"})
            octets_zip = self._requete(
                base, jeton, "/telecharger?id=" + archive["id"])
            with zipfile.ZipFile(io.BytesIO(octets_zip)) as z:
                noms = sorted(z.namelist())
                assert noms == ["Patients/A/rapport.docx",
                                "Patients/B/rapport.docx"]
                for nom in noms:
                    with zipfile.ZipFile(io.BytesIO(z.read(nom))) as docx:
                        assert AVS not in docx.read("word/document.xml").decode()
        finally:
            arret.set()
            fil.join(timeout=10)

    def test_retirer_par_le_serveur(self):
        base, jeton, arret, fil = self._lancer()
        try:
            depot = self._json(base, jeton, "/televerser", data=b"%PDF-1.4 x",
                               entetes={interface.ENTETE_NOM: "a.pdf",
                                        "Content-Type": "application/octet-stream"})
            assert self._json(base, jeton, "/retirer",
                              data=json.dumps({"id": depot["id"]}).encode(),
                              entetes={"Content-Type": "application/json"})["retire"]
            with pytest.raises(urllib.error.HTTPError) as e:
                self._json(base, jeton, "/retirer",
                           data=json.dumps({"id": depot["id"]}).encode(),
                           entetes={"Content-Type": "application/json"})
            assert e.value.code == 404
        finally:
            arret.set()
            fil.join(timeout=10)


class TestPage:
    def test_on_peut_choisir_un_dossier(self):
        page = interface.page_html("jeton", "modele")
        assert 'id="choixDossier"' in page and "webkitdirectory" in page
        assert 'id="choisirFichiers"' in page and 'id="choisirDossier"' in page

    def test_la_relecture_n_existe_qu_une_fois(self):
        """Elle figurait deux fois dans la console : dans l'onglet, et hors
        des onglets. `getElementById` ne voyait que la premiere."""
        for page in (interface.page_html("jeton", "modele"),
                     interface.page_console("jeton", "modele", "app")):
            assert page.count('id="relecture"') == 1
            assert page.count('id="valider"') == 1

    def test_les_masquages_sont_reunis(self):
        """Personnes et dates se reglent ensemble, sous « Ce qui est masque »."""
        page = interface.page_html("jeton", "modele")
        groupe = page[page.index("Ce qui est masqué"):
                      page.index("Ce que vous obtenez")]
        for champ in ('name="niveau"', 'name="dates"'):
            assert champ in groupe, champ

    def test_la_page_dit_que_les_metadonnees_partent(self):
        """Ce qu'on ne voit pas, on ne pense pas a le verifier : la page doit
        dire que les metadonnees sont retirees. Une phrase, sans la liste :
        le bilan de chaque document dit ensuite lesquelles."""
        page = " ".join(interface.page_html("jeton", "modele").split())
        groupe = page[page.index("Ce qui est masqué"):
                      page.index("Ce que vous obtenez")]
        assert "Métadonnées · toujours retirées" in groupe
        assert "Ce qu'un document porte sans l'afficher." in groupe

    def test_images_et_entetes_suivent_le_niveau(self):
        """Plus d'interrupteur : « Toutes les personnes » retire logos et
        en-tetes, « Le patient seulement » les garde. Deux reglages de moins,
        et plus de combinaison incoherente a expliquer."""
        page = " ".join(interface.page_html("jeton", "modele").split())
        for champ in ("logos", "entetes"):
            assert f'id="{champ}"' not in page, champ
        assert 'id="usage"' not in page
        assert "Images et logos" not in page

    def test_le_niveau_decide_des_logos_et_des_entetes(self):
        from caviardoc.roles import Niveau
        complet = interface.zones_du_niveau({}, Niveau.COMPLET)
        assert (complet.logos, complet.entetes) == (True, True)
        patient = interface.zones_du_niveau({}, Niveau.PATIENT)
        assert (patient.logos, patient.entetes) == (False, False)
        # Une requete qui les fixe (script, ancienne page) reste obeie.
        force = interface.zones_du_niveau({"logos": False}, Niveau.COMPLET)
        assert (force.logos, force.entetes) == (False, True)

    def test_le_texte_se_dit_pseudonymise(self):
        # « Anonymisé » promettait ce que l'outil ne tient pas : le recit
        # clinique reste lisible et peut suffire a retrouver la personne.
        page = " ".join(interface.page_html("jeton", "modele").split())
        assert "pseudonymes" not in page
        assert "<span>Texte pseudonymisé</span>" in page
        assert "Texte anonymisé" not in page
