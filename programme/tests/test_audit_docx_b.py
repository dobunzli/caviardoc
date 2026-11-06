"""Pistes d'audit sur la chaine .docx : contenu non lu mais declare « verifie »."""

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_docx import AVS, _fichier, _p, _t  # noqa: E402

from caviardoc.documents.docx import (ChaineImageRequise,  # noqa: E402
                                    caviarder_docx, lire_partie)

MARQUEUR = "Zorglub Quidam"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CORPS_BANAL = _p(_t("Compte rendu de consultation sans particularite. " * 3))


def _sortie(tmp_path, corps, extras=None, **kw):
    source = _fichier(tmp_path / "e.docx", corps, extras)
    sortie = tmp_path / "s.docx"
    rapport = caviarder_docx(None, source, sortie, **kw)
    return rapport, sortie


def _toutes_parties(chemin) -> str:
    with zipfile.ZipFile(chemin) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace")
                         for n in z.namelist())


# --- 1. zero texte lu, pourtant « verifie » ----------------------------------

def test_altchunk_est_retire_et_compte(tmp_path):
    # Decision produit : l'altChunk n'est plus refuse, il est retire (voir
    # test_audit_docx_objets.py pour le detail).
    corps = CORPS_BANAL + '<w:altChunk xmlns:r="x" r:id="rId9"/>'
    extras = {"word/afchunk.mht": f"Patient {MARQUEUR}",
              "word/_rels/document.xml.rels": (
                  '<Relationships xmlns="http://schemas.openxmlformats.org/'
                  'package/2006/relationships"><Relationship Id="rId9" Type='
                  '"http://schemas.openxmlformats.org/officeDocument/2006/'
                  'relationships/aFChunk" Target="afchunk.mht"/>'
                  '</Relationships>')}
    rapport, sortie = _sortie(tmp_path, corps, extras)
    assert rapport.objets == 1
    assert MARQUEUR not in _toutes_parties(sortie)


def test_prefixe_autre_que_w_est_refuse(tmp_path):
    corps = (f'<x:p xmlns:x="{NS_W}"><x:r><x:t>{MARQUEUR}</x:t></x:r></x:p>')
    with pytest.raises(ChaineImageRequise):
        _sortie(tmp_path, corps)


def test_document_sans_texte_est_refuse(tmp_path):
    with pytest.raises(ChaineImageRequise):
        _sortie(tmp_path, "<w:p/>")


# --- 2. texte hors w:t (numero AVS : detecte sans modele) --------------------

CHART = ('<c:chartSpace xmlns:c="c" xmlns:a="http://schemas.openxmlformats.org/'
         f'drawingml/2006/main"><a:p><a:r><a:t>{AVS}</a:t></a:r></a:p>'
         '</c:chartSpace>')


def test_titre_de_graphique_est_masque(tmp_path):
    rapport, sortie = _sortie(tmp_path, CORPS_BANAL,
                              {"word/charts/chart1.xml": CHART})
    assert AVS not in _toutes_parties(sortie) or not rapport.verifie


def test_code_de_champ_est_traite(tmp_path):
    champ = (f'<w:r><w:instrText> MERGEFIELD {AVS} </w:instrText></w:r>')
    rapport, sortie = _sortie(tmp_path, CORPS_BANAL + _p(champ))
    assert AVS not in _toutes_parties(sortie) or not rapport.verifie


def test_alias_de_controle_est_traite(tmp_path):
    sdt = (f'<w:sdt><w:sdtPr><w:alias w:val="{AVS}"/></w:sdtPr>'
           '<w:sdtContent><w:r><w:t>x</w:t></w:r></w:sdtContent></w:sdt>')
    rapport, sortie = _sortie(tmp_path, CORPS_BANAL + _p(sdt))
    assert AVS not in _toutes_parties(sortie) or not rapport.verifie


# --- 3. objets incorpores ----------------------------------------------------

def test_objet_incorpore_n_est_pas_verifie(tmp_path):
    rapport, sortie = _sortie(tmp_path, CORPS_BANAL,
                              {"word/embeddings/o1.xlsx": MARQUEUR})
    assert rapport.objets == 1
    assert not rapport.verifie or MARQUEUR not in _toutes_parties(sortie)
    assert MARQUEUR not in _toutes_parties(sortie)


# --- 4. traits d'union ------------------------------------------------------

def test_trait_d_union_insecable_lu_comme_tiret():
    xml = ('<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Jean</w:t>'
           '<w:noBreakHyphen/><w:t>Zorglub</w:t></w:r></w:p></w:body></w:document>')
    assert lire_partie(xml)[0] == "Jean-Zorglub"


def test_trait_d_union_conditionnel_ignore():
    xml = ('<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>Zorg</w:t>'
           '<w:softHyphen/><w:t>lub</w:t></w:r></w:p></w:body></w:document>')
    assert lire_partie(xml)[0] == "Zorglub"


# --- revue : SmartArt et guillemets simples ---------------------------------

def test_smartart_n_est_pas_refuse_et_son_texte_est_masque(tmp_path):
    data = ('<dgm:dataModel xmlns:dgm="d" xmlns:a="http://schemas.openxmlformats'
            '.org/drawingml/2006/main"><dgm:t><a:bodyPr/><a:p><a:r><a:t>'
            f'{AVS}</a:t></a:r></a:p></dgm:t></dgm:dataModel>')
    rapport, sortie = _sortie(tmp_path, CORPS_BANAL,
                              {"word/diagrams/data1.xml": data})
    assert rapport.verifie
    assert AVS not in _toutes_parties(sortie)


@pytest.mark.parametrize("guillemet", ['"', "'"])
def test_alias_et_etiquette_vides_quel_que_soit_le_guillemet(tmp_path, guillemet):
    g = guillemet
    sdt = (f'<w:sdt><w:sdtPr><w:alias w:val={g}{MARQUEUR}{g}/>'
           f'<w:tag w:val={g}{MARQUEUR}{g}/></w:sdtPr>'
           '<w:sdtContent><w:r><w:t>x</w:t></w:r></w:sdtContent></w:sdt>')
    _, sortie = _sortie(tmp_path, CORPS_BANAL + _p(sdt))
    assert MARQUEUR not in _toutes_parties(sortie)
