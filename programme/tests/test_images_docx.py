"""Les images incorporees d'un .docx : lisibles apres traitement, et sans alerte.

Un rapport colle en capture d'ecran dans une demande de consilium traversait la
chaine bureautique intact. Le nom de la patiente, sa date de naissance, son
numero AVS et son adresse restaient parfaitement lisibles dans le document
produit — et la verification, qui cherchait les chaines interdites dans les
parties XML de l'archive, ou un pixel ne dit rien, annoncait « succes ».

C'est la meme forme de defaut que les pages pivotees : une surface entiere qui
passe au travers, et un signalement qui rassure au lieu d'alerter. Le seul
recours, `--logos`, retirait toutes les images sans distinction.

Ces tests n'ont pas besoin du modele : un numero AVS et une date sont reconnus
par les regles de la couche 1, ce qui suffit a mesurer ce qui est en cause — la
lecture de l'image, la geometrie des boites, et la relecture. La propagation
depuis le texte du document est testee en passant les chaines connues
directement.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Image = pytest.importorskip("PIL.Image")
pytest.importorskip("pytesseract")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
ImageFont = pytest.importorskip("PIL.ImageFont")

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents.docx import caviarder_docx, verifier_sortie  # noqa: E402
from caviardoc.documents.images import (_noircir,  # noqa: E402
                                      caviarder_image, fuites_dans_images)
from caviardoc.documents.ocr import ocr_image  # noqa: E402
from caviardoc.documents.zones import OptionsZones  # noqa: E402

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

ENTETE = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
DOCUMENT = (ENTETE + '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>{corps}</w:body></w:document>')

#: Assez de mots pour que le document ne soit pas pris pour une enveloppe
#: autour d'un scan, cas ou la chaine refuse de traiter.
CORPS = ("Demande de consilium pour un avis rhumatologique concernant des "
         "douleurs lombaires persistantes evoluant depuis plusieurs mois "
         "malgre un traitement antalgique bien conduit. Rapport precedent "
         "joint en capture d'ecran ci-dessous.")

AVS = "756.1234.5678.97"
DATE = "12.03.1958"
NOM = "Martine Jaquier"


# --------------------------------------------------------------------------- #
# Fabrication
# --------------------------------------------------------------------------- #

def _image(lignes: list[str], format: str = "PNG",
           mode: str = "RGB", **enregistrement) -> bytes:
    """Une capture d'ecran : du texte, en pixels, assez gros pour etre relu."""
    img = Image.new(mode, (1100, 90 + 70 * len(lignes)), "white")
    dessin = ImageDraw.Draw(img)
    fonte = ImageFont.load_default(size=44)
    for i, ligne in enumerate(lignes):
        dessin.text((30, 25 + i * 70), ligne, fill="black", font=fonte)
    tampon = io.BytesIO()
    img.save(tampon, format=format, **enregistrement)
    return tampon.getvalue()


def _reencode(octets: bytes) -> bytes:
    """Les octets qu'un simple aller-retour par l'encodeur produirait."""
    image = Image.open(io.BytesIO(octets))
    tampon = io.BytesIO()
    image.save(tampon, format=image.format)
    return tampon.getvalue()


def _fichier(chemin: Path, media: dict[str, bytes] | None = None,
             corps: str = CORPS) -> Path:
    """Archive .docx minimale portant du texte et des images."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ENTETE + '<Types xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", ENTETE + '<Relationships xmlns="http://'
                   'schemas.openxmlformats.org/package/2006/relationships"/>')
        z.writestr("word/document.xml",
                   DOCUMENT.format(corps=f"<w:p><w:r><w:t>{corps}</w:t>"
                                         "</w:r></w:p>"))
        for nom, octets in (media or {}).items():
            z.writestr(nom, octets)
    chemin.write_bytes(tampon.getvalue())
    return chemin


def _media(chemin: Path, nom: str = "word/media/image1.png") -> bytes:
    with zipfile.ZipFile(chemin) as z:
        return z.read(nom)


def _relu(octets: bytes) -> str:
    """Le texte que l'on lit dans une image, blancs et casse ecartes."""
    image = Image.open(io.BytesIO(octets)).convert("RGB")
    return "".join(ocr_image(image, langue="fra").texte.split()).lower()


def _lisible(octets: bytes, chaine: str) -> bool:
    return "".join(chaine.split()).lower() in _relu(octets)


# --------------------------------------------------------------------------- #
# Ce qui ne depend pas de Tesseract
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_un_aplat_noircit_les_pixels_designes(format):
    """La geometrie et le format, verifies sans reconnaissance optique.

    Ce test et celui de l'image illisible sont les seuls du fichier que
    l'integration continue execute : elle n'installe pas Tesseract. Ils valent
    donc pour Windows et pour Linux, que le poste de developpement n'a pas — et
    ce sont eux qui garantissent que l'aplat tombe bien la ou on le demande,
    l'erreur commise sur les pages pivotees.

    `_noircir` est prive ; c'est neanmoins lui qui porte la promesse en pixels,
    et le seul moyen de l'eprouver sans lire une image.
    """
    octets = _image(["Peu importe ce qui est ecrit"], format=format)
    image = Image.open(io.BytesIO(octets))
    image.load()

    produits = _noircir(image, [(100.0, 20.0, 300.0, 60.0)])

    relu = Image.open(io.BytesIO(produits))
    assert relu.format == format, "le format d'origine doit etre conserve"
    gris = relu.convert("L")
    assert gris.getpixel((200, 40)) < 40, "l'interieur de la boite est noir"
    assert gris.getpixel((900, 40)) > 200, "le reste de l'image ne bouge pas"


# --------------------------------------------------------------------------- #
# Le controle : sans lui, tout ce qui suit pourrait etre vacant
# --------------------------------------------------------------------------- #

@besoin_ocr
def test_l_image_fabriquee_est_bien_lisible():
    """Si l'OCR ne lit rien de la source, aucun test suivant ne prouve rien."""
    octets = _image([f"Nom : {NOM}", f"Nee le {DATE}", f"AVS {AVS}"])
    assert _lisible(octets, AVS), "le numero AVS doit se lire dans la source"
    assert _lisible(octets, DATE)


# --------------------------------------------------------------------------- #
# Caviardage
# --------------------------------------------------------------------------- #

@besoin_ocr
def test_le_texte_d_une_capture_est_masque(tmp_path):
    """Le defaut lui-meme : ce que porte l'image doit disparaitre du produit."""
    source = _fichier(tmp_path / "r.docx", {
        "word/media/image1.png": _image([f"Nee le {DATE}", f"AVS {AVS}"])})

    rapport = caviarder_docx(None, source, tmp_path / "out.docx",
                             formats=frozenset({"docx"}))

    produite = _media(tmp_path / "out.docx")
    assert not _lisible(produite, AVS), "le numero AVS reste lisible"
    assert not _lisible(produite, DATE), "la date de naissance reste lisible"
    assert rapport.images_masquees == 1
    assert rapport.images_lues[0].boites > 0


@besoin_ocr
def test_les_boites_couvrent_les_pixels_du_mot(tmp_path):
    """Une boite doit recouvrir des pixels sombres : ceux du mot lui-meme.

    Un rectangle pose a cote noircirait du blanc et laisserait le texte
    intact — exactement ce que faisaient les pages pivotees.
    """
    octets = _image([f"AVS {AVS}"])
    produits, compte_rendu = caviarder_image(None, "image1.png", octets)

    avant = Image.open(io.BytesIO(octets)).convert("L")
    apres = Image.open(io.BytesIO(produits)).convert("L")
    sombres_avant = sum(1 for v in avant.tobytes() if v < 128)
    sombres_apres = sum(1 for v in apres.tobytes() if v < 128)

    assert compte_rendu.boites > 0
    assert sombres_apres > sombres_avant, ("les aplats noirs doivent ajouter "
                                           "des pixels sombres")
    assert not _lisible(produits, AVS)


@besoin_ocr
def test_une_chaine_connue_du_texte_est_masquee_dans_l_image():
    """Le nom vient du corps du document, pas de la lecture de l'image.

    Aucune regle ne reconnait « Martine Jaquier », et le modele n'est pas charge
    ici : si le nom disparait, c'est par propagation — la seule voie qui ne
    depende pas de ce que le modele arrive a lire d'une capture de trois lignes.
    """
    octets = _image([f"Nom : {NOM}", "Service de rhumatologie"])

    produits, compte_rendu = caviarder_image(
        None, "image1.png", octets, connues={NOM: "personne"})

    assert not _lisible(produits, NOM)
    assert compte_rendu.boites > 0


@besoin_ocr
def test_une_image_sans_rien_a_masquer_n_est_pas_touchee():
    """Un logo ou une courbe traverse la chaine sans une recompression.

    Le JPEG de qualite 97 n'est pas un detail : reenregistre par defaut, il
    perdrait de l'information et changerait de taille. C'est ce qui rend
    l'egalite ci-dessous verifiable — en PNG, un aller-retour rend les memes
    octets et l'assertion serait vraie sans rien prouver.
    """
    octets = _image(["Laboratoire de chimie clinique",
                     "Valeurs de reference"], format="JPEG", quality=97)
    assert _reencode(octets) != octets, ("le controle est vain si l'encodeur "
                                         "rend les memes octets")

    produits, compte_rendu = caviarder_image(None, "logo.jpeg", octets)

    assert produits == octets, "les octets d'origine doivent etre recopies"
    assert compte_rendu.boites == 0
    assert compte_rendu.lue
    assert compte_rendu.mots_reconnus > 0


@besoin_ocr
@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_le_format_de_l_image_est_conserve(format):
    """L'archive nomme l'image par son extension et declare son type.

    Un PNG reecrit sous un nom en .jpeg est une image que Word n'affiche plus.
    """
    octets = _image([f"AVS {AVS}"], format=format)

    produits, compte_rendu = caviarder_image(None, f"image1.{format}", octets)

    assert compte_rendu.boites > 0
    assert Image.open(io.BytesIO(produits)).format == format


@besoin_ocr
def test_un_jpeg_masque_garde_ses_tables_de_quantification():
    """Reencoder a la qualite par defaut ferait perdre une generation.

    Un rapport scanne traite deux fois deviendrait visiblement plus flou, sans
    que rien ne l'annonce. Le controle vient d'abord : si un aller-retour par
    l'encodeur rendait les memes tables, l'assertion ne prouverait rien.
    """
    octets = _image([f"AVS {AVS}"], format="JPEG", quality=97)
    tables = Image.open(io.BytesIO(octets)).quantization
    assert Image.open(io.BytesIO(_reencode(octets))).quantization != tables

    produits, compte_rendu = caviarder_image(None, "image1.jpeg", octets)

    assert compte_rendu.boites > 0, "sans aplat, rien ne serait reencode"
    assert Image.open(io.BytesIO(produits)).quantization == tables


def test_une_image_illisible_est_signalee_et_conservee(tmp_path):
    """Ce que la chaine ne sait pas ouvrir, elle le dit — et n'y touche pas.

    C'est le cas ou l'ancien avertissement garde tout son sens : un
    metafichier Windows, une animation, un fichier tronque.
    """
    source = _fichier(tmp_path / "r.docx",
                      {"word/media/image1.png": b"\x89PNG tronque"})

    rapport = caviarder_docx(None, source, tmp_path / "out.docx",
                             formats=frozenset({"docx"}))

    assert rapport.images == 1
    assert rapport.images_illisibles == 1
    assert rapport.images_lues[0].illisible
    assert _media(tmp_path / "out.docx") == b"\x89PNG tronque"


@besoin_ocr
def test_logos_dispense_de_lire_les_images(tmp_path):
    """Une image retiree n'a pas a etre lue : elle ne sera pas dans l'archive."""
    source = _fichier(tmp_path / "r.docx", {
        "word/media/image1.png": _image([f"AVS {AVS}"])})

    rapport = caviarder_docx(None, source, tmp_path / "out.docx",
                             formats=frozenset({"docx"}),
                             zones=OptionsZones(logos=True))

    with zipfile.ZipFile(tmp_path / "out.docx") as z:
        assert "word/media/image1.png" not in z.namelist()
    assert rapport.images_lues == []


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

@besoin_ocr
def test_la_verification_relit_les_images(tmp_path):
    """Sur un document non traite, les chaines de l'image doivent etre vues.

    Elles ne figurent nulle part dans le XML : si la verification les signale,
    c'est qu'elle a relu les pixels. C'est le controle qui manquait — sans lui,
    « aucune fuite » sur un document traite ne veut rien dire.
    """
    source = _fichier(tmp_path / "brut.docx", {
        "word/media/image1.png": _image([f"Nom : {NOM}", f"AVS {AVS}"])})

    fuites = verifier_sortie(source, {NOM: "personne", AVS: "avs"})

    assert set(fuites) == {NOM, AVS}


@besoin_ocr
def test_une_fuite_nomme_l_image_ou_elle_subsiste():
    """« Cette date subsiste » n'aide personne ; « dans image2.png » se verifie."""
    medias = {"word/media/image1.png": _image(["Courbe de laboratoire"]),
              "word/media/image2.png": _image([f"AVS {AVS}"])}

    par_chaine = fuites_dans_images(medias, {AVS: "avs"})

    assert par_chaine == {AVS: ["word/media/image2.png"]}


@besoin_ocr
def test_un_document_traite_ne_signale_plus_rien(tmp_path):
    """La boucle complete : on masque, on relit, et il ne reste rien."""
    source = _fichier(tmp_path / "r.docx", {
        "word/media/image1.png": _image([f"Nee le {DATE}", f"AVS {AVS}"])})

    rapport = caviarder_docx(None, source, tmp_path / "out.docx",
                             formats=frozenset({"docx"}))

    assert rapport.verifie
    assert rapport.fuites == []
    assert rapport.images_lues[0].fuites == []
    assert rapport.succes
