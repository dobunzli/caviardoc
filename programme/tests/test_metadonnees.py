"""Metadonnees : ce qu'un document porte sans l'afficher.

Auteur, titre, dates, proprietes personnalisees, vignette d'apercu, donnees
liees aux champs, variables de document, chemin du modele, EXIF et GPS des
photos. Rien de cela n'est du texte visible : ni la detection ni la relecture
finale ne le voyaient, et tout passait tel quel dans le .docx produit.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageCms, PngImagePlugin  # noqa: E402

from caviardoc.documents.docx import caviarder_docx  # noqa: E402
from caviardoc.documents.metadonnees import nettoyer_image  # noqa: E402
from test_docx import AVS, ENTETE, _docx, _p, _t  # noqa: E402

SECRET = "Dossier PAT-48213 de Jean Dupont"


def _photo():
    image = Image.new("RGB", (64, 48), "white")
    for x in range(64):
        image.putpixel((x, x % 48), (x * 4, 10, 200))
    exif = Image.Exif()
    exif[0x013B] = "Dr Paul Tartempion"          # auteur
    exif[0x010E] = SECRET                     # description
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (46.0, 59.0, 30.0)
    return image, exif


def _pixels(octets):
    image = Image.open(io.BytesIO(octets))
    return image.tobytes()


class TestImages:
    @pytest.mark.parametrize("progressif", [False, True])
    def test_jpeg_sans_exif_ni_gps_aux_memes_pixels(self, progressif):
        image, exif = _photo()
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        tampon = io.BytesIO()
        image.save(tampon, "JPEG", exif=exif.tobytes(), icc_profile=icc,
                   comment=SECRET.encode(), progressive=progressif)
        source = tampon.getvalue()

        r = nettoyer_image(source)
        relu = Image.open(io.BytesIO(r.octets))
        assert b"Dupont" not in r.octets and b"Tartempion" not in r.octets
        assert not relu.getexif()
        # Rien n'est reencode : memes pixels, et le profil de couleur reste.
        assert _pixels(r.octets) == _pixels(source)
        assert relu.info.get("icc_profile") == icc

    def test_ce_qui_suit_la_fin_de_l_image_part(self):
        """Les telephones accolent des vignettes apres la fin de l'image."""
        image, _ = _photo()
        tampon = io.BytesIO()
        image.save(tampon, "JPEG")
        source = tampon.getvalue() + b"\xff\xd8" + SECRET.encode() + b"\xff\xd9"
        r = nettoyer_image(source)
        assert SECRET.encode() not in r.octets
        assert _pixels(r.octets) == _pixels(tampon.getvalue())

    def test_png_sans_textes_ni_exif(self):
        image, exif = _photo()
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "Jean Dupont")
        info.add_itxt("Description", SECRET)
        tampon = io.BytesIO()
        image.save(tampon, "PNG", pnginfo=info, exif=exif.tobytes())
        r = nettoyer_image(tampon.getvalue())
        assert b"Dupont" not in r.octets
        assert Image.open(io.BytesIO(r.octets)).info == {}
        assert _pixels(r.octets) == _pixels(tampon.getvalue())

    def test_un_format_inconnu_est_rendu_tel_quel_et_signale(self):
        tampon = io.BytesIO()
        Image.new("RGB", (8, 8)).save(tampon, "GIF")
        r = nettoyer_image(tampon.getvalue())
        assert r.non_pris_en_charge and r.octets == tampon.getvalue()

    def test_un_jpeg_tronque_n_est_pas_abime(self):
        image, exif = _photo()
        tampon = io.BytesIO()
        image.save(tampon, "JPEG", exif=exif.tobytes())
        coupe = tampon.getvalue()[:200]
        r = nettoyer_image(coupe)
        assert r.non_pris_en_charge and r.octets == coupe


# --------------------------------------------------------------------- .docx

TYPES = (ENTETE + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
         'content-types"><Default Extension="jpeg" ContentType="image/jpeg"/>'
         '<Override PartName="/docProps/custom.xml" ContentType="application/'
         'vnd.openxmlformats-officedocument.custom-properties+xml"/>'
         '<Override PartName="/customXml/itemProps1.xml" ContentType="application/'
         'vnd.openxmlformats-officedocument.customXmlProperties+xml"/></Types>')
RELS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
TYPE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RACINE_RELS = (ENTETE + f'<Relationships {RELS}>'
               f'<Relationship Id="r1" Type="{TYPE_REL}officeDocument" '
               'Target="word/document.xml"/>'
               '<Relationship Id="r2" Type="http://schemas.openxmlformats.org/package/'
               '2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.jpeg"/>'
               f'<Relationship Id="r3" Type="{TYPE_REL}custom-properties" '
               'Target="docProps/custom.xml"/></Relationships>')
DOCUMENT_RELS = (ENTETE + f'<Relationships {RELS}>'
                 f'<Relationship Id="rX" Type="{TYPE_REL}customXml" '
                 'Target="../customXml/item1.xml"/>'
                 f'<Relationship Id="rL" Type="{TYPE_REL}hyperlink" '
                 'Target="file:///C:/Patients/Dupont%20Jean/bilan.pdf" '
                 'TargetMode="External"/></Relationships>')
REGLAGES = (ENTETE + '<w:settings xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships"><w:zoom w:percent="100"/>'
            '<w:attachedTemplate r:id="rT"/>'
            '<w:mailMerge><w:query w:val="SELECT * FROM patients"/></w:mailMerge>'
            f'<w:docVars><w:docVar w:name="Patient" w:val="{SECRET}"/></w:docVars>'
            '</w:settings>')
REGLAGES_RELS = (ENTETE + f'<Relationships {RELS}>'
                 f'<Relationship Id="rT" Type="{TYPE_REL}attachedTemplate" '
                 'Target="file:///C:/Users/jdupont/Modeles/Rapport.dotx" '
                 'TargetMode="External"/></Relationships>')
CORE_DATE = (ENTETE + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org'
             '/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/'
             'elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
             '<dc:creator>Dr Paul Tartempion</dc:creator>'
             '<dcterms:created xsi:type="dcterms:W3CDTF">2024-05-04T10:12:00Z'
             '</dcterms:created></cp:coreProperties>')
APP = (ENTETE + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
       '2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/'
       'officeDocument/2006/docPropsVTypes"><Template>Dupont.dotx</Template>'
       f'<TitlesOfParts><vt:vector size="1" baseType="lpstr"><vt:lpstr>{SECRET}'
       '</vt:lpstr></vt:vector></TitlesOfParts></Properties>')
CUSTOM = (ENTETE + '<Properties xmlns="http://schemas.openxmlformats.org/'
          'officeDocument/2006/custom-properties"><property name="NoDossier">'
          '<lpwstr>PAT-48213</lpwstr></property></Properties>')
LIAISON = ('<w:sdt><w:sdtPr><w:dataBinding w:xpath="/dossier/patient" '
           'w:storeItemID="{1234}"/></w:sdtPr><w:sdtContent>')


@pytest.fixture
def produit(tmp_path):
    image, exif = _photo()
    photo = io.BytesIO()
    image.save(photo, "JPEG", exif=exif.tobytes())
    corps = (_p(_t("Assure "), _t(AVS), _t(" vu en consultation."))
             + _p(_t("Le controle annuel est sans particularite ; la tension est "
                     "normale et le traitement est poursuivi sans changement."))
             + _p(_t("Nouveau controle dans un an, ou plus tot si besoin."))
             + LIAISON + _p(_t("[nom du patient]")) + "</w:sdtContent></w:sdt>")
    source = tmp_path / "piege.docx"
    source.write_bytes(_docx(corps, extras={
        "docProps/thumbnail.jpeg": photo.getvalue(),
        "docProps/custom.xml": CUSTOM,
        "customXml/item1.xml": f"<dossier><patient>{SECRET}</patient></dossier>",
        "customXml/itemProps1.xml": ENTETE + "<ds:datastoreItem/>",
        "customXml/_rels/item1.xml.rels": ENTETE + f"<Relationships {RELS}/>",
        "word/media/image1.jpeg": photo.getvalue(),
        "word/settings.xml": REGLAGES,
        "word/_rels/settings.xml.rels": REGLAGES_RELS,
        "word/_rels/document.xml.rels": DOCUMENT_RELS,
        "docProps/app.xml": APP,
    }))
    # `_docx` ecrit ses propres types, relations et proprietes : on remplace.
    contenu = {}
    with zipfile.ZipFile(source) as z:
        for nom in z.namelist():
            contenu[nom] = z.read(nom)
    contenu["[Content_Types].xml"] = TYPES.encode()
    contenu["_rels/.rels"] = RACINE_RELS.encode()
    contenu["docProps/core.xml"] = CORE_DATE.encode()
    with zipfile.ZipFile(source, "w") as z:
        for nom, octets in contenu.items():
            z.writestr(nom, octets)

    sortie = tmp_path / "out.docx"
    rapport = caviarder_docx(None, source, sortie)
    with zipfile.ZipFile(sortie) as z:
        parties = {nom: z.read(nom) for nom in z.namelist()}
    return rapport, parties


class TestDocx:
    def test_aucune_trace_ne_subsiste(self, produit):
        _, parties = produit
        for nom, octets in parties.items():
            for trace in (b"Dupont", b"PAT-48213", b"Tartempion", b"jdupont",
                          b"2024-05-04"):
                assert trace not in octets, (nom, trace)

    def test_les_parties_de_metadonnees_sont_retirees(self, produit):
        _, parties = produit
        assert "docProps/thumbnail.jpeg" not in parties
        assert "docProps/custom.xml" not in parties
        assert not [n for n in parties if n.startswith("customXml/")]

    def test_aucune_relation_ne_pointe_dans_le_vide(self, produit):
        _, parties = produit
        assert b"thumbnail" not in parties["_rels/.rels"]
        assert b"custom-properties" not in parties["_rels/.rels"]
        assert b"customXml" not in parties["word/_rels/document.xml.rels"]
        assert b"/docProps/custom.xml" not in parties["[Content_Types].xml"]
        assert b"/customXml/" not in parties["[Content_Types].xml"]

    def test_reglages_et_liaisons(self, produit):
        _, parties = produit
        reglages = parties["word/settings.xml"]
        for element in (b"w:docVars", b"w:mailMerge", b"w:attachedTemplate"):
            assert element not in reglages
        assert b'<w:zoom w:percent="100"/>' in reglages     # la mise en page reste
        assert b"attachedTemplate" not in parties["word/_rels/settings.xml.rels"]
        assert b"w:dataBinding" not in parties["word/document.xml"]

    def test_un_lien_vers_un_fichier_local_perd_son_chemin(self, produit):
        _, parties = produit
        rels = parties["word/_rels/document.xml.rels"]
        assert b"C:/Patients" not in rels and b'Target="file:///"' in rels

    def test_la_photo_reste_mais_sans_exif(self, produit):
        _, parties = produit
        photo = Image.open(io.BytesIO(parties["word/media/image1.jpeg"]))
        assert not photo.getexif()

    def test_le_compte_rendu_dit_ce_qui_est_parti(self, produit):
        rapport, _ = produit
        dit = " ".join(rapport.metadonnees)
        for mot in ("vignette", "personnalisées", "liées", "variables",
                    "modèle", "dates", "image"):
            assert mot in dit, mot


def test_le_pdf_produit_ne_recopie_rien(tmp_path):
    """La chaine image reconstruit le PDF a partir des pixels : rien de
    l'original — proprietes, XMP, pieces jointes, notes, signets — ne passe."""
    pymupdf = pytest.importorskip("pymupdf")
    from caviardoc.documents.caviardage import caviarder_pdf

    d = pymupdf.open()
    page = d.new_page()
    # Assez de mots pour que la couche texte soit lue telle quelle : le test
    # porte sur la structure du PDF produit, et doit tourner sans Tesseract.
    page.insert_text((72, 72), f"Assure {AVS}, controle annuel sans particularite.")
    page.insert_text((72, 90), "La tension arterielle est normale, le traitement "
                     "est poursuivi sans changement et un nouveau controle est "
                     "prevu dans un an.")
    d.set_metadata({"author": "Dr Paul Tartempion", "title": SECRET,
                    "keywords": "Dupont"})
    d.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'>{SECRET}</x:xmpmeta>")
    d.embfile_add("dupont.txt", SECRET.encode())
    note = page.add_text_annot((300, 300), SECRET)
    note.update()
    d.set_toc([[1, SECRET, 1]])
    source = tmp_path / "piege.pdf"
    d.save(source)

    caviarder_pdf(None, source, tmp_path / "out.pdf", verifier=False)
    produit = pymupdf.open(tmp_path / "out.pdf")
    # Rien du document d'origine : seulement les valeurs neutres de l'outil.
    from caviardoc.documents.caviardage import metadonnees_pdf
    assert {k: v for k, v in produit.metadata.items()
            if v and k != "format"} == metadonnees_pdf()
    assert not produit.get_xml_metadata()
    assert produit.embfile_count() == 0
    assert produit.get_toc() == []
    assert not list(produit[0].annots())
    assert b"Dupont" not in (tmp_path / "out.pdf").read_bytes()
