"""Signature, mentions legales, licences, et gestion du modele.

L'AGPL demande que toute interface interactive affiche l'auteur, la licence,
l'absence de garantie et le moyen de lire le texte de la licence ; et que le
code source soit offert a qui utilise le programme. Le modele, lui, pese
plusieurs centaines de megaoctets : on doit pouvoir le supprimer, et savoir
avant de l'installer s'il convient a la machine.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import installateur, interface, licences, models  # noqa: E402
from caviardoc.web_commun import ENTETE_JETON  # noqa: E402

GO = 1024 ** 3
FP16 = models.GLINER_MULTI_PII_FP16.key
DEFAUT = models.DEFAULT_MODEL
FAUX = "gliner-test-deconseille"


@pytest.fixture
def modele_deconseille(monkeypatch):
    """Un modele deconseille, pour exercer le mecanisme.

    Le registre n'en porte plus : int8, le seul, a ete retire — il n'atteignait
    le seuil de 90 % de rappel sur aucune machine mesuree. Le mecanisme, lui,
    reste : un futur modele douteux doit pouvoir etre propose avec avertissement.
    """
    spec = models.ModelSpec(
        key=FAUX, repo="exemple/modele", revision="main", license="Apache-2.0",
        description="Modele de test", memoire=GO, rappel_reference=50.0,
        deconseille="Pas assez fiable.",
        files=(models.RemoteFile("onnx/model_test.onnx", "0" * 64, 1000),))
    monkeypatch.setitem(models.REGISTRY, FAUX, spec)
    monkeypatch.setitem(models.FICHIER_ONNX, FAUX, "onnx/model_test.onnx")
    return spec


class TestPages:
    def _pages(self):
        return {"interface": interface.page_html("j", FP16),
                "console": interface.page_console("j", FP16, "app"),
                "installateur": installateur.page_html("j")}

    def test_l_auteur_signe_chaque_page(self):
        for nom, page in self._pages().items():
            assert "par D. Bünzli" in page, nom

    def test_chaque_page_affiche_la_version_dans_le_pied(self):
        from caviardoc import __version__, doctor
        for nom, page in self._pages().items():
            pied = page[page.index('<footer class="pied">'):]
            assert f"Caviardoc {__version__}" in pied, nom
        assert doctor.check_version().detail == __version__

    def test_le_modele_n_est_plus_dans_l_en_tete(self):
        page = interface.page_html("j", FP16)
        entete = page[page.index("<header"):page.index("</header>")]
        assert FP16 not in entete and "modèle" not in entete

    def test_chaque_page_porte_les_mentions_legales(self):
        """Article 5 d de l'AGPL : auteur, licence, absence de garantie."""
        for nom, page in self._pages().items():
            pied = page[page.index('<footer class="pied">'):]
            for mention in ("© 2026 D. Bünzli", "AGPL-3.0", "sans aucune garantie",
                            "/licence?jeton=j", licences.DEPOT_SOURCE):
                assert mention in pied, (nom, mention)

    def test_la_console_detaille_les_licences(self):
        page = interface.page_console("j", FP16, "app")
        section = page[page.index('<section id="licences">'):]
        section = section[:section.index("</section>")]
        assert "PyMuPDF" in section and "AGPL-3.0" in section
        assert "Apache-2.0" in section and "GLiNER multi PII v1" in section
        assert "sans aucune garantie" in section

    def test_chaque_composant_a_sa_licence_et_son_lien(self):
        for c in licences.COMPOSANTS:
            assert c.licence and c.lien.startswith("https://"), c.nom


class TestVueModeles:
    @pytest.fixture(autouse=True)
    def _donnees(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))

    def _par_cle(self, ram):
        return {m["cle"]: m for m in interface.vue_modeles(ram)["modeles"]}

    def test_le_defaut_est_conseille_sur_une_machine_confortable(self):
        vue = self._par_cle(16 * GO)
        assert vue[DEFAUT]["avis"] == "conseille" and vue[DEFAUT]["actif"]
        assert vue[DEFAUT]["taille"] == models.REGISTRY[DEFAUT].total_size

    def test_le_registre_ne_porte_plus_de_modele_deconseille(self):
        """int8 en a ete retire : 62 % de rappel sur arm64, 51 % sur x86, jamais
        90 %. Un modele propose doit tenir le seuil."""
        assert not [s for s in models.REGISTRY.values() if s.deconseille]
        assert not any("int8" in cle for cle in models.REGISTRY)

    def test_un_modele_deconseille_le_reste_quelle_que_soit_la_machine(
            self, modele_deconseille):
        for ram in (2 * GO, 64 * GO):
            assert self._par_cle(ram)[FAUX]["avis"] == "deconseille"

    def test_une_petite_machine_est_prevenue(self):
        vue = self._par_cle(3 * GO)
        assert vue[DEFAUT]["avis"] == "juste"
        assert "mémoire" in vue[DEFAUT]["raison"]

    def test_rien_n_est_installe_dans_un_repertoire_vide(self):
        vue = self._par_cle(16 * GO)
        assert not vue[DEFAUT]["installe"] and vue[DEFAUT]["sur_disque"] == 0


def test_supprimer_libere_la_place(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    spec = models.GLINER_MULTI_PII_FP32
    dossier = spec.local_dir()
    (dossier / "onnx").mkdir(parents=True)
    (dossier / "onnx" / "model.onnx").write_bytes(b"x" * 1000)
    (dossier / "tokenizer.json").write_bytes(b"y" * 24)
    assert models.taille_installee(spec) == 1024
    assert models.supprimer(spec) == 1024
    assert not dossier.exists()
    assert models.supprimer(spec) == 0          # deja parti : sans erreur


class TestServeur:
    @pytest.fixture
    def console(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        lance = []
        monkeypatch.setattr(installateur, "executer",
                            lambda inst, cle: lance.append(cle))
        adresse: dict = {}
        prete, arret = threading.Event(), threading.Event()

        def pret(url):
            adresse["url"] = url
            prete.set()

        fil = threading.Thread(target=lambda: interface.servir(
            ouvrir=False, pret=pret, arret=arret, console=True), daemon=True)
        fil.start()
        assert prete.wait(5)
        a = urllib.parse.urlparse(adresse["url"])
        yield (f"{a.scheme}://{a.netloc}",
               urllib.parse.parse_qs(a.query)["jeton"][0], lance)
        arret.set()
        fil.join(timeout=10)

    def _post(self, base, jeton, chemin, charge):
        req = urllib.request.Request(
            base + chemin, data=json.dumps(charge).encode(), method="POST",
            headers={ENTETE_JETON: jeton, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def test_la_licence_se_lit_avec_le_jeton(self, console):
        base, jeton, _ = console
        with urllib.request.urlopen(f"{base}/licence?jeton={jeton}", timeout=10) as r:
            assert b"GNU AFFERO GENERAL PUBLIC LICENSE" in r.read()
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/licence", timeout=10)
        assert e.value.code == 403

    def test_supprimer_un_modele(self, console):
        base, jeton, _ = console
        dossier = models.GLINER_MULTI_PII_FP32.local_dir()
        dossier.mkdir(parents=True)
        (dossier / "tokenizer.json").write_bytes(b"x" * 10)
        r = self._post(base, jeton, "/modeles/supprimer",
                       {"cle": models.GLINER_MULTI_PII_FP32.key})
        assert r["liberes"] == 10 and not dossier.exists()

    def test_un_modele_deconseille_ne_s_installe_pas_sans_confirmation(
            self, console, modele_deconseille):
        base, jeton, lance = console
        with pytest.raises(urllib.error.HTTPError) as e:
            self._post(base, jeton, "/modeles/installer", {"cle": FAUX})
        assert e.value.code == 409 and not lance

    def test_un_modele_deconseille_s_installe_une_fois_confirme(
            self, console, modele_deconseille):
        base, jeton, lance = console
        r = self._post(base, jeton, "/modeles/installer",
                       {"cle": FAUX, "confirme": True})
        assert r["lance"] and lance == ["modele"]

    def test_installer_lance_l_etape_du_modele(self, console):
        base, jeton, lance = console
        assert self._post(base, jeton, "/modeles/installer", {"cle": FP16})["lance"]
        assert lance == ["modele"]

    def test_un_modele_absent_ne_se_met_pas_en_service(self, console):
        base, jeton, _ = console
        with pytest.raises(urllib.error.HTTPError) as e:
            self._post(base, jeton, "/modeles/utiliser", {"cle": FP16})
        assert e.value.code == 409

    def test_une_cle_inventee_est_refusee(self, console):
        base, jeton, _ = console
        with pytest.raises(urllib.error.HTTPError) as e:
            self._post(base, jeton, "/modeles/supprimer", {"cle": "../../etc"})
        assert e.value.code == 404
