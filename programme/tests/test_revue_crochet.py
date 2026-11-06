"""Le crochet de relecture : appele au bon moment, et obei.

Le drapeau `--revue` existait, etait documente, importait `preparer`,
`appliquer` et `servir` — et n'etait jamais lu. Un utilisateur qui le passait
croyait relire son document ; il ne relisait rien, et rien ne le lui disait.
La chaine .docx, elle, n'avait meme pas de crochet.

Ces tests verrouillent le contrat des deux chaines : le crochet recoit les
detections apres propagation et avant ecriture, et ce qu'il rend est ce qui
est applique.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from caviardoc.documents.docx import caviarder_docx  # noqa: E402
from test_docx import AVS, _docx, _lire, _p, _t  # noqa: E402


def _source(tmp_path, corps=None):
    chemin = tmp_path / "rapport.docx"
    chemin.write_bytes(_docx(corps or _p(_t("Assure "), _t(AVS), _t(" ici"))))
    sortie = tmp_path / "sortie" / "rapport.docx"
    sortie.parent.mkdir(exist_ok=True)
    return chemin, sortie


class TestChaineDocx:
    def test_le_crochet_recoit_les_detections(self, tmp_path):
        source, sortie = _source(tmp_path)
        vus = []

        def relire(parties):
            vus.extend(parties)
            return {intitule: spans for intitule, _, spans in parties}

        caviarder_docx(None, source, sortie, revue=relire)

        assert vus, "le crochet doit etre appele"
        intitules = [intitule for intitule, _, _ in vus]
        assert "corps du document" in intitules
        corps = next(p for p in vus if p[0] == "corps du document")
        assert AVS in corps[1], "le crochet voit le texte de la partie"
        assert any(sp.text == AVS for sp in corps[2]), \
            "il voit les detections, deja propagees"

    def test_un_masque_retire_par_le_crochet_ne_sapplique_pas(self, tmp_path):
        """C'est tout l'objet de la relecture : corriger un faux positif."""
        source, sortie = _source(tmp_path)

        caviarder_docx(None, source, sortie,
                       revue=lambda parties: {i: [] for i, _, _ in parties})

        produit = _lire(sortie)
        assert AVS in produit, "le masque retire ne devait pas etre pose"
        assert "[NUMERO_AVS_1]" not in produit

    def test_sans_crochet_le_comportement_ne_change_pas(self, tmp_path):
        source, sortie = _source(tmp_path)
        caviarder_docx(None, source, sortie)
        produit = _lire(sortie)
        assert AVS not in produit
        assert "[NUMERO_AVS_1]" in produit

    def test_le_crochet_agit_avant_ecriture(self, tmp_path):
        """Relire apres coup n'aurait plus rien a corriger."""
        source, sortie = _source(tmp_path)
        existait = []

        def relire(parties):
            existait.append(sortie.exists())
            return {i: spans for i, _, spans in parties}

        caviarder_docx(None, source, sortie, revue=relire)
        assert existait == [False], "le fichier ne doit pas exister encore"

    def test_une_partie_absente_du_retour_garde_ses_detections(self, tmp_path):
        """Un crochet qui rend un dictionnaire incomplet ne doit rien decouvrir."""
        source, sortie = _source(tmp_path)

        caviarder_docx(None, source, sortie, revue=lambda parties: {})

        produit = _lire(sortie)
        assert AVS not in produit, "l'absence de decision vaut « masquer »"


class TestDrapeauCli:
    """`--revue` doit atteindre les deux chaines."""

    def _espionner(self, monkeypatch):
        """Remplace la chaine .docx par un espion qui delegue a la vraie.

        La fonction d'origine est capturee **avant** le remplacement : la
        prendre depuis le module ensuite ferait appeler l'espion par
        lui-meme.
        """
        from caviardoc import engine, revue_web
        from caviardoc.documents import docx as mod_docx

        monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)
        # `servir` ouvre un serveur et attend l'utilisateur : sans ce
        # remplacement, le test se bloque — ce qui est en soi la preuve que le
        # drapeau agit desormais.
        monkeypatch.setattr(revue_web, "servir",
                            lambda session, textes, **kw: session)
        vraie = mod_docx.caviarder_docx
        recus = {}

        def espion(model, source, sortie, **kw):
            recus["revue"] = kw.get("revue")
            return vraie(model, source, sortie, **kw)

        monkeypatch.setattr(mod_docx, "caviarder_docx", espion)
        return recus

    def _lancer(self, tmp_path, *drapeaux):
        from caviardoc import cli

        source = tmp_path / "rapport.docx"
        source.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
        args = cli.build_parser().parse_args(
            ["caviarder", str(source), "--sortie", str(tmp_path / "out"),
             *drapeaux])
        cli.cmd_caviarder(args)
        return args

    def test_le_drapeau_construit_un_crochet(self, tmp_path, monkeypatch):
        recus = self._espionner(monkeypatch)
        args = self._lancer(tmp_path, "--revue")
        assert args.revue is True
        assert recus["revue"] is not None, \
            "le drapeau --revue doit passer un crochet a la chaine"
        assert callable(recus["revue"])

    def test_sans_le_drapeau_aucun_crochet(self, tmp_path, monkeypatch):
        recus = self._espionner(monkeypatch)
        self._lancer(tmp_path)
        assert recus["revue"] is None
