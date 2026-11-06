"""Objets incorpores d'un .docx : retires de la sortie, comptes, signales."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx  # noqa: E402
from test_docx import CORE, DOCUMENT, ENTETE, _p, _t  # noqa: E402

from caviardoc.documents.docx import MENTION_OBJET, caviarder_docx  # noqa: E402
from caviardoc.engine import Span  # noqa: E402
from caviardoc.interface import a_verifier  # noqa: E402

NOM = "Testnom Fictif"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
T_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
BANAL = _p(_t("Compte rendu de consultation sans particularite. " * 3))


def _rels(*relations):
    return (ENTETE + f'<Relationships xmlns="{NS_REL}">' + "".join(relations)
            + "</Relationships>")


def _rel(ident, type_, cible):
    return f'<Relationship Id="{ident}" Type="{T_REL}{type_}" Target="{cible}"/>'


NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _paquet(corps, extras, overrides=()):
    """Paquet que python-docx sait rouvrir (relation racine vers le corps)."""
    extras = dict(extras or {})
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ENTETE + f'<Types xmlns="{NS_CT}">'
                   '<Default Extension="rels" ContentType="application/vnd.'
                   'openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="'
                   'application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/>'
                   + "".join(f'<Override PartName="/{n}" ContentType="'
                             'application/octet-stream"/>' for n in overrides)
                   + "</Types>")
        z.writestr("_rels/.rels", _rels(_rel("rId1", "officeDocument",
                                             "word/document.xml")))
        z.writestr("word/document.xml", DOCUMENT.format(corps=corps))
        z.writestr("docProps/core.xml", CORE)
        if "word/_rels/document.xml.rels" not in extras:
            extras["word/_rels/document.xml.rels"] = _rels()
        for nom, contenu in extras.items():
            z.writestr(nom, contenu)
    return tampon.getvalue()


def _produire(tmp_path, corps, extras=None, overrides=()):
    source = tmp_path / "e.docx"
    source.write_bytes(_paquet(corps, extras, overrides))
    sortie = tmp_path / "s.docx"
    return caviarder_docx(None, source, sortie), sortie


def _noms(sortie):
    with zipfile.ZipFile(sortie) as z:
        return z.namelist()


def _tout(sortie):
    with zipfile.ZipFile(sortie) as z:
        return b"\n".join(z.read(n) for n in z.namelist()).decode("utf-8", "replace")


def _png():
    from PIL import Image
    tampon = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(tampon, "PNG")
    return tampon.getvalue()


def _contenu_types(sortie):
    with zipfile.ZipFile(sortie) as z:
        return z.read("[Content_Types].xml").decode("utf-8")


def test_objet_ole_retire_avec_son_apercu(tmp_path):
    ole = ('<w:r><w:object xmlns:v="v" xmlns:o="o" xmlns:r="r">'
           '<v:shape><v:imagedata r:id="rId5"/></v:shape>'
           '<o:OLEObject r:id="rId6"/></w:object></w:r>')
    extras = {
        "word/_rels/document.xml.rels": _rels(
            _rel("rId5", "image", "media/apercu.png"),
            _rel("rId6", "oleObject", "embeddings/o1.bin")),
        "word/embeddings/o1.bin": NOM,
        "word/media/apercu.png": b"pas une image " + NOM.encode(),
    }
    rapport, sortie = _produire(tmp_path, BANAL + _p(ole), extras,
                                overrides=["word/embeddings/o1.bin"])
    assert rapport.objets == 1
    noms = _noms(sortie)
    assert not [n for n in noms if "embeddings" in n or "media/apercu" in n]
    tout = _tout(sortie)
    assert NOM not in tout and "o1.bin" not in tout and "rId6" not in tout
    assert MENTION_OBJET in tout
    assert "embeddings" not in _contenu_types(sortie)
    assert docx.Document(str(sortie)).paragraphs
    assert a_verifier({"fuites": [], "verifie": True, "reserves": [],
                       "images_opaques": [], "images_metadonnees": [],
                       "repli_complet": False, "objets_retires": rapport.objets})


def test_altchunk_retire(tmp_path):
    corps = BANAL + '<w:altChunk xmlns:r="r" r:id="rId9"/>'
    extras = {
        "word/_rels/document.xml.rels": _rels(_rel("rId9", "aFChunk", "afchunk.html")),
        "word/afchunk.html": f"<html>Patient {NOM}</html>",
    }
    rapport, sortie = _produire(tmp_path, corps, extras)
    assert rapport.objets == 1
    assert "word/afchunk.html" not in _noms(sortie)
    tout = _tout(sortie)
    assert NOM not in tout and "rId9" not in tout and MENTION_OBJET in tout
    docx.Document(str(sortie))


def test_objet_dans_un_en_tete_retire_sa_relation(tmp_path):
    entete = ('<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
              '2006/main" xmlns:r="r"><w:p><w:r><w:object><o:OLEObject xmlns:o="o" '
              'r:id="rId1"/></w:object></w:r></w:p></w:hdr>')
    extras = {
        "word/header1.xml": entete,
        "word/_rels/header1.xml.rels": _rels(_rel("rId1", "package", "embeddings/p.xlsx")),
        "word/embeddings/p.xlsx": NOM,
    }
    rapport, sortie = _produire(tmp_path, BANAL, extras)
    assert rapport.objets == 1
    assert "word/embeddings/p.xlsx" not in _noms(sortie)
    assert "p.xlsx" not in _tout(sortie)


def test_document_sans_objet_inchange(tmp_path):
    rapport, sortie = _produire(tmp_path, BANAL)
    assert rapport.objets == 0
    assert MENTION_OBJET not in _tout(sortie)
    assert not a_verifier({"fuites": [], "verifie": True, "reserves": [],
                           "images_opaques": [], "images_metadonnees": [],
                           "repli_complet": False, "objets_retires": rapport.objets})


def test_le_compte_est_rapporte(tmp_path):
    corps = (BANAL + '<w:altChunk xmlns:r="r" r:id="rId1"/>'
             + '<w:altChunk xmlns:r="r" r:id="rId2"/>')
    extras = {
        "word/_rels/document.xml.rels": _rels(
            _rel("rId1", "aFChunk", "a1.html"), _rel("rId2", "aFChunk", "a2.html")),
        "word/a1.html": NOM, "word/a2.html": NOM,
        "word/embeddings/o.bin": NOM,
    }
    rapport, _ = _produire(tmp_path, corps, extras)
    assert rapport.objets == 3


def test_aucun_identifiant_ne_vise_une_relation_absente(tmp_path):
    import re
    ole = ('<w:r><w:object xmlns:v="v" xmlns:o="o" xmlns:r="r">'
           '<v:shape><v:imagedata r:id="rId5"/></v:shape>'
           '<o:OLEObject r:id="rId6"/></w:object></w:r>')
    isole = ('<w:r xmlns:o="o" xmlns:r="r"><o:OLEObject r:id="rId7"/></w:r>')
    corps = BANAL + _p(ole) + _p(isole) + '<w:altChunk xmlns:r="r" r:id="rId9"/>'
    extras = {
        "word/_rels/document.xml.rels": _rels(
            _rel("rId5", "image", "media/apercu.png"),
            _rel("rId6", "oleObject", "embeddings/o1.bin"),
            _rel("rId7", "oleObject", "embeddings/o2.bin"),
            _rel("rId9", "aFChunk", "afchunk.html")),
        "word/embeddings/o1.bin": NOM, "word/embeddings/o2.bin": NOM,
        "word/media/apercu.png": _png(), "word/afchunk.html": NOM,
    }
    rapport, sortie = _produire(tmp_path, corps, extras)
    assert rapport.objets == 3
    with zipfile.ZipFile(sortie) as z:
        for nom in z.namelist():
            if not nom.endswith(".xml") or nom.startswith(("docProps", "[")):
                continue
            dossier, _, fichier = nom.rpartition("/")
            rels = f"{dossier}/_rels/{fichier}.rels"
            declarees = set(re.findall(r'\bId="([^"]*)"', z.read(rels).decode())
                            if rels in z.namelist() else [])
            vises = re.findall(r'\b(?:r:id|r:embed|r:link|r:pict|o:relid)="([^"]*)"',
                               z.read(nom).decode())
            assert set(vises) <= declarees, (nom, vises, declarees)


def test_graphique_avec_classeur_incorpore(tmp_path):
    graphique = ('<c:chartSpace xmlns:c="c" xmlns:r="r"><c:chart><c:title><c:tx>'
                 '<c:strRef><c:strCache><c:pt><c:v>Titre</c:v></c:pt></c:strCache>'
                 '</c:strRef></c:tx></c:title></c:chart>'
                 '<c:externalData r:id="rId1"><c:autoUpdate val="0"/>'
                 '</c:externalData></c:chartSpace>')
    extras = {
        "word/charts/chart1.xml": graphique,
        "word/charts/_rels/chart1.xml.rels": _rels(
            _rel("rId1", "package", "../embeddings/classeur.xlsx")),
        "word/embeddings/classeur.xlsx": NOM,
    }
    rapport, sortie = _produire(tmp_path, BANAL, extras)
    assert rapport.objets == 1
    noms = _noms(sortie)
    assert "word/charts/chart1.xml" in noms
    assert not [n for n in noms if "embeddings" in n]
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
        rels = z.read("word/charts/_rels/chart1.xml.rels").decode()
    assert "externalData" not in chart and "r:id" not in chart
    assert "rId1" not in rels and "classeur" not in rels
    docx.Document(str(sortie))


def _graphique(etiquette, nombre="12"):
    return ('<c:chartSpace xmlns:c="c"><c:chart><c:plotArea><c:ser><c:tx>'
            f'<c:strRef><c:f>A1</c:f><c:strCache><c:pt idx="0"><c:v>{etiquette}'
            '</c:v></c:pt></c:strCache></c:strRef></c:tx><c:val><c:numRef>'
            f'<c:numCache><c:pt idx="0"><c:v>{nombre}</c:v></c:pt></c:numCache>'
            '</c:numRef></c:val></c:ser></c:plotArea></c:chart></c:chartSpace>')


def test_etiquette_de_serie_en_cache_est_masquee(tmp_path):
    from test_docx import AVS
    rapport, sortie = _produire(tmp_path, BANAL, {
        "word/charts/chart1.xml": _graphique(AVS, nombre="7561234567897")})
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
    assert AVS not in chart
    # Un AVS stocke comme nombre est un identifiant : neutralise, XML valide.
    assert "7561234567897" not in chart
    assert "<c:v>0</c:v>" in chart
    assert rapport.entites_total >= 1 and rapport.verifie and not rapport.fuites
    docx.Document(str(sortie))


def test_etiquette_de_serie_relue_si_le_masquage_est_contourne():
    from test_docx import AVS

    from caviardoc.documents.docx import _fuites_xml
    brut = _graphique(AVS).encode()
    assert _fuites_xml({"word/charts/chart1.xml": brut}, {AVS}) == {AVS}


def _graphique_nombres(valeurs, format_code="General"):
    pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>'
                  for i, v in enumerate(valeurs))
    return ('<c:chartSpace xmlns:c="c"><c:chart><c:plotArea><c:ser><c:val>'
            f'<c:numRef><c:f>A1</c:f><c:numCache><c:formatCode>{format_code}'
            f'</c:formatCode>{pts}</c:numCache></c:numRef></c:val>'
            f'<c:xVal><c:numLit>{pts}</c:numLit></c:xVal></c:ser></c:plotArea>'
            '</c:chart></c:chartSpace>')


def test_identifiants_numeriques_en_cache_sont_neutralises(tmp_path):
    valeurs = ["7561234567897", "791234567", "5.4", "120", "0.25", "1.2E-3"]
    graphique = _graphique_nombres(valeurs, "General")
    rapport, sortie = _produire(tmp_path, BANAL, {
        "word/charts/chart1.xml": graphique})
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
    assert "7561234567897" not in chart
    assert "791234567" not in chart
    # Les mesures ordinaires sont intactes, dans numCache comme dans numLit.
    assert chart.count("<c:v>5.4</c:v>") == 2
    assert chart.count("<c:v>120</c:v>") == 2
    assert chart.count("<c:v>0.25</c:v>") == 2 and "1.2E-3" in chart
    assert chart.count("<c:v>0</c:v>") == 4
    assert rapport.verifie and not rapport.fuites
    docx.Document(str(sortie))


def test_code_de_format_identifiant_est_neutralise(tmp_path):
    graphique = _graphique_nombres(["5.4"], '0.00 "AVS 756.1234.5678.97"')
    _, sortie = _produire(tmp_path, BANAL, {"word/charts/chart1.xml": graphique})
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
    assert "756.1234" not in chart and "<c:v>5.4</c:v>" in chart


def test_graphique_classeur_retire_et_graphique_valide(tmp_path):
    graphique = _graphique_nombres(["7561234567897"]).replace(
        "</c:chartSpace>", '<c:externalData r:id="rId1"><c:autoUpdate val="0"/>'
        "</c:externalData></c:chartSpace>").replace(
        'xmlns:c="c"', 'xmlns:c="c" xmlns:r="r"')
    _, sortie = _produire(tmp_path, BANAL, {
        "word/charts/chart1.xml": graphique,
        "word/charts/_rels/chart1.xml.rels": _rels(
            _rel("rId1", "package", "../embeddings/classeur.xlsx")),
        "word/embeddings/classeur.xlsx": NOM})
    assert not [n for n in _noms(sortie) if "embeddings" in n]
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
    assert "externalData" not in chart and "7561234567897" not in chart
    docx.Document(str(sortie))


def test_nom_en_texte_libre_dans_un_cache_de_graphique(tmp_path, monkeypatch):
    from caviardoc.documents import docx as module
    nom = "Zorglub Quidam"

    def detecte(model, texte, **kw):
        i = texte.find(nom)
        if i == -1:
            return []
        return [Span(i, i + len(nom), "personne", 0.9, nom)]
    monkeypatch.setattr(module, "analyse", detecte)
    corps = _p(_t(f"Patient : {nom}. ")) + BANAL
    _, sortie = _produire(tmp_path, corps, {
        "word/charts/chart1.xml": _graphique(f"Courbe de {nom}")})
    with zipfile.ZipFile(sortie) as z:
        chart = z.read("word/charts/chart1.xml").decode()
    assert "Zorglub" not in _tout(sortie) and "Quidam" not in _tout(sortie)
    assert "<c:v>" in chart
    docx.Document(str(sortie))
