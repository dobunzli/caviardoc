"""Chaine bureautique : un .docx anonymise ne doit rien garder de l'original.

Ces tests n'ont pas besoin du modele : la couche 1 (regles et sommes de
controle) suffit a produire des detections deterministes, et c'est la mecanique
de reecriture du XML qui est en jeu ici, pas la qualite de la detection.
"""

import io
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.docx import (ChaineImageRequise,  # noqa: E402
                                    caviarder_docx, effacer_chaines,
                                    lire_partie, neutraliser_auteurs,
                                    neutraliser_proprietes, remplacer,
                                    supprimer_element, verifier_octets)
from caviardoc.documents.zones import OptionsZones  # noqa: E402
from caviardoc.engine import Span  # noqa: E402

#: Numero AVS valide (prefixe 756, cle EAN-13) : detecte par la couche 1, donc
#: sans modele.
AVS = "756.1234.5678.97"

ENTETE = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

DOCUMENT = (ENTETE + '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main" xmlns:mc="http://schemas.openxmlformats'
            '.org/markup-compatibility/2006" mc:Ignorable="w14">'
            '<w:body>{corps}</w:body></w:document>')

CORE = (ENTETE + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org'
        '/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/'
        'elements/1.1/"><dc:creator>Jean-Bidule Zorglub</dc:creator>'
        '<cp:lastModifiedBy>Jean-Bidule Zorglub</cp:lastModifiedBy>'
        '<dc:title>Rapport Zorglub</dc:title></cp:coreProperties>')


def _t(texte, balise="w:t"):
    return f"<w:r><{balise}>{texte}</{balise}></w:r>"


def _p(*runs):
    return "<w:p>" + "".join(runs) + "</w:p>"


def _docx(corps, extras=None):
    """Archive .docx minimale, en memoire."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ENTETE + '<Types xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", ENTETE + '<Relationships xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/relationships"/>')
        z.writestr("word/document.xml", DOCUMENT.format(corps=corps))
        z.writestr("docProps/core.xml", CORE)
        for nom, contenu in (extras or {}).items():
            z.writestr(nom, contenu)
    return tampon.getvalue()


def _fichier(chemin, corps, extras=None):
    chemin.write_bytes(_docx(corps, extras))
    return chemin


def _lire(chemin, partie="word/document.xml"):
    with zipfile.ZipFile(chemin) as z:
        return z.read(partie).decode("utf-8")


# --------------------------------------------------------------------------- #
# Lecture du texte logique
# --------------------------------------------------------------------------- #

def test_texte_logique_traverse_les_runs():
    """Word coupe les mots ; le texte analyse doit les recoller.

    C'est le point de depart de toute la chaine : chercher « Zorglub » dans
    le XML brut ne le trouverait jamais.
    """
    xml = DOCUMENT.format(corps=_p(_t("Zorg"), _t("lub")))
    texte, segments = lire_partie(xml)
    assert texte == "Zorglub"
    assert len(segments) == 2


def test_paragraphes_et_cellules_separes():
    """Sans separateur, deux paragraphes formeraient une chaine unique."""
    xml = DOCUMENT.format(corps=_p(_t("Nom")) + _p(_t("Prenom")))
    texte, _ = lire_partie(xml)
    assert texte == "Nom\nPrenom"


def test_paragraphe_vide_rend_une_ligne_vide():
    """Sans elle, « 1510 Moudon » et « Grandvaux, le … » separes par une ligne
    blanche se lisaient comme une seule adresse sur deux lignes. Word ecrit le
    paragraphe vide « <w:p></w:p> » ; d'autres outils, « <w:p/> »."""
    for vide in ("<w:p></w:p>", "<w:p/>", '<w:p w:rsidR="00A1"/>'):
        xml = DOCUMENT.format(corps=_p(_t("1510 Moudon")) + vide
                              + _p(_t("Grandvaux, le")))
        texte, _ = lire_partie(xml)
        assert texte == "1510 Moudon\n\nGrandvaux, le", vide


def test_cellules_dun_tableau_restent_sur_une_ligne():
    """Un intitule et sa valeur sont souvent dans deux cellules voisines.

    Les separer par un saut de ligne priverait la politique des dates du
    repere « Date de naissance » qui la precede.
    """
    ligne = ("<w:tr><w:tc>" + _p(_t("Date de naissance")) + "</w:tc>"
             "<w:tc>" + _p(_t("12.03.1958")) + "</w:tc></w:tr>")
    xml = DOCUMENT.format(corps=f"<w:tbl>{ligne}{ligne}</w:tbl>")
    texte, _ = lire_partie(xml)
    assert texte == ("Date de naissance\t12.03.1958\n"
                     "Date de naissance\t12.03.1958")


def test_entites_xml_resolues():
    xml = DOCUMENT.format(corps=_p(_t("Dupont &amp; fils &lt;ici&gt;")))
    texte, _ = lire_partie(xml)
    assert texte == "Dupont & fils <ici>"


def test_texte_supprime_dune_revision_est_lu():
    """`w:delText` est invisible a l'ecran et pourtant present dans le fichier."""
    xml = DOCUMENT.format(corps=_p(_t("Ancien nom : ", "w:delText")))
    texte, _ = lire_partie(xml)
    assert texte == "Ancien nom : "


# --------------------------------------------------------------------------- #
# Reecriture
# --------------------------------------------------------------------------- #

def test_span_a_cheval_sur_deux_runs_ne_laisse_rien():
    """Le cas qui fait tout l'interet du module.

    Un remplacement pose sur le premier fragment seulement laisserait la
    seconde moitie du nom lisible en clair.
    """
    xml = DOCUMENT.format(corps=_p(_t("Zorg"), _t("lub Jean")))
    texte, segments = lire_partie(xml)
    span = Span(0, len("Zorglub"), "personne", 1.0, "Zorglub")

    produit, n = remplacer(xml, texte, segments, [span],
                           lambda s: "[PERSONNE_1]")

    assert n == 1
    assert "Zorg" not in produit
    assert lire_partie(produit)[0] == "[PERSONNE_1] Jean"


def test_reecriture_conserve_un_xml_bien_forme():
    xml = DOCUMENT.format(corps=_p(_t("Zorg"), _t("lub")))
    texte, segments = lire_partie(xml)
    produit, _ = remplacer(xml, texte, segments,
                           [Span(0, 7, "personne", 1.0, "Zorglub")],
                           lambda s: "[PERSONNE_1]")
    ElementTree.fromstring(produit)          # ne doit pas lever


def test_remplacement_reechappe_les_caracteres_speciaux():
    xml = DOCUMENT.format(corps=_p(_t("a")))
    texte, segments = lire_partie(xml)
    produit, _ = remplacer(xml, texte, segments,
                           [Span(0, 1, "personne", 1.0, "a")],
                           lambda s: "<&>")
    assert "&lt;&amp;&gt;" in produit
    assert lire_partie(produit)[0] == "<&>"


def test_espaces_de_bord_preserves():
    """Sans `xml:space`, Word mange l'espace laisse par le remplacement."""
    xml = DOCUMENT.format(corps=_p(_t("M. Dupont ")))
    texte, segments = lire_partie(xml)
    produit, _ = remplacer(xml, texte, segments,
                           [Span(3, 9, "personne", 1.0, "Dupont")],
                           lambda s: "[PERSONNE_1]")
    assert 'xml:space="preserve"' in produit
    assert lire_partie(produit)[0] == "M. [PERSONNE_1] "


def test_supprimer_element_gere_l_imbrication():
    """Une expression non gourmande laisserait une balise fermante orpheline."""
    xml = "<a><w:drawing><w:drawing/>x</w:drawing><b/><w:drawing>y</w:drawing></a>"
    produit, n = supprimer_element(xml, "w:drawing")
    assert produit == "<a><b/></a>"
    assert n == 2


def test_effacer_chaines_protege_les_identifiants_de_word():
    """Une detection numerique ne doit pas corrompre un `w:rsid`."""
    xml = f'<w:p w:rsidR="00756123"><w:t>{AVS}</w:t></w:p>'.replace(
        AVS, "00756123")
    produit = effacer_chaines(xml, {"00756123": "[NUMERO_1]"})
    assert 'w:rsidR="00756123"' in produit
    assert ">[NUMERO_1]<" in produit


def test_neutraliser_auteurs_et_proprietes():
    xml = ('<w:ins w:author="Dr Juvalier" w:initials="JJ"/>'
           '<w15:person w15:author="Jean-Bidule Zorglub"/>')
    produit = neutraliser_auteurs(xml)
    assert "Juvalier" not in produit
    assert "Zorglub" not in produit

    produit, vides = neutraliser_proprietes(CORE)
    assert vides == 3
    assert "Zorglub" not in produit
    assert "<dc:creator></dc:creator>" in produit


# --------------------------------------------------------------------------- #
# Chaine complete
# --------------------------------------------------------------------------- #

def test_caviardage_complet(tmp_path):
    """Le numero disparait du document produit, et des proprietes avec lui."""
    source = _fichier(tmp_path / "rapport.docx",
                      _p(_t("Assure "), _t("756.1234."), _t("5678.97 ici")))
    sortie = tmp_path / "sortie" / "rapport.docx"
    sortie.parent.mkdir()

    rapport = caviarder_docx(None, source, sortie)

    assert sortie.exists()
    produit = _lire(sortie)
    assert AVS not in produit
    assert "756.1234." not in produit
    assert "[NUMERO_AVS_1]" in produit
    assert rapport.fuites == []
    assert rapport.verifie
    assert rapport.entites_total == 1
    assert "Zorglub" not in _lire(sortie, "docProps/core.xml")
    assert rapport.proprietes == 3


def test_entetes_et_notes_sont_traites(tmp_path):
    """Le texte d'un en-tete vit dans une autre partie de l'archive."""
    extras = {"word/header1.xml": DOCUMENT.format(corps=_p(_t(f"AVS {AVS}"))),
              "word/footnotes.xml": DOCUMENT.format(corps=_p(_t(AVS)))}
    source = _fichier(tmp_path / "r.docx", _p(_t("Rien ici")), extras)
    sortie = tmp_path / "out.docx"

    rapport = caviarder_docx(None, source, sortie)

    assert AVS not in _lire(sortie, "word/header1.xml")
    assert AVS not in _lire(sortie, "word/footnotes.xml")
    assert rapport.fuites == []


def test_propagation_vers_les_parties_non_redigees(tmp_path):
    """Une chaine detectee dans le corps est effacee partout ailleurs."""
    extras = {"word/settings.xml": DOCUMENT.format(corps=_p(_t(AVS))),
              "word/_rels/document.xml.rels":
                  f'<Relationships><Relationship Target="mailto:{AVS}"/>'
                  '</Relationships>'}
    source = _fichier(tmp_path / "r.docx", _p(_t(f"Assure {AVS}")), extras)
    sortie = tmp_path / "out.docx"

    rapport = caviarder_docx(None, source, sortie)

    assert AVS not in _lire(sortie, "word/settings.xml")
    assert AVS not in _lire(sortie, "word/_rels/document.xml.rels")
    assert rapport.fuites == []


def test_texte_supprime_dune_revision_est_caviarde(tmp_path):
    source = _fichier(tmp_path / "r.docx", _p(_t(AVS, "w:delText")))
    sortie = tmp_path / "out.docx"

    caviarder_docx(None, source, sortie)

    assert AVS not in _lire(sortie)


def test_sortie_texte_pseudonymisee(tmp_path):
    source = _fichier(tmp_path / "r.docx", _p(_t(f"Assure {AVS}")))
    sortie = tmp_path / "out.docx"

    rapport = caviarder_docx(None, source, sortie,
                             formats=frozenset({"pdf", "texte"}))

    texte = sortie.with_suffix(".txt").read_text(encoding="utf-8")
    assert AVS not in texte
    assert "[NUMERO_AVS_1]" in texte
    assert rapport.fuites_texte == []


def test_enveloppe_autour_dun_scan_est_refusee(tmp_path):
    """Un .docx sans texte mais avec des images ne peut pas etre traite ici.

    L'anonymiser produirait un fichier qui parait traite alors que le scan
    reste parfaitement lisible : c'est l'echec que la chaine image evite.
    """
    source = _fichier(tmp_path / "scan.docx", _p(_t("Page 1")),
                      {"word/media/image1.png": b"\x89PNG"})

    with pytest.raises(ChaineImageRequise):
        caviarder_docx(None, source, tmp_path / "out.docx")


def test_logos_retire_les_images(tmp_path):
    corps = (_p(_t("Assure "), _t(AVS))
             + _p("<w:r><w:drawing><wp:inline descr='Scan'/></w:drawing></w:r>")
             + _p(_t("Suite du rapport, assez longue pour depasser le seuil "
                     "de mots au-dela duquel le document est juge vide.")))
    source = _fichier(tmp_path / "r.docx", corps,
                      {"word/media/image1.png": b"\x89PNG"})
    sortie = tmp_path / "out.docx"

    rapport = caviarder_docx(None, source, sortie,
                             zones=OptionsZones(logos=True))

    with zipfile.ZipFile(sortie) as z:
        noms = z.namelist()
    assert "word/media/image1.png" not in noms
    assert "w:drawing" not in _lire(sortie)
    assert rapport.images == 0


def test_images_conservees_sont_signalees(tmp_path):
    corps = _p(_t("Rapport de consultation du patient, texte suffisamment long "
                  "pour que le document ne soit pas pris pour une enveloppe "
                  "autour d'un scan."))
    source = _fichier(tmp_path / "r.docx", corps,
                      {"word/media/image1.png": b"\x89PNG"})

    rapport = caviarder_docx(None, source, tmp_path / "out.docx")

    assert rapport.images == 1


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

def test_relecteurs_de_coedition_sont_anonymises(tmp_path):
    """`word/people.xml` nomme en clair tous ceux qui ont ouvert le document."""
    gens = (ENTETE + '<w15:people xmlns:w15="http://schemas.microsoft.com/'
            'office/word/2012/wordml"><w15:person w15:author="Jean-Bidule '
            'Zorglub"/></w15:people>')
    source = _fichier(tmp_path / "r.docx", _p(_t(f"Assure {AVS}")),
                      {"word/people.xml": gens})

    caviarder_docx(None, source, tmp_path / "out.docx")

    assert "Zorglub" not in _lire(tmp_path / "out.docx", "word/people.xml")


def test_parties_intactes_restent_identiques_a_l_octet(tmp_path):
    """Ce qui n'a pas a changer ne doit pas changer.

    Reecrire une partie sans raison, c'est risquer de casser un document que
    Word ouvrirait sans broncher autrement.
    """
    styles = ENTETE + '<w:styles xmlns:w="x"><w:t>Normal</w:t></w:styles>'
    source = _fichier(tmp_path / "r.docx", _p(_t(f"Assure {AVS}")),
                      {"word/styles.xml": styles})
    sortie = tmp_path / "out.docx"

    caviarder_docx(None, source, sortie)

    assert _lire(sortie, "word/styles.xml") == styles


def test_xml_space_deja_present_n_est_pas_duplique():
    xml = DOCUMENT.format(
        corps='<w:p><w:r><w:t xml:space="preserve">M. Dupont </w:t></w:r></w:p>')
    texte, segments = lire_partie(xml)
    produit, _ = remplacer(xml, texte, segments,
                           [Span(3, 9, "personne", 1.0, "Dupont")],
                           lambda s: "[PERSONNE_1]")
    assert produit.count('xml:space="preserve"') == 1


def test_verification_voit_un_nom_coupe_en_deux():
    """Le controle porte sur le texte logique, pas sur le XML brut.

    Une recherche dans les octets du fichier passerait a cote du cas le plus
    frequent : le numero reparti sur deux noeuds.
    """
    octets = _docx(_p(_t("756.1234."), _t("5678.97")))
    assert verifier_octets(octets, [AVS]) == [AVS]


def test_verification_voit_un_reste_dans_un_attribut():
    octets = _docx(_p("<w:r><w:drawing "
                      f"descr=\"{AVS}\"/></w:r>"))
    assert verifier_octets(octets, [AVS]) == [AVS]


def test_verification_ne_signale_rien_sur_un_document_propre():
    octets = _docx(_p(_t("[NUMERO_AVS_1]")))
    assert verifier_octets(octets, [AVS]) == []


# --------------------------------------------------------------------------- #
# Propagation insensible a la mise en page
# --------------------------------------------------------------------------- #

def test_propagation_trouve_un_nom_en_lettres_espacees():
    """Un pied de page compose lettre par lettre doit etre atteint.

    Cas releve sur un rapport reel : l'en-tete portait « Cabinet d'imagerie de
    la Combe », masque correctement, mais le pied de page repetait le meme nom
    en lettres espacees. La propagation litterale n'y voyait rien, tandis que
    la verification finale — qui compare en ignorant les blancs — le signalait
    comme une fuite a chaque execution.
    """
    from caviardoc.documents.commun import propager_chaines

    texte = ("Cabinet d'imagerie de la Combe\n\n"
             "C a b i n e t  d ' i m a g e r i e  d e  l a  C o m b e")
    spans = propager_chaines(texte, [], {"Cabinet d'imagerie de la Combe": "employeur"})
    assert len(spans) == 2, "les deux occurrences doivent etre trouvees"
    for sp in spans:
        assert texte[sp.start:sp.end] == sp.text
        assert "".join(sp.text.split()).lower() == "cabinetd'imageriedelacombe"


def test_propagation_ignore_la_casse():
    from caviardoc.documents.commun import propager_chaines

    texte = "ZORGLUB ici, Zorglub la"
    spans = propager_chaines(texte, [], {"Zorglub": "personne"})
    assert len(spans) == 2


def test_compacter_preserve_les_offsets():
    from caviardoc.documents.commun import compacter

    texte = "a b\nc"
    compact, positions = compacter(texte)
    assert compact == "abc"
    assert [texte[i] for i in positions] == ["a", "b", "c"]


def test_propagation_conserve_le_texte_reel_pas_l_aiguille():
    """Le span doit porter le texte tel qu'il figure dans le document."""
    from caviardoc.documents.commun import propager_chaines

    texte = "vu par Z O R G L U B hier"
    span, = propager_chaines(texte, [], {"Zorglub": "personne"})
    assert span.text == "Z O R G L U B"
