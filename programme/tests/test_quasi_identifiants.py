"""Quasi-identifiants : signales a la relecture, jamais masques d'office.

Un extrait d'arret caviarde, sans un seul nom — un cycliste, le Giro 2022, une
blessure a la cuisse —, a suffi a une IA pour retrouver la personne. Le modele
ne cherchait ni la profession, ni le sport, ni la nationalite, ni l'evenement,
ni l'age. Il les cherche desormais, dans une passe a part, et les montre « a
juger » : le medecin decide de les masquer.

Ces tests verrouillent trois choses. La classification : un quasi-identifiant
n'est pas un identifiant direct, et une etiquette inconnue ne passe pas pour
un quasi-identifiant. La relecture : un passage a juger arrive lisible, un clic
le masque, et le laisser lisible n'apprend rien a la machine. L'affichage : une
couleur a part, et la phrase qui dit pourquoi.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from caviardoc import interface, tesseract  # noqa: E402
from caviardoc.documents.fuites import Fuite  # noqa: E402
from caviardoc.engine import (DEFAULT_LABELS, QUASI_LABELS, SEUIL_QUASI,  # noqa: E402
                              Categorie, Span, est_contextuel, est_date,
                              est_identifiant_direct, est_personne,
                              est_quasi_identifiant)
from caviardoc.pipeline import analyse, quasi_identifiants  # noqa: E402
from caviardoc.revue import Etat, appliquer, decider, preparer  # noqa: E402
from caviardoc.revue_web import page_html, surligner  # noqa: E402

TEXTE = ("Monsieur Zorglub, cycliste professionnel, a chute lors du "
         "Giro 2022 a 31 ans.")


class FauxModele:
    """Rend, pour chaque etiquette demandee, les mots qu'on lui a confies.

    Il note les etiquettes de chaque appel : c'est ce qui montre que la passe
    des quasi-identifiants ne touche pas a celle qui masque.
    """

    def __init__(self, reponses: dict[str, list[tuple[str, float]]]):
        self.reponses = reponses
        self.appels: list[tuple[str, ...]] = []

    def predict_entities(self, texte, labels, threshold=0.0):
        self.appels.append(tuple(labels))
        trouves = []
        for label in labels:
            for mot, score in self.reponses.get(label, []):
                i = texte.find(mot)
                if i >= 0 and score >= threshold:
                    trouves.append({"start": i, "end": i + len(mot),
                                    "label": label, "score": score,
                                    "text": mot})
        return trouves


def _modele():
    return FauxModele({
        "personne": [("Zorglub", 0.95)],
        "profession": [("cycliste professionnel", 0.86),
                       ("Monsieur Zorglub", 0.5)],
        "evenement": [("Giro 2022", 0.97)],
        "age": [("31 ans", 0.9)],
        "nationalite ou origine": [("Monsieur", 0.2)],
    })


def _session():
    personne = Span(9, 18, "personne", 0.95, "Zorglub")
    profession = Span(20, 42, "profession", 0.86, "cycliste professionnel")
    return preparer("essai.pdf", [("corps", TEXTE, [personne, profession])])


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

class TestClassification:
    @pytest.mark.parametrize("etiquette", QUASI_LABELS)
    def test_un_quasi_identifiant_situe_sans_designer(self, etiquette):
        assert est_quasi_identifiant(etiquette)
        assert est_contextuel(etiquette)
        assert not est_identifiant_direct(etiquette)

    def test_les_cinq_categories_demandees(self):
        assert set(QUASI_LABELS) == {
            Categorie.PROFESSION.value, Categorie.ACTIVITE.value,
            Categorie.NATIONALITE.value, Categorie.EVENEMENT.value,
            Categorie.AGE.value}

    @pytest.mark.parametrize("etiquette", [
        "personne", "date", "numero AVS", "page", "message", "ajout manuel",
        "garage", "", "etiquette inventee"])
    def test_le_reste_n_en_est_pas(self, etiquette):
        """Egalite stricte : « age » ne doit pas mordre dans « page »."""
        assert not est_quasi_identifiant(etiquette)

    def test_une_etiquette_inconnue_reste_grave(self):
        assert est_identifiant_direct("etiquette inventee")

    @pytest.mark.parametrize("etiquette", QUASI_LABELS)
    def test_aucun_ne_passe_pour_une_date_ou_une_personne(self, etiquette):
        """`est_date` cherche « date » dans l'etiquette : « evenement date »
        aurait ete masque comme une date, ou demasque par `--dates garder`."""
        assert not est_date(etiquette)
        assert not est_personne(etiquette)

    def test_la_passe_qui_masque_ne_change_pas(self):
        """Ajoutees a `DEFAULT_LABELS`, ces etiquettes disputeraient leurs mots
        aux identifiants directs."""
        assert not set(QUASI_LABELS) & set(DEFAULT_LABELS)

    def test_une_fuite_de_quasi_identifiant_est_un_element_de_contexte(self):
        fuite = Fuite("cycliste professionnel", "profession")
        assert not fuite.identifiante
        assert fuite.contextuelle


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

class TestDetection:
    def test_une_passe_a_part_avec_ses_seules_etiquettes(self):
        modele = _modele()
        masques = analyse(modele, TEXTE)
        appels_analyse = list(modele.appels)
        trouves = quasi_identifiants(modele, TEXTE, masques)

        assert all(not set(QUASI_LABELS) & set(a) for a in appels_analyse), \
            "l'analyse qui masque ne demande aucun quasi-identifiant"
        assert set(modele.appels[len(appels_analyse):]) == {QUASI_LABELS}
        assert [s.text for s in trouves] == [
            "cycliste professionnel", "Giro 2022", "31 ans"]
        assert all(est_quasi_identifiant(s.label) for s in trouves)

    def test_rien_n_est_masque_d_office(self):
        modele = _modele()
        masques = [s.text for s in analyse(modele, TEXTE)]
        assert "cycliste professionnel" not in masques
        assert "Giro 2022" not in masques

    def test_ce_qui_recoupe_un_masque_n_est_pas_montre(self):
        """« Monsieur Zorglub » contient un nom deja masque : il n'y a plus
        rien a juger a part."""
        modele = _modele()
        trouves = quasi_identifiants(modele, TEXTE, analyse(modele, TEXTE))
        assert all("Zorglub" not in s.text for s in trouves)

    def test_le_seuil_est_plus_haut_que_celui_des_masques(self):
        modele = _modele()
        trouves = quasi_identifiants(modele, TEXTE, [])
        assert SEUIL_QUASI > 0.2
        assert "Monsieur" not in [s.text for s in trouves]

    def test_sans_modele_rien(self):
        assert quasi_identifiants(None, TEXTE, []) == []

    @pytest.mark.parametrize("texte", [
        "Spécialiste en ophtalmologie", "Médecin hospitalier",
        "Médecine Interne Générale (CH)", "chef de clinique",
        "gastro-entérologue"])
    def test_le_poste_du_medecin_qui_ecrit_n_est_pas_signale(self, texte):
        """Il figure dans l'en-tete de presque chaque courrier, et designe
        l'expediteur. Releve sur le corpus de regression."""
        from caviardoc.roles import est_poste_soignant
        assert est_poste_soignant(texte)
        modele = FauxModele({"profession": [(texte, 0.9)]})
        assert quasi_identifiants(modele, "Dr X, " + texte + ".", []) == []

    @pytest.mark.parametrize("texte", [
        "cycliste professionnel", "infirmière scolaire", "sage-femme",
        "pilote de ligne"])
    def test_la_profession_d_un_patient_l_est(self, texte):
        """Les professions de sante non medicales decrivent souvent la
        patiente elle-meme. « Infirmiere » seule, elle, reste du vocabulaire
        neutre (`generique`) : c'est le plus souvent l'equipe soignante."""
        from caviardoc.roles import est_poste_soignant
        assert not est_poste_soignant(texte)
        modele = FauxModele({"profession": [(texte, 0.9)]})
        assert [s.text for s in quasi_identifiants(modele, texte + ", 40 ans", [])] \
            == [texte]

    def test_un_titre_en_capitales_n_est_pas_une_profession(self):
        """« ECHO-DOPPLER VEINEUX… » sortait en profession a 0,94."""
        titre = "ECHO-DOPPLER VEINEUX DU MEMBRE INFERIEUR GAUCHE"
        modele = FauxModele({"profession": [(titre, 0.94)],
                             "evenement": [("GIRO 2022", 0.9)]})
        trouves = quasi_identifiants(modele, titre + "\nGIRO 2022", [])
        assert [s.text for s in trouves] == ["GIRO 2022"], \
            "le filtre ne vise que la profession"


# --------------------------------------------------------------------------- #
# Relecture
# --------------------------------------------------------------------------- #

class TestRelecture:
    def test_arrive_a_juger_et_lisible(self):
        s = _session()
        d = next(d for d in s.detections if d.texte == "cycliste professionnel")
        assert d.etat == Etat.A_JUGER.value
        assert d.a_juger and not d.masquee and not d.doute
        assert [x.text for x in appliquer(s, "corps")] == ["Zorglub"]

    def test_un_clic_le_masque_comme_une_detection_ordinaire(self):
        s = _session()
        decider(s, {"corps:1": {"etat": "retenue"}})
        masques = appliquer(s, "corps")
        assert [x.text for x in masques] == ["Zorglub", "cycliste professionnel"]
        assert masques[1].label == "profession"

    def test_le_laisser_lisible_n_est_pas_un_rejet(self):
        """Un rejet s'apprend (« ne jamais masquer ») ; laisser lisible un
        passage a juger, c'est le laisser tel qu'il est arrive."""
        s = _session()
        decider(s, {"corps:1": {"etat": "retenue"}})
        decider(s, {"corps:1": {"etat": "rejetee"}})
        assert s.detections[1].etat == Etat.A_JUGER.value
        assert s.rejetees == []
        assert [x.text for x in appliquer(s, "corps")] == ["Zorglub"]

    def test_rien_n_est_appris_d_un_passage_a_juger(self, monkeypatch):
        from caviardoc import exceptions
        s = _session()
        decider(s, {"corps:1": {"etat": "rejetee"}})

        class Repertoire:
            retenus = []

            def retenir_jamais(self, texte, motif):
                self.retenus.append(texte)
                return True, ""

            def enregistrer(self):
                pass

        bilan = exceptions.apprendre_de(s, Repertoire())
        assert bilan["total"] == 0 and Repertoire.retenus == []

    def test_un_etat_a_juger_envoye_ne_decouvre_pas_un_nom(self):
        """Seuls « retenue » et « rejetee » s'envoient : « a_juger » sur un nom
        le rendrait lisible sans laisser de trace de rejet."""
        s = _session()
        decider(s, {"corps:0": {"etat": "a_juger"}})
        assert s.detections[0].masquee

    def test_la_session_se_relit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
        from caviardoc.revue import SessionRevue
        s = _session()
        relue = SessionRevue.charger(s.enregistrer())
        assert [d.a_juger for d in relue.detections] == [False, True]
        assert not relue.detections[1].masquee


# --------------------------------------------------------------------------- #
# Affichage
# --------------------------------------------------------------------------- #

class TestAffichage:
    def test_une_couleur_a_part_et_la_raison(self):
        s = _session()
        rendu = surligner(TEXTE, s.detections)
        assert 'class="juger rejetee"' in rendu
        assert "peut aider à reconnaître la personne" in rendu
        assert 'class="modele"' in rendu, "le nom garde sa couleur de provenance"

    def test_masque_il_garde_sa_couleur(self):
        s = _session()
        decider(s, {"corps:1": {"etat": "retenue"}})
        assert 'class="juger"' in surligner(TEXTE, s.detections)

    def test_la_page_de_revue_l_explique(self):
        page = page_html(_session(), {"corps": TEXTE})
        assert "l-juger" in page and "à juger" in page
        assert "peut aider à reconnaître la personne" in page

    def test_la_vue_de_l_interface_le_signale(self):
        tache = interface.Tache("t", "d", "doc.pdf")
        tache.session = _session()
        tache.textes = {"corps": TEXTE}
        vue = interface.revue_en_json(tache)
        passage = vue["parties"][0]["passages"][1]
        assert passage["juger"] is True and passage["masque"] is False
        assert vue["a_juger"] == 1 and vue["doutes"] == 1

    def test_la_page_de_l_interface_porte_la_legende_et_l_aide(self):
        page = interface.page_console("jeton", "gliner-multi-pii-fp16", "app")
        assert 'data-origine="juger"' in page
        assert "peut aider à reconnaître la personne" in page
        assert "mark.juger" in page and ".zone.juger" in page

    def test_en_relecture_des_doutes_un_passage_a_juger_est_montre(self, tmp_path):
        """Sans un regard, il resterait lisible : un document qui n'a que
        cela n'est pas valide en silence."""
        atelier = interface.Atelier(racine=tmp_path)
        tache = interface.Tache("t", "d", "doc.docx")
        relire = interface.crochet_de_relecture(atelier, tache,
                                                seulement_doutes=True)
        parties = [("corps", TEXTE, [
            Span(9, 18, "personne", 1.0, "Zorglub", "regle"),
            Span(20, 42, "profession", 0.86, "cycliste professionnel")])]
        sortie = {}
        fil = threading.Thread(target=lambda: sortie.update(relire(parties)),
                               daemon=True)
        fil.start()
        for _ in range(100):
            if tache.etat == "revue":
                break
            threading.Event().wait(0.05)
        assert tache.etat == "revue"
        tache.reprendre.set()
        fil.join(timeout=5)
        assert [s.text for s in sortie["corps"]] == ["Zorglub"]


# --------------------------------------------------------------------------- #
# Chaine .docx, de bout en bout
# --------------------------------------------------------------------------- #

class TestChaineDocx:
    def _source(self, tmp_path):
        from test_docx import _docx, _p, _t
        chemin = tmp_path / "rapport.docx"
        chemin.write_bytes(_docx(_p(_t(TEXTE))))
        sortie = tmp_path / "sortie" / "rapport.docx"
        sortie.parent.mkdir(exist_ok=True)
        return chemin, sortie

    def test_le_crochet_les_recoit_et_rien_ne_les_masque(self, tmp_path):
        from caviardoc.documents.docx import caviarder_docx
        from test_docx import _lire
        source, sortie = self._source(tmp_path)
        vus = []

        def relire(parties):
            vus.extend(parties)
            session = preparer("rapport.docx", parties)
            return {i: appliquer(session, i) for i, _, _ in parties}

        caviarder_docx(_modele(), source, sortie, revue=relire)
        recus = [sp.text for _, _, spans in vus for sp in spans]
        assert "cycliste professionnel" in recus and "Giro 2022" in recus
        produit = _lire(sortie)
        assert "Zorglub" not in produit
        assert "cycliste professionnel" in produit and "Giro 2022" in produit

    def test_masque_a_la_relecture_il_disparait(self, tmp_path):
        from caviardoc.documents.docx import caviarder_docx
        from test_docx import _lire
        source, sortie = self._source(tmp_path)

        def relire(parties):
            session = preparer("rapport.docx", parties)
            decider(session, {d.identifiant: {"etat": "retenue"}
                              for d in session.detections if d.a_juger})
            return {i: appliquer(session, i) for i, _, _ in parties}

        caviarder_docx(_modele(), source, sortie, revue=relire)
        produit = _lire(sortie)
        assert "cycliste professionnel" not in produit
        assert "Giro 2022" not in produit and "31 ans" not in produit
        assert "[PROFESSION_1]" in produit

    def test_sans_relecture_on_ne_les_cherche_pas(self, tmp_path):
        from caviardoc.documents.docx import caviarder_docx
        source, sortie = self._source(tmp_path)
        modele = _modele()
        caviarder_docx(modele, source, sortie)
        assert all(not set(QUASI_LABELS) & set(a) for a in modele.appels)


# --------------------------------------------------------------------------- #
# Chaine PDF
# --------------------------------------------------------------------------- #

class TestChainePdf:
    @pytest.mark.skipif(
        tesseract.binaire() is None
        or "fra" not in (tesseract.langues() or []),
        reason="la chaine PDF relit le document produit avec Tesseract")
    def test_page_par_page_comme_le_docx(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        from caviardoc.documents import caviardage
        d = pymupdf.open()
        d.new_page().insert_text((72, 80), TEXTE, fontsize=10)
        d.save(tmp_path / "cr.pdf")
        vus = []

        def relire(parties):
            vus.extend(parties)
            session = preparer("cr.pdf", parties)
            decider(session, {x.identifiant: {"etat": "retenue"}
                              for x in session.detections
                              if x.texte == "Giro 2022"})
            return {i: appliquer(session, i) for i, _, _ in parties}

        caviardage.caviarder_pdf(_modele(), tmp_path / "cr.pdf",
                                 tmp_path / "out.pdf", verifier=False,
                                 formats=frozenset({"texte"}), revue=relire)
        recus = {sp.text: sp.label for _, _, spans in vus for sp in spans}
        assert recus.get("cycliste professionnel") == "profession"
        texte = (tmp_path / "out.txt").read_text(encoding="utf-8")
        assert "Giro 2022" not in texte, "masque d'un clic"
        assert "cycliste professionnel" in texte, "laisse lisible sans decision"
