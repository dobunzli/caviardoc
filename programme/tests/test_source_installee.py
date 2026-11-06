"""Le venv execute-t-il le code de ce depot, ou celui d'un autre dossier ?

L'installation editable retient le chemin du depot au moment du `uv sync`.
Deplacer ou renommer le dossier ne le met pas a jour : la commande `caviardoc`
execute alors un code different de celui qu'on lit et modifie.

Le cas s'est produit : le venv d'une machine pointait encore vers
« Projets/PDF anonymizer », nom porte par le projet sur une autre machine et
inexistant sur celle-ci, pendant que le diagnostic affichait tout au vert.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import doctor, paths  # noqa: E402
from caviardoc.doctor import Level  # noqa: E402


@pytest.fixture
def venv(tmp_path, monkeypatch):
    """Un venv factice, avec la trace PEP 610 d'une installation editable.

    `sys.prefix` est detourne vers un dossier vide : le controle examine aussi
    le venv de l'interpreteur courant, et sans cela la vraie installation du
    projet serait trouvee, rendant les cas d'espece intestables.
    """
    racine = tmp_path / "venv"
    infos = racine / "lib/python3.11/site-packages/caviardoc-0.1.0.dist-info"
    infos.mkdir(parents=True)
    (tmp_path / "prefix-vide").mkdir()
    monkeypatch.setattr(paths, "venv_dir", lambda: racine)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix-vide"))

    def ecrire(url: str) -> None:
        (infos / "direct_url.json").write_text(
            json.dumps({"url": url, "dir_info": {"editable": True}}),
            encoding="utf-8")

    return ecrire


def _url(chemin: Path) -> str:
    return chemin.as_uri()


class TestConcordance:
    def test_meme_dossier(self, venv, tmp_path, monkeypatch):
        depot = tmp_path / "depot"
        depot.mkdir()
        monkeypatch.setattr(paths, "project_root", lambda: depot)
        venv(_url(depot))

        ch = doctor.check_source_installee()
        assert ch.level is Level.OK

    def test_dossier_different_mais_existant(self, venv, tmp_path, monkeypatch):
        """Le pire cas : les deux existent, et `caviardoc` execute le mauvais."""
        depot, autre = tmp_path / "depot", tmp_path / "autre"
        depot.mkdir()
        autre.mkdir()
        monkeypatch.setattr(paths, "project_root", lambda: depot)
        venv(_url(autre))

        ch = doctor.check_source_installee()
        assert ch.level is Level.FAIL
        assert str(autre) in ch.detail
        assert "n'existe pas" not in ch.detail
        assert ch.auto_fixable

    def test_dossier_disparu(self, venv, tmp_path, monkeypatch):
        depot = tmp_path / "depot"
        depot.mkdir()
        monkeypatch.setattr(paths, "project_root", lambda: depot)
        venv(_url(tmp_path / "envole"))

        ch = doctor.check_source_installee()
        assert ch.level is Level.FAIL
        assert "n'existe pas" in ch.detail


class TestCheminsParticuliers:
    def test_espace_encodee_dans_l_url(self, venv, tmp_path, monkeypatch):
        """« PDF%20anonymizer » doit se comparer comme « PDF anonymizer ».

        Sans decodage, tout chemin contenant une espace paraitrait different
        de lui-meme.
        """
        depot = tmp_path / "PDF anonymizer"
        depot.mkdir()
        monkeypatch.setattr(paths, "project_root", lambda: depot)
        venv(_url(depot))

        assert doctor.check_source_installee().level is Level.OK

    def test_lien_symbolique_resolu(self, venv, tmp_path, monkeypatch):
        reel = tmp_path / "reel"
        reel.mkdir()
        lien = tmp_path / "lien"
        lien.symlink_to(reel)
        monkeypatch.setattr(paths, "project_root", lambda: lien)
        venv(_url(reel))

        assert doctor.check_source_installee().level is Level.OK


class TestRienAVerifier:
    def test_sans_trace_d_installation(self, tmp_path, monkeypatch):
        """Un paquet non installe en editable ne se compare a rien."""
        (tmp_path / "prefix-vide").mkdir()
        monkeypatch.setattr(paths, "venv_dir", lambda: tmp_path / "vide")
        monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix-vide"))
        assert doctor.check_source_installee() is None

    def test_direct_url_illisible(self, venv, tmp_path, monkeypatch):
        infos = (tmp_path / "venv/lib/python3.11/site-packages"
                 / "caviardoc-0.1.0.dist-info/direct_url.json")
        infos.write_text("{pas du json", encoding="utf-8")
        monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
        assert doctor.check_source_installee() is None

    def test_installation_non_locale_ignoree(self, venv, tmp_path, monkeypatch):
        """Un paquet installe depuis un index n'a pas de dossier source."""
        monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
        venv("https://example.invalid/caviardoc.whl")
        assert doctor.check_source_installee() is None


class TestIntegrationAuDiagnostic:
    """Le controle ne s'affiche que s'il y a quelque chose a comparer.

    Une execution sur Python nu, sans installation editable, n'a pas de
    `direct_url.json` : le controle rend None et le diagnostic n'affiche rien
    plutot qu'un verdict invente. Ces tests s'adaptent donc a la facon dont la
    suite est lancee, au lieu de supposer un environnement.
    """

    def test_le_controle_figure_quand_il_a_de_quoi_comparer(self):
        noms = [ch.name for ch in doctor.run_all()]
        if doctor._source_installee() is None:
            assert "Code execute" not in noms, \
                "sans trace d'installation, ne rien affirmer"
        else:
            assert "Code execute" in noms

    def test_il_precede_la_mesure(self):
        """Un code errone rend la mesure sans objet : le signaler avant."""
        if doctor._source_installee() is None:
            pytest.skip("aucune installation editable a comparer ici")
        noms = [ch.name for ch in doctor.run_all()]
        assert noms.index("Code execute") < noms.index("Mesure du rappel")

    def test_le_venv_de_l_interpreteur_est_examine(self, tmp_path, monkeypatch):
        """Lancer `caviardoc` depuis un autre venv que celui de la machine ne
        doit pas rendre le controle muet : c'est le code qui s'execute qui
        compte."""
        autre = tmp_path / "venv-courant"
        infos = autre / "lib/python3.11/site-packages/caviardoc-0.1.0.dist-info"
        infos.mkdir(parents=True)
        (infos / "direct_url.json").write_text(
            json.dumps({"url": (tmp_path / "depot").as_uri(),
                        "dir_info": {"editable": True}}), encoding="utf-8")

        monkeypatch.setattr(paths, "venv_dir", lambda: tmp_path / "absent")
        monkeypatch.setattr(sys, "prefix", str(autre))
        assert doctor._source_installee() == tmp_path / "depot"
