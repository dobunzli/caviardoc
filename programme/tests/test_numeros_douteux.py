"""Numeros que le modele ne tient qu'a peine : recadres ou ecartes.

Les cas viennent d'un courrier d'orthopedie reel. Le texte ci-dessous en
reprend la forme, sans aucune donnee.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.pipeline import filtrer_numeros_douteux  # noqa: E402

TEXTE = ("Consolidation de la fracture C2, reclinaison reduite. "
         "Antérolisthèse de Mayerling degré I, L4 sur L5, sans glissement. "
         "Une décompensation\nmusculaire avec douleurs. Dossier 48213/07.")


def _span(sous, label, score, origine=Origine.MODELE.value, debut_dans=None):
    i = TEXTE.index(sous) if debut_dans is None else debut_dans
    return Span(i, i + len(sous), label, score, sous, origine)


def _textes(spans):
    return [sp.text for sp in spans]


def test_un_fragment_de_mot_est_recadre_sur_le_numero():
    """« fractur[e C2] » : le modele a pris la fin du mot qui precede."""
    sp = _span("e C2", "numero de patient", 0.25, debut_dans=TEXTE.index("fracture") + 7)
    assert sp.text == "e C2"
    # Un niveau vertebral n'est pas un numero : une fois recadre, il disparait.
    assert filtrer_numeros_douteux(TEXTE, [sp]) == []


def test_un_fragment_de_mot_sans_chiffre_disparait():
    sp = _span("n\nmusculaire", "numero de patient", 0.24)
    assert filtrer_numeros_douteux(TEXTE, [sp]) == []


def test_des_niveaux_vertebraux_ne_sont_pas_des_numeros_de_dossier():
    sp = _span("I, L4 sur L5", "numero de dossier", 0.37)
    assert filtrer_numeros_douteux(TEXTE, [sp]) == []


def test_un_numero_vraisemblable_a_faible_confiance_reste_masque():
    sp = _span("48213/07", "numero de dossier", 0.2)
    assert _textes(filtrer_numeros_douteux(TEXTE, [sp])) == ["48213/07"]


def test_un_fragment_colle_a_un_vrai_numero_est_recadre_pas_efface():
    texte = "suivi du dossierX 48213/07 en cours"
    debut = texte.index("X 48213")
    sp = Span(debut, debut + len("X 48213/07"), "numero de dossier", 0.3,
              "X 48213/07", Origine.MODELE.value)
    (recadre,) = filtrer_numeros_douteux(texte, [sp])
    assert recadre.text == "48213/07"
    assert texte[recadre.start:recadre.end] == "48213/07"


def test_de_la_ponctuation_et_un_chiffre_ne_font_pas_un_numero():
    texte = "preservee.\n\n./.\n\n/\n\n1,\n\nen"
    debut = texte.index(".\n\n./.")
    fin = texte.index("1,") + 1
    sp = Span(debut, fin, "numero de dossier", 0.13, texte[debut:fin],
              Origine.MODELE.value)
    assert filtrer_numeros_douteux(texte, [sp]) == []


def test_une_confiance_elevee_n_est_pas_discutee():
    sp = _span("I, L4 sur L5", "numero de dossier", 0.5)
    assert filtrer_numeros_douteux(TEXTE, [sp]) == [sp]


def test_une_regle_deterministe_n_est_pas_discutee():
    sp = _span("C2", "numero de dossier", 0.1, origine=Origine.REGLE.value)
    assert filtrer_numeros_douteux(TEXTE, [sp]) == [sp]


def test_les_autres_etiquettes_ne_sont_pas_touchees():
    sp = _span("musculaire", "personne", 0.2)
    assert filtrer_numeros_douteux(TEXTE, [sp]) == [sp]


def test_un_numero_avs_sans_chiffre_est_ecarte():
    """« Avis ORL » sortait en numero AVS sur un dossier reel."""
    t = "Avis ORL: examen normal"
    for label in ("numero AVS", "IBAN", "numero de telephone", "numero de carte"):
        for score in (0.29, 0.95):
            sp = Span(0, 8, label, score, "Avis ORL")
            assert filtrer_numeros_douteux(t, [sp]) == [], (label, score)


def test_un_numero_avs_avec_chiffres_reste():
    t = "AVS 756.1234.5678.97"
    sp = Span(4, len(t), "numero AVS", 0.2, t[4:])
    assert filtrer_numeros_douteux(t, [sp]) == [sp]


def test_un_mot_pose_par_une_regle_ou_une_personne_reste():
    t = "Avis ORL"
    for origine in (Origine.REGLE.value, Origine.HUMAIN.value):
        sp = Span(0, 8, "numero AVS", 1.0, t, origine)
        assert filtrer_numeros_douteux(t, [sp]) == [sp]


# --- schemas posologiques ---------------------------------------------------
# Cas d'un plan de medication reel : la colonne Posologie sortait du modele en
# numero de dossier ou AVS, a 0,19 et 0,35, et etait noircie entiere.

from caviardoc.pipeline import analyse, filtrer_posologies  # noqa: E402

PLAN = ("Medicament Posologie\n"
        "COMPRIME A cpr 10 mg\n0 - 0 - 2 - 0\n\n"
        "COMPRIME B caps 200 mg\n1 - 0 - 1 - 0\n\n"
        "COMPRIME C cpr 40 mg\n1 - 0 - 0 - 0\n\n"
        "0-0-2-0\n1-0-1-0\n1-0-0-0\n\n"
        "Demi-dose ½-0-½ ou 1/2-0-1/2-0\n"
        "Dossier 48213/07, ne le 01-02-24.")


def _dans_plan(sous, label, score, origine=Origine.MODELE.value, n=0):
    i = -1
    for _ in range(n + 1):
        i = PLAN.index(sous, i + 1)
    return Span(i, i + len(sous), label, score, sous, origine)


def test_une_posologie_n_est_jamais_un_numero_ni_une_date():
    spans = [_dans_plan("1 - 0 - 0 - 0", "numero de dossier", 0.19),
             _dans_plan("1 - 0 - 0 - 0", "numero AVS", 0.9),
             _dans_plan("0 - 0 - 2 - 0", "numero de telephone", 1.0,
                        Origine.REGLE.value),
             _dans_plan("0-0-2-0\n1-0-1-0\n1-0-0-0", "numero de dossier", 0.35),
             _dans_plan("1-0-1-0", "date", 1.0, Origine.REGLE.value),
             _dans_plan("½-0-½", "numero de dossier", 0.4),
             _dans_plan("1/2-0-1/2-0", "numero de patient", 0.4)]
    assert filtrer_posologies(spans) == []


def test_un_vrai_numero_ou_une_date_ne_passe_pas_pour_une_posologie():
    gardes = [_dans_plan("48213/07", "numero de dossier", 0.3),
              _dans_plan("01-02-24", "date", 1.0, Origine.REGLE.value),
              _dans_plan("cpr 40 mg\n1 - 0 - 0 - 0", "numero de dossier", 0.3)]
    assert filtrer_posologies(gardes) == gardes


def test_analyse_ne_masque_aucune_cellule_de_posologie():
    """Chemin complet : regles, puis detections du modele injectees."""
    modele = [_dans_plan("1 - 0 - 0 - 0", "numero de dossier", 0.19),
              _dans_plan("0-0-2-0\n1-0-1-0\n1-0-0-0", "numero de dossier", 0.35),
              _dans_plan("0-0-2-0", "numero de dossier", 0.28)]
    spans = analyse(object(), PLAN, modele=modele, normalise_case=False)
    masques = "".join(PLAN[sp.start:sp.end] for sp in spans)
    assert "- 0 -" not in masques and "-0-" not in masques, spans
    assert any(sp.text == "01-02-24" for sp in spans)


def test_un_numero_sans_chiffre_est_ecarte_meme_a_forte_confiance():
    """« assura » sortait en « numero d'assure » a 0,69."""
    for label in ("numero d'assure", "numero de dossier", "numero de patient",
                  "identifiant de dossier"):
        sp = _span("Dossier", label, 0.95)
        assert filtrer_numeros_douteux(TEXTE, [sp]) == []


def test_un_numero_d_assure_alphanumerique_est_garde():
    texte = "Assure A123456 et 80756001234."
    for sous in ("A123456", "80756001234"):
        i = texte.index(sous)
        sp = Span(i, i + len(sous), "numero d'assure", 0.9, sous, Origine.MODELE.value)
        assert filtrer_numeros_douteux(texte, [sp]) == [sp]
