"""Les zones (blocs d'en-tete, logos) se montrent a la relecture et se refusent.

Releve sur une lettre reelle : au niveau complet, tout le bloc de l'expediteur
et tout celui du destinataire etaient noircis d'un seul rectangle, alors que
l'utilisateur, en relecture, n'avait jamais vu ces rectangles ni pu les refuser.
Les zones arrivent maintenant dans la relecture comme des elements d'un genre a
part, retenus par defaut ; seules celles qui restent retenues sont peintes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")

from caviardoc import revue, revue_web  # noqa: E402
from caviardoc.documents import caviardage  # noqa: E402
from caviardoc.documents.caviardage import caviarder_pdf  # noqa: E402
from caviardoc.documents.zones import OptionsZones  # noqa: E402
from caviardoc.engine import Span  # noqa: E402

ENTETE = ["Cabinet du Dr Machin",
          "Rue des Lilas 12",
          "1000 Lausanne",
          "Tel 021 555 12 34"]
CORPS = ["Echocardiographie dans les normes.",
         "Le ventricule gauche n'est pas dilate et sa fonction est conservee.",
         "Aucun epanchement pericardique n'est visible sur les coupes."]


def _lettre(chemin):
    d = pymupdf.open()
    page = d.new_page()
    for i, ligne in enumerate(ENTETE):
        page.insert_text((60, 60 + i * 14), ligne, fontsize=10)
    for i, ligne in enumerate(CORPS):
        page.insert_text((72, 300 + i * 18), ligne, fontsize=11)
    d.save(chemin)


def _relecture(refuser=lambda genre: False, vues=None):
    """Un crochet qui valide tout, sauf les zones dont `refuser(genre)`."""
    def relire(parties, zones=None):
        session = revue.preparer("lettre.pdf", parties, zones)
        if vues is not None:
            vues.append(session)
        decisions = {z.identifiant: {"etat": "rejetee"}
                     for z in session.zones if refuser(z.genre)}
        revue.decider(session, decisions)
        return revue.avec_zones(session, {
            p: revue.appliquer(session, p) for p, _, _ in parties})
    return relire


def _sortir(tmp_path, **kw):
    _lettre(tmp_path / "l.pdf")
    return caviarder_pdf(None, tmp_path / "l.pdf", tmp_path / "out.pdf",
                         zones=OptionsZones(entetes=True),
                         formats=frozenset({"pdf", "texte"}), **kw)


class TestSession:
    def test_les_zones_arrivent_retenues_et_distinctes(self):
        zones = {"page 1": [("entete", (10.0, 20.0, 200.0, 90.0), "Cabinet"),
                            ("logo", (5.0, 5.0, 50.0, 50.0), "")]}
        session = revue.preparer("d", [("page 1", "texte", [])], zones)
        assert [z.genre for z in session.zones] == ["entete", "logo"]
        assert all(z.masquee for z in session.zones)
        assert session.zones[0].etiquette == "zone : bloc d'en-tête"
        assert session.zones[1].etiquette == "zone : logo ou tampon"
        # Ni detection, ni doute : une zone n'est pas un passage du texte.
        assert session.detections == [] and session.doutes == []

    def test_refuser_retire_le_rectangle_et_accepter_le_garde(self):
        zones = {"page 1": [("entete", (10.0, 20.0, 200.0, 90.0), ""),
                            ("logo", (5.0, 5.0, 50.0, 50.0), "")]}
        session = revue.preparer("d", [("page 1", "t", [])], zones)
        assert len(revue.zones_retenues(session, "page 1")) == 2
        revue.decider(session, {"page 1:zone:1": {"etat": "rejetee"}})
        assert revue.zones_retenues(session, "page 1") == [(10.0, 20.0, 200.0, 90.0)]
        revue.decider(session, {"page 1:zone:1": {"etat": "retenue"}})
        assert len(revue.zones_retenues(session, "page 1")) == 2

    def test_la_session_enregistree_garde_ses_zones(self, tmp_path, monkeypatch):
        monkeypatch.setattr(revue, "dossier_sessions", lambda: tmp_path)
        session = revue.preparer("d", [("page 1", "t", [])],
                                 {"page 1": [("logo", (1, 2, 3, 4), "x")]})
        revue.decider(session, {"page 1:zone:0": {"etat": "rejetee"}})
        relue = revue.SessionRevue.charger(session.enregistrer())
        assert relue.zones == session.zones and not relue.zones[0].masquee

    def test_sans_zone_les_retenus_sont_rendus_tels_quels(self):
        session = revue.preparer("d", [("page 1", "t", [])])
        assert revue.avec_zones(session, {"page 1": []}) == {"page 1": []}

    def test_la_page_de_revue_ne_plante_pas_sur_une_session_a_zones(self):
        session = revue.preparer("d", [("page 1", "t", [])],
                                 {"page 1": [("logo", (1, 2, 3, 4), "")]})
        assert "page 1" in revue_web.page_html(session, {"page 1": "t"})


class TestPipeline:
    def test_la_zone_est_proposee_a_la_relecture(self, tmp_path):
        vues = []
        _sortir(tmp_path, revue=_relecture(vues=vues))
        genres = [z.genre for s in vues for z in s.zones]
        assert "entete" in genres
        assert all(z.boite[2] > z.boite[0] for s in vues for z in s.zones)

    def test_accepter_garde_le_rectangle_peint(self, tmp_path):
        rapport = _sortir(tmp_path, revue=_relecture())
        assert rapport.pages[0].zones >= 1
        texte = (tmp_path / "out.txt").read_text(encoding="utf-8").lower()
        assert "lilas" not in texte and "lausanne" not in texte

    def test_refuser_retire_le_rectangle_peint(self, tmp_path):
        accepte = _sortir(tmp_path, revue=_relecture())
        refuse = _sortir(tmp_path, revue=_relecture(lambda genre: genre == "entete"))
        assert refuse.pages[0].zones < accepte.pages[0].zones
        assert refuse.pages[0].boites < accepte.pages[0].boites
        # Le texte suit : un bloc refuse n'est plus retire de la sortie texte,
        # hormis ce que les detections masquent mot a mot.
        texte = (tmp_path / "out.txt").read_text(encoding="utf-8").lower()
        assert "lilas" in texte or "cabinet" in texte

    def test_sans_relecture_les_zones_sont_peintes_comme_avant(self, tmp_path):
        sans = _sortir(tmp_path)
        avec = _sortir(tmp_path, revue=_relecture())
        assert sans.pages[0].zones == avec.pages[0].zones >= 1
        assert sans.pages[0].boites == avec.pages[0].boites

    def test_une_relecture_qui_ignore_les_zones_ne_refuse_rien(self, tmp_path):
        """Un crochet a l'ancienne (parties seules, sans cle de zones)."""
        sans = _sortir(tmp_path)
        ancien = _sortir(tmp_path, revue=lambda parties, zones=None: {
            p: s for p, _, s in parties})
        assert ancien.pages[0].zones == sans.pages[0].zones

    def test_un_bloc_retenu_par_un_span_utilise_les_spans_apres_relecture(
            self, tmp_path, monkeypatch):
        """Les zones peintes se calculent sur les spans d'apres relecture."""
        recus = []
        reel = caviardage.boites_supplementaires

        def espion(page, lecture, spans, *a, **kw):
            recus.append(list(spans))
            return reel(page, lecture, spans, *a, **kw)

        monkeypatch.setattr(caviardage, "boites_supplementaires", espion)
        # L'utilisateur retire tous les spans.
        _sortir(tmp_path, revue=lambda parties, zones=None: {
            p: [] for p, _, _ in parties})
        assert recus == [[]]

    def test_les_spans_ajoutes_par_l_utilisateur_sont_ceux_vus_par_les_zones(
            self, tmp_path, monkeypatch):
        recus = []
        reel = caviardage.boites_supplementaires

        def espion(page, lecture, spans, *a, **kw):
            recus.append(list(spans))
            return reel(page, lecture, spans, *a, **kw)

        monkeypatch.setattr(caviardage, "boites_supplementaires", espion)
        ajout = Span(0, 7, "nom", 1.0, "Cabinet", "humain")
        _sortir(tmp_path, revue=lambda parties, zones=None: {
            p: [ajout] for p, _, _ in parties})
        assert recus == [[ajout]]
