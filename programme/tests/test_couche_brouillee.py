"""Couche texte a encodage decale, et propagation des detections douteuses."""
from __future__ import annotations

from caviardoc.documents import caviardage
from caviardoc.documents.ocr import MotOCR, PageOCR
from caviardoc.engine import Origine, Span
from caviardoc.pipeline import propager

PHRASE = ("Le patient doit prendre deux comprimes par jour pendant une semaine "
          "avec un grand verre d'eau, puis revenir nous voir pour un controle "
          "de la tension et du poids avant la fin du mois prochain. ")


def _decale(texte: str) -> str:
    """Simule une police a encodage decale : chaque lettre glisse de 7 rangs."""
    return "".join(chr(ord(c) + 7) if c.isalpha() and c.isascii() else c
                   for c in texte)


def _page(mots_couche: list[str], mots_ocr: list[str]) -> tuple[PageOCR, PageOCR]:
    def faire(mots):
        page = PageOCR()
        for i, t in enumerate(mots):
            page.mots.append(MotOCR(t, 10 * i, 0, 10 * i + 9, 10, 90, 1, 1))
        page.texte = " ".join(mots)
        return page
    return faire(mots_couche), faire(mots_ocr)


def test_heuristique_detecte_une_couche_decalee():
    assert caviardage._couche_brouillee_par_les_mots(_decale(PHRASE * 2))


def test_heuristique_laisse_le_francais_normal():
    assert not caviardage._couche_brouillee_par_les_mots(PHRASE * 2)


def test_heuristique_ne_juge_pas_un_texte_trop_court():
    assert not caviardage._couche_brouillee_par_les_mots(_decale("Le patient"))


def test_accord_ocr_couche_decalee():
    vrais = (PHRASE * 2).split()
    couche, ocr = _page([_decale(m) for m in vrais], vrais)
    assert caviardage._couche_en_desaccord_avec_l_ocr(couche, ocr)


def test_accord_ocr_couche_saine_casse_et_accents():
    vrais = (PHRASE * 2).split()
    couche, ocr = _page(vrais, [m.upper().replace("E", "É") if i % 2 else m
                                for i, m in enumerate(vrais)])
    assert not caviardage._couche_en_desaccord_avec_l_ocr(couche, ocr)


def test_accord_ocr_trop_peu_de_mots_comparables():
    couche, ocr = _page(["xqzv", "wkjh"], ["patient", "semaine"])
    assert not caviardage._couche_en_desaccord_avec_l_ocr(couche, ocr)


def test_lire_page_ecarte_la_couche_decalee(monkeypatch):
    vrais = (PHRASE * 2).split()
    couche, ocr = _page([_decale(m) for m in vrais], vrais)
    monkeypatch.setattr(caviardage, "mots_depuis_couche_texte",
                        lambda page, dpi: couche)
    monkeypatch.setattr(caviardage, "_rendu", lambda page, dpi: object())
    monkeypatch.setattr(caviardage, "ocr_page", lambda *a, **k: ocr)
    monkeypatch.setattr(caviardage, "couche_invisible", lambda page: False)
    # Ni la couche ni son heuristique ne doivent suffire : on force l'OCR.
    monkeypatch.setattr(caviardage, "_couche_brouillee_par_les_mots",
                        lambda t: False)
    lecture, source = caviardage._lire_page(object(), 300, "fra")
    assert source == "OCR" and lecture is ocr


# --- propagation ------------------------------------------------------------

def _sp(texte, label, score, origine=Origine.MODELE.value):
    return Span(0, len(texte), label, score, texte, origine)


def test_propagation_ignore_un_doute_du_modele():
    texte = "Zorglub puis Zorglub encore Zorglub"
    assert propager(texte, [_sp("Zorglub", "personne", 0.3)]) == \
        [_sp("Zorglub", "personne", 0.3)]


def test_propagation_garde_un_modele_sur_et_une_regle():
    # La detection d'origine plus ses deux occurrences retrouvees.
    texte = "Zorglub puis Zorglub"
    assert len(propager(texte, [_sp("Zorglub", "personne", 0.9)])) == 3
    regle = _sp("Zorglub", "personne", 0.2, Origine.REGLE.value)
    assert len(propager(texte, [regle])) == 3


def test_propagation_ignore_une_chaine_illisible():
    texte = "@RVROR•Lü et @RVROR•Lü"
    assert len(propager(texte, [_sp("@RVROR•Lü", "personne", 0.9)])) == 1


def test_propagation_ignore_un_faux_courriel():
    texte = "UüFRPPIQZIWL et UüFRPPIQZIWL"
    assert len(propager(texte, [_sp("UüFRPPIQZIWL", "email", 0.9)])) == 1


def test_propagation_garde_un_vrai_courriel():
    texte = "ecrire a jean.dupont@example.org ou jean.dupont@example.org"
    assert len(propager(texte, [_sp("jean.dupont@example.org", "email",
                                    0.9)])) == 3


# --- propagation d'une page a l'autre ----------------------------------------

def test_un_doute_du_modele_ne_se_propage_pas_aux_autres_pages(tmp_path,
                                                                  monkeypatch):
    """Lu a 0,19 en page 1, il revenait en page 2 a 1,0, origine « propage »."""
    pymupdf = __import__("pytest").importorskip("pymupdf")
    numero = "ZX4471-Q"

    def _document(chemin):
        d = pymupdf.open()
        for entete in ("Premiere page", "Seconde page"):
            p = d.new_page()
            p.insert_text((72, 80), entete, fontsize=11)
            p.insert_text((72, 120), f"Reference {numero} du laboratoire.",
                          fontsize=11)
            # Assez de texte pour que la couche soit lue, sans OCR.
            for i, ligne in enumerate(PHRASE.split(", ")):
                p.insert_text((72, 160 + 20 * i), ligne, fontsize=11)
        d.save(chemin)

    def _rapport(score):
        def _detection(model, texte, **kw):
            if "Premiere" not in texte:
                return [], []
            i = texte.index(numero)
            sp = Span(i, i + len(numero), "numero de dossier", score, numero,
                      Origine.MODELE.value)
            return [sp], [sp]
        monkeypatch.setattr(caviardage, "analyse_detaillee", _detection)
        monkeypatch.setattr(caviardage, "completer_par_la_page",
                            lambda *a, **k: 0)
        monkeypatch.setattr(caviardage, "ocr_page", lambda *a, **k: PageOCR())
        return caviardage.caviarder_pdf(None, tmp_path / "doc.pdf",
                                        tmp_path / "out.pdf", verifier=False)

    _document(tmp_path / "doc.pdf")
    douteux = _rapport(0.19)
    assert not any(numero in sp.text for sp in douteux.pages[1].entites)
    assert any(numero in sp.text for sp in douteux.pages[0].entites)
    sur = _rapport(0.9)
    assert any(numero in sp.text for sp in sur.pages[1].entites)
