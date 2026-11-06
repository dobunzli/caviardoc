"""Relecture humaine : le defaut doit toujours etre de masquer.

Aucun modele ne detecte tout, et un transfert vers un service en nuage est
irreversible. La relecture est la seule etape qui rattrape ce que la mesure
annonce comme manquant — mais elle ne doit jamais, par inattention ou par
abandon, produire un document moins masque qu'une execution sans elle.
"""

import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.revue import (Etat, SessionRevue, ajouter, appliquer,  # noqa: E402
                           decider, preparer)
from caviardoc.revue_web import ENTETE_JETON, page_html, servir  # noqa: E402

TEXTE = "Monsieur Zorglub, ne le 23.04.1931, suivi par le Dr Mercier."


def _session():
    spans = [Span(9, 16, "personne", 0.9, "Zorglub"),
             Span(24, 34, "date", 1.0, "23.04.1931", Origine.REGLE.value)]
    return preparer("essai.pdf", [("corps", TEXTE, spans)])


class TestDefautMasquer:
    def test_sans_decision_tout_reste_masque(self):
        s = _session()
        assert len(appliquer(s, "corps")) == 2

    def test_une_decision_inconnue_ne_perd_pas_les_autres(self):
        """Une session regeneree entre l'affichage et l'envoi ne doit pas
        faire perdre les decisions valides."""
        s = _session()
        decider(s, {"inexistant:9": {"etat": "rejetee"},
                    "corps:0": {"etat": "rejetee", "motif": "faux positif"}})
        assert [d.text for d in appliquer(s, "corps")] == ["23.04.1931"]

    def test_un_etat_invalide_est_ignore(self):
        s = _session()
        decider(s, {"corps:0": {"etat": "supprimee"}})
        assert len(appliquer(s, "corps")) == 2, "un etat inconnu ne decouvre rien"

    def test_le_motif_est_conserve(self):
        s = _session()
        decider(s, {"corps:0": {"etat": "rejetee", "motif": "nom de rue"}})
        assert s.rejetees[0].motif == "nom de rue"


class TestAjoutManuel:
    def test_ajout_dune_entite_manquee(self):
        s = _session()
        d = ajouter(s, "corps", TEXTE, 52, 59)
        assert d.texte == "Mercier"
        assert d.etat == Etat.AJOUTEE.value
        assert len(appliquer(s, "corps")) == 3

    def test_intervalle_hors_du_texte_refuse(self):
        s = _session()
        with pytest.raises(ValueError):
            ajouter(s, "corps", TEXTE, 5, 9999)

    def test_intervalle_vide_refuse(self):
        with pytest.raises(ValueError):
            ajouter(_session(), "corps", TEXTE, 7, 7)


class TestSessionLocale:
    def test_la_session_vit_hors_du_depot(self):
        """Elle porte les entites en clair : jamais dans le depot versionne."""
        from caviardoc.paths import is_inside_synced_folder
        assert not is_inside_synced_folder(_session().chemin())

    def test_aller_retour_sur_disque(self, tmp_path):
        s = _session()
        decider(s, {"corps:0": {"etat": "rejetee", "motif": "essai"}})
        c = tmp_path / "s.json"
        c.write_text(json.dumps({**{k: v for k, v in s.__dict__.items()
                                    if k != "detections"},
                                 "detections": [d.__dict__ for d in s.detections]},
                                ensure_ascii=False), encoding="utf-8")
        relue = SessionRevue.charger(c)
        assert len(relue.rejetees) == 1
        assert relue.rejetees[0].motif == "essai"


class TestPage:
    def test_les_entites_sont_surlignees(self):
        h = page_html(_session(), {"corps": TEXTE})
        assert h.count("<mark") == 2
        assert "Zorglub" in h

    def test_le_html_du_document_est_echappe(self):
        """Un document malveillant ne doit pas injecter de script."""
        s = preparer("x", [("corps", "<script>alert(1)</script>", [])])
        h = page_html(s, {"corps": "<script>alert(1)</script>"})
        assert "<script>alert(1)</script>" not in h
        assert "&lt;script&gt;" in h

    def test_lorigine_est_visible(self):
        h = page_html(_session(), {"corps": TEXTE})
        assert 'class="regle"' in h and 'class="modele"' in h


class TestServeur:
    """Le serveur est le seul point du module qui touche au reseau."""

    def _lancer(self, session, textes):
        adresse: dict = {}
        prete = threading.Event()

        def pret(url):
            adresse["url"] = url
            prete.set()

        fil = threading.Thread(
            target=lambda: servir(session, textes, ouvrir=False, pret=pret),
            daemon=True)
        fil.start()
        assert prete.wait(5), "le serveur n'a pas demarre"
        return adresse["url"], fil

    def _poster(self, url, chemin, charge, jeton=None):
        """Poste sur le serveur, avec le jeton porte par l'adresse de session."""
        adresse = urllib.parse.urlparse(url)
        if jeton is None:
            jeton = urllib.parse.parse_qs(adresse.query)["jeton"][0]
        req = urllib.request.Request(
            f"{adresse.scheme}://{adresse.netloc}{chemin}",
            data=json.dumps(charge).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     ENTETE_JETON: jeton},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def test_cycle_complet(self):
        """Sert la page, envoie un rejet, verifie qu'il s'applique et que le
        serveur s'arrete."""
        s = _session()
        url, fil = self._lancer(s, {"corps": TEXTE})

        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.status == 200
            assert "<mark" in r.read().decode("utf-8")

        assert self._poster(url, "/decisions",
                            {"corps:0": {"etat": "rejetee", "motif": "essai"}}) == 200
        fil.join(timeout=5)
        assert not fil.is_alive(), "le serveur doit s'arreter apres validation"
        assert [d.text for d in appliquer(s, "corps")] == ["23.04.1931"]

    def test_ajout_par_le_reseau(self):
        s = _session()
        url, fil = self._lancer(s, {"corps": TEXTE})
        assert self._poster(url, "/ajout",
                            {"partie": "corps", "debut": 52, "fin": 59}) == 200
        assert len(appliquer(s, "corps")) == 3
        self._poster(url, "/decisions", {})
        fil.join(timeout=5)

    def test_un_ajout_hors_du_texte_est_refuse(self):
        s = _session()
        url, fil = self._lancer(s, {"corps": TEXTE})
        with pytest.raises(urllib.error.HTTPError) as e:
            self._poster(url, "/ajout",
                         {"partie": "corps", "debut": 0, "fin": 99999})
        assert e.value.code == 400
        self._poster(url, "/decisions", {})
        fil.join(timeout=5)


class TestAcces:
    """La page affiche un dossier patient en clair : ecouter sur la boucle
    locale ne suffit pas, tout processus de la machine peut y frapper."""

    def _lancer(self):
        return TestServeur._lancer(TestServeur(), _session(), {"corps": TEXTE})

    def _sans_jeton(self, url):
        adresse = urllib.parse.urlparse(url)
        return f"{adresse.scheme}://{adresse.netloc}/"

    def test_la_page_exige_le_jeton(self):
        url, fil = self._lancer()
        try:
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self._sans_jeton(url), timeout=5)
            assert e.value.code == 403
        finally:
            self._poster_fin(url, fil)

    def test_un_mauvais_jeton_ne_passe_pas(self):
        url, fil = self._lancer()
        try:
            adresse = urllib.parse.urlparse(url)
            faux = f"{adresse.scheme}://{adresse.netloc}/?jeton=devine"
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(faux, timeout=5)
            assert e.value.code == 403
        finally:
            self._poster_fin(url, fil)

    def test_ecrire_sans_jeton_est_refuse(self):
        """Le jeton voyage en en-tete : une page tierce ne peut pas le forger."""
        url, fil = self._lancer()
        try:
            with pytest.raises(urllib.error.HTTPError) as e:
                TestServeur()._poster(url, "/decisions", {}, jeton="")
            assert e.value.code == 403
        finally:
            self._poster_fin(url, fil)

    def test_un_hote_etranger_est_refuse(self):
        """Defense contre le « DNS rebinding » : une page tierce peut faire
        pointer son domaine vers 127.0.0.1, mais l'en-tete Host la trahit."""
        url, fil = self._lancer()
        try:
            adresse = urllib.parse.urlparse(url)
            req = urllib.request.Request(url)
            req.add_header("Host", "attaquant.example")
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 403
            assert adresse.hostname == "127.0.0.1"
        finally:
            self._poster_fin(url, fil)

    def test_le_jeton_change_a_chaque_session(self):
        url1, fil1 = self._lancer()
        jeton1 = urllib.parse.parse_qs(urllib.parse.urlparse(url1).query)["jeton"][0]
        self._poster_fin(url1, fil1)
        url2, fil2 = self._lancer()
        jeton2 = urllib.parse.parse_qs(urllib.parse.urlparse(url2).query)["jeton"][0]
        self._poster_fin(url2, fil2)
        assert jeton1 != jeton2
        assert len(jeton1) >= 32

    def _poster_fin(self, url, fil):
        try:
            TestServeur()._poster(url, "/decisions", {})
        except urllib.error.HTTPError:
            pass
        fil.join(timeout=5)
