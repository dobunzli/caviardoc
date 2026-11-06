"""Aiguillage de la commande `caviarder` selon le format d'entree.

Le moteur n'est pas charge ici : `load_model` est remplace par un modele nul,
qui laisse la couche 1 — les regles et leurs sommes de controle — faire la
detection. Ce sont l'aiguillage, le renommage et la lecture du rapport qui sont
en jeu, pas la qualite du modele.
"""

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import cli, engine  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_docx import AVS, _docx, _p, _t  # noqa: E402


def _args(fichiers, sortie, **kw):
    """Arguments construits par le vrai analyseur, non a la main.

    Un Namespace fabrique de toutes pieces derive a chaque option ajoutee a la
    commande : le test tombe alors sur un AttributeError sans rapport avec ce
    qu'il verifie, et c'est arrive deux fois. Passer par `build_parser`
    garantit qu'il porte exactement ce que `cmd_caviarder` recevra.
    """
    args = cli.build_parser().parse_args(
        ["caviarder", *[str(f) for f in fichiers], "--sortie", str(sortie)])
    for cle, valeur in kw.items():
        setattr(args, cle, valeur)
    return args


def _sans_modele(monkeypatch):
    monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)


@pytest.mark.parametrize("options, logos, entetes", [
    ([], True, True),
    (["--sans-logos"], False, True),
    (["--sans-entetes"], True, False),
    (["--sans-logos", "--sans-entetes"], False, False),
    # Les anciennes options restent acceptees : des scripts les passent.
    (["--logos", "--entetes"], True, True),
])
def test_logos_et_entetes_masques_par_defaut(tmp_path, options, logos, entetes):
    """Comme dans l'interface : les laisser passer est un choix explicite."""
    args = cli.build_parser().parse_args(
        ["caviarder", "f.pdf", "--sortie", str(tmp_path), *options])
    assert (args.logos, args.entetes) == (logos, entetes)


def test_docx_traite_et_renomme(tmp_path, monkeypatch, capsys):
    """Le .docx passe par la chaine bureautique, et son nom est anonymise."""
    _sans_modele(monkeypatch)
    source = tmp_path / f"rapport_{AVS}.docx"
    source.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
    dest = tmp_path / "sortie"

    code = cli.cmd_caviarder(_args([source], dest))

    assert code == 0
    produits = list(dest.iterdir())
    assert len(produits) == 1
    assert AVS not in produits[0].name
    with zipfile.ZipFile(produits[0]) as z:
        assert AVS not in z.read("word/document.xml").decode("utf-8")
    assert "word/document.xml" in capsys.readouterr().out


def test_format_inconnu_est_signale_et_ignore(tmp_path, monkeypatch, capsys):
    """Un .doc binaire doit etre refuse, jamais traite a moitie."""
    _sans_modele(monkeypatch)
    source = tmp_path / "vieux.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0")
    dest = tmp_path / "sortie"

    code = cli.cmd_caviarder(_args([source], dest))

    assert code == 0
    assert list(dest.iterdir()) == []
    assert "enregistrez le document en .docx" in capsys.readouterr().out


def test_lot_mixte_partage_ses_pseudonymes(tmp_path, monkeypatch):
    """Deux .docx du meme lot doivent nommer la meme entite de la meme facon."""
    _sans_modele(monkeypatch)
    premier = tmp_path / "a.docx"
    second = tmp_path / "b.docx"
    premier.write_bytes(_docx(_p(_t(f"Assure {AVS}"))))
    second.write_bytes(_docx(_p(_t(f"Le meme assure {AVS}"))))
    dest = tmp_path / "sortie"

    assert cli.cmd_caviarder(_args([premier, second], dest,
                                   format="texte")) == 0

    textes = [f.read_text(encoding="utf-8") for f in sorted(dest.glob("*.txt"))]
    assert len(textes) == 2
    assert all("[NUMERO_AVS_1]" in t for t in textes)


@pytest.mark.parametrize("secondes, attendu", [
    (0.4, "0 s"), (45, "45 s"), (59.6, "1 min 00 s"), (185, "3 min 05 s"),
    (3720, "1 h 02 min"),
])
def test_duree_lisible(secondes, attendu):
    assert cli.duree_lisible(secondes) == attendu


def test_le_lot_annonce_sa_duree(tmp_path, monkeypatch, capsys):
    """La fin du lot dit combien de documents ont ete traites, et en combien
    de temps."""
    _sans_modele(monkeypatch)
    source = tmp_path / "rapport.docx"
    source.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))

    cli.cmd_caviarder(_args([source], tmp_path / "sortie"))

    assert "1 document(s) traite(s) en 0 s." in capsys.readouterr().out


def test_sans_tesseract_le_lot_s_arrete_une_fois_avant_tout_fichier(
        tmp_path, monkeypatch, capsys):
    """Message unique, sans nom de classe, et rien n'est cree ni traite."""
    monkeypatch.setattr(cli.tesseract, "binaire", lambda: None)
    appels = []
    monkeypatch.setattr(engine, "load_model",
                        lambda *a, **kw: appels.append(1))
    for nom in ("a.pdf", "b.pdf"):
        (tmp_path / nom).write_bytes(b"%PDF-1.4")
    sortie = tmp_path / "out"
    code = cli.cmd_caviarder(_args([tmp_path / "a.pdf", tmp_path / "b.pdf"],
                                   sortie))
    sortie_console = capsys.readouterr().out
    assert code == 1
    assert sortie_console.count("Tesseract (OCR) est absent") == 1
    assert "TesseractIndisponible" not in sortie_console
    assert "caviardoc setup" in sortie_console
    assert not appels and not sortie.exists()


def test_document_trop_long_message_clair(monkeypatch):
    """Le motif d'echec est la phrase seule, sans nom de classe."""
    from caviardoc.documents import caviardage
    monkeypatch.setattr(caviardage, "PAGES_MAX", 3)
    with pytest.raises(caviardage.DocumentTropLong) as err:
        caviardage._borner_pages(4)
    motif = cli._motif_echec(err.value)
    assert motif == ("Document trop long : 4 pages (maximum 3). "
                     "Découpez-le en plusieurs fichiers.")
    assert "ValueError" not in motif and "DocumentTropLong" not in motif


def test_sans_zxing_le_lot_s_arrete_une_fois_avant_tout_fichier(
        tmp_path, monkeypatch, capsys):
    """Meme refus unique que pour Tesseract : un message, pas une trace."""
    monkeypatch.setattr(cli.tesseract, "binaire", lambda: "/x")
    monkeypatch.setattr(cli.tesseract, "langues", lambda: ["fra"])
    monkeypatch.setitem(sys.modules, "zxingcpp", None)
    appels = []
    monkeypatch.setattr(engine, "load_model",
                        lambda *a, **kw: appels.append(1))
    for nom in ("a.pdf", "b.pdf"):
        (tmp_path / nom).write_bytes(b"%PDF-1.4")
    sortie = tmp_path / "out"
    code = cli.cmd_caviarder(_args([tmp_path / "a.pdf", tmp_path / "b.pdf"],
                                   sortie))
    sortie_console = capsys.readouterr().out
    assert code == 1
    assert sortie_console.count("La lecture des codes-barres") == 1
    assert "Traceback" not in sortie_console
    assert "CodesBarresIndisponibles" not in sortie_console
    assert "caviardoc setup" in sortie_console
    assert not appels and not sortie.exists()
