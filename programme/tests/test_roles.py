"""Niveau d'anonymisation : distinguer le patient du soignant.

Le point critique n'est pas la finesse de la distinction mais son comportement
en cas d'echec : un outil de confidentialite doit se tromper du cote sur.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import Span  # noqa: E402
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402
from caviardoc.roles import (Niveau, est_patient, est_professionnel,  # noqa: E402
                           filtrer_par_role, noms_du_patient,
                           qualifier_personnes)


def _span(texte, aiguille, label="personne"):
    d = texte.index(aiguille)
    return Span(d, d + len(aiguille), label, 0.9, aiguille)


class TestProfessionnel:
    def test_titre_dans_le_nom(self):
        t = "Dr Philippe Mercier"
        assert est_professionnel(t, _span(t, "Dr Philippe Mercier"))

    def test_titre_juste_avant(self):
        t = "la Dre Sylvie Girard"
        assert est_professionnel(t, _span(t, "Sylvie Girard"))

    def test_nom_nu_non_professionnel(self):
        t = "Yannick Fontannaz"
        assert not est_professionnel(t, _span(t, "Yannick Fontannaz"))


class TestPatient:
    def test_date_de_naissance_accolee(self):
        t = "Monsieur Yannick Fontannaz, ne le 23.04.1931"
        dn = [_span(t, "23.04.1931", "date")]
        assert est_patient(t, _span(t, "Monsieur Yannick Fontannaz"), dn)

    def test_mention_concernant(self):
        t = "Concernant : Yannick Fontannaz"
        assert est_patient(t, _span(t, "Yannick Fontannaz"), [])

    def test_medecin_jamais_patient(self):
        """Meme suivi d'une date, un titre professionnel exclut le patient."""
        t = "Dr Philippe Mercier, ne le 23.04.1931"
        dn = [_span(t, "23.04.1931", "date")]
        assert not est_patient(t, _span(t, "Dr Philippe Mercier"), dn)


class TestFiltrage:
    TEXTE = ("Dr Philippe Mercier\nRue des Noisetiers 42\n\n"
             "Concernant : Yannick Fontannaz\nChemin des Ifs 17\n")

    def _spans(self):
        return [_span(self.TEXTE, "Dr Philippe Mercier"),
                _span(self.TEXTE, "Rue des Noisetiers 42", "rue"),
                _span(self.TEXTE, "Yannick Fontannaz"),
                _span(self.TEXTE, "Chemin des Ifs 17", "rue")]

    def test_niveau_complet_conserve_tout(self):
        assert len(filtrer_par_role(self.TEXTE, self._spans(),
                                    Niveau.COMPLET, set())) == 4

    def test_niveau_patient_epargne_le_soignant(self):
        noms = noms_du_patient(self.TEXTE, self._spans())
        gardes = [g.text for g in filtrer_par_role(
            self.TEXTE, self._spans(), Niveau.PATIENT, noms)]
        assert "Yannick Fontannaz" in gardes
        assert "Dr Philippe Mercier" not in gardes
        assert "Rue des Noisetiers 42" not in gardes, "adresse du cabinet conservee"

    def test_adresse_du_patient_masquee(self):
        noms = noms_du_patient(self.TEXTE, self._spans())
        gardes = [g.text for g in filtrer_par_role(
            self.TEXTE, self._spans(), Niveau.PATIENT, noms)]
        assert "Chemin des Ifs 17" in gardes

    def test_sans_patient_identifie_rien_n_est_conserve(self):
        """Le filtre seul ne masque rien : c'est l'appelant qui doit se replier.

        Ce comportement est verifie explicitement parce qu'il est dangereux :
        sans repli, un document dont le patient n'est pas reconnu sortirait
        intact. `caviarder_pdf` applique le niveau complet dans ce cas.
        """
        gardes = filtrer_par_role(self.TEXTE, self._spans(),
                                  Niveau.PATIENT, set())
        assert gardes == []


class TestSoignantsSansTitre:
    """Cas types (noms inventes) : des soignants pris pour le patient."""

    def test_qualification_entre_parentheses(self):
        """Les tableaux hospitaliers qualifient le soignant apres son nom."""
        t = "Prescripteur\nLAMY AMANDINE (PRT)\nFinal"
        assert est_professionnel(t, _span(t, "LAMY AMANDINE"))

    def test_qualification_explicite_entre_parentheses(self):
        t = "Valide par COSTA Ana (Medecin hospitalier) le 08.03."
        assert est_professionnel(t, _span(t, "COSTA Ana"))

    def test_titre_a_plusieurs_mots_de_distance(self):
        """« Au medecin responsable du patient / COSTA Ana ».

        Le mot colle au nom est « patient » ; le titre est quatre mots plus
        loin. Regarder le seul mot adjacent faisait du destinataire — un
        medecin — le patient lui-meme.
        """
        t = "Au medecin responsable du patient\nCOSTA Ana\n"
        assert est_professionnel(t, _span(t, "COSTA Ana"))
        assert not est_patient(t, _span(t, "COSTA Ana"), [])

    def test_un_nom_nu_reste_candidat(self):
        t = "Rapport concernant Yannick Fontannaz"
        assert not est_professionnel(t, _span(t, "Yannick Fontannaz"))


class TestNaissanceCommeAncre:
    """La date de naissance prime sur les mentions textuelles."""

    def _spans(self, texte, *noms):
        out = [_span(texte, n) for n in noms]
        d = texte.index("23.04.1931")
        out.append(Span(d, d + 10, "date", 1.0, "23.04.1931"))
        return out

    def test_seuls_les_noms_ancres_sont_retenus(self):
        """Quand une naissance ancre un nom, les mentions sont ignorees.

        Un dossier medical emploie le mot « patient » a chaque paragraphe ;
        une date de naissance ne suit qu'un seul nom.
        """
        t = ("Rapport du patient suivi par Marc Ducommun\n"
             "Fontannaz Yannick, ne le 23.04.1931\n")
        noms = noms_du_patient(t, self._spans(t, "Marc Ducommun", "Fontannaz Yannick"))
        assert noms == {"Fontannaz Yannick"}

    def test_un_mot_entre_le_nom_et_la_date_rompt_le_lien(self):
        """Sans cette exigence, tout nom proche d'un en-tete devient patient.

        Sur un rapport de readaptation la date de naissance figure en tete de
        chaque page : « Tiffeneau », un indice spirometrique, etait retenu.
        """
        t = "Indice Tiffeneau mesure chez le sujet 23.04.1931"
        d = t.index("23.04.1931")
        naissance = [Span(d, d + 10, "date", 1.0, "23.04.1931")]
        assert not est_patient(t, _span(t, "Tiffeneau"), naissance)

    def test_mention_introductive_toleree(self):
        for forme in ("Fontannaz Yannick, ne le 23.04.1931",
                      "Fontannaz Yannick Né(e) le 23.04.1931",
                      "Fontannaz Yannick 23.04.1931"):
            d = forme.index("23.04.1931")
            naissance = [Span(d, d + 10, "date", 1.0, "23.04.1931")]
            assert est_patient(forme, _span(forme, "Fontannaz Yannick"),
                               naissance), forme

    def test_repli_sur_les_mentions_sans_date(self):
        """Sans aucune date de naissance, les mentions reprennent leur role."""
        t = "Concernant : Yannick Fontannaz\n"
        assert noms_du_patient(t, [_span(t, "Yannick Fontannaz")]) == {"Yannick Fontannaz"}


class TestNaissanceParEtiquetteSeule:
    """Une etiquette du modele non confirmee ne doit pas exclure le patient."""

    TEXTE = ("Patient : Alice Exemple\nRue Fictive 1, 1000 Villeville\n"
             "Tel 021 000 00 00\n\n"
             "Edite par Bob Factice, le 03.05.2024\n")

    def _spans(self, texte, label_date):
        d = texte.index("03.05.2024")
        return [_span(texte, "Alice Exemple"), _span(texte, "Bob Factice"),
                Span(d, d + 10, label_date, 0.9, "03.05.2024"),
                _span(texte, "Rue Fictive 1", "adresse"),
                _span(texte, "021 000 00 00", "telephone")]

    def test_etiquette_seule_protege_les_deux_blocs(self):
        spans = self._spans(self.TEXTE, "date de naissance")
        noms = noms_du_patient(self.TEXTE, spans)
        assert {"Alice Exemple", "Bob Factice"} <= noms
        gardes = [g.text for g in filtrer_par_role(
            self.TEXTE, spans, Niveau.PATIENT, noms)]
        assert "Rue Fictive 1" in gardes
        assert "021 000 00 00" in gardes

    def test_naissance_confirmee_par_le_texte_reste_exclusive(self):
        t = ("Patient : Alice Exemple\n\n"
             "Bob Factice, ne le 03.05.1950\n")
        d = t.index("03.05.1950")
        spans = [_span(t, "Alice Exemple"), _span(t, "Bob Factice"),
                 Span(d, d + 10, "date", 0.9, "03.05.1950")]
        assert noms_du_patient(t, spans) == {"Bob Factice"}


class TestQualification:
    """Le texte exporte dit qui est le patient et qui le soigne.

    « PERSONNE_3 » ne permet pas a un modele de langage de rediger un rapport :
    il ne sait plus qui a examine qui. Le role et la specialite figurent deja
    en clair dans le document ; les reporter dans le pseudonyme ne revele rien.
    """

    TEXTE = ("Dr méd. Jean Mercier\nSpécialiste FMH en rhumatologie\n"
             "Dre Anne Dupont, cardiologue\n\n"
             "Concernant : Bidule ZORGLUB, né le 23.04.1958\n"
             "Vu par la physiothérapeute Mme Claire Lamy (PRT). "
             "Accompagné de Lucie Haldi.\n"
             "Le Dr Mercier a revu le patient.")

    def spans(self):
        t = self.TEXTE
        return [_span(t, "Dr méd. Jean Mercier"), _span(t, "Dre Anne Dupont"),
                _span(t, "Bidule ZORGLUB"),
                _span(t, "23.04.1958", "date de naissance"),
                _span(t, "Mme Claire Lamy"), _span(t, "Lucie Haldi"),
                _span(t, "Dr Mercier")]

    def test_chaque_medecin_garde_sa_specialite(self):
        roles = qualifier_personnes([(self.TEXTE, self.spans())])
        assert roles["Dr méd. Jean Mercier"] == ("medecin", "rhumatologue")
        assert roles["Dre Anne Dupont"] == ("medecin", "cardiologue")

    def test_patient_et_soignant(self):
        roles = qualifier_personnes([(self.TEXTE, self.spans())])
        assert roles["Bidule ZORGLUB"] == ("patient", "")
        assert roles["Mme Claire Lamy"] == ("soignant", "physiotherapeute")

    def test_un_proche_n_est_pas_qualifie(self):
        assert "Lucie Haldi" not in qualifier_personnes(
            [(self.TEXTE, self.spans())])

    def test_un_adjectif_n_est_pas_une_specialite(self):
        t = "Dr Paul Martin : examen neurologique sans particularité."
        assert qualifier_personnes([(t, [_span(t, "Dr Paul Martin")])]) == {
            "Dr Paul Martin": ("medecin", "")}

    def test_le_pseudonyme_porte_le_role(self):
        table = TablePseudonymes()
        table.qualifier(qualifier_personnes([(self.TEXTE, self.spans())]))
        table.enregistrer(self.spans())
        t = self.TEXTE
        assert table.rendre(_span(t, "Dr méd. Jean Mercier")) == \
            "[MEDECIN_1_RHUMATOLOGUE]"
        assert table.rendre(_span(t, "Dre Anne Dupont")) == \
            "[MEDECIN_2_CARDIOLOGUE]"
        assert table.rendre(_span(t, "Bidule ZORGLUB")) == "[PATIENT_1]"
        assert table.rendre(_span(t, "Lucie Haldi")) == "[PERSONNE_1]"
        # La forme courte reprend le pseudonyme de la forme longue.
        assert table.rendre(_span(t, "Dr Mercier")) == "[MEDECIN_1_RHUMATOLOGUE]"

    def test_un_pseudonyme_deja_attribue_ne_change_pas(self):
        """Un meme medecin ne doit pas porter deux noms d'un document a
        l'autre du lot."""
        table = TablePseudonymes()
        assert table.pseudonyme("Jean Mercier", "personne") == "PERSONNE_1"
        table.qualifier({"Jean Mercier": ("medecin", "rhumatologue")})
        assert table.pseudonyme("Jean Mercier", "personne") == "PERSONNE_1"

    def test_titre_au_troisieme_mot_du_nom(self):
        """Cas type : le modele rend « Mr le Docteur\\nMERCIER PHILIPPE » d'un
        seul tenant, et la specialite suit a la ligne."""
        t = ("Mr le Docteur\nMERCIER PHILIPPE\nMédecine interne FMH\n"
             "Rue des Noisetiers 42")
        nom = "Mr le Docteur\nMERCIER PHILIPPE"
        assert qualifier_personnes([(t, [_span(t, nom)])]) == {
            nom: ("medecin", "interniste")}


class TestIdentites:
    """Qui est qui, quand les noms se ressemblent ou changent de forme."""

    TEXTE = ("Concerne : M. Jean Dupont, né le 12.03.1961\n\n"
             "J'ai vu votre patient, Monsieur Jean Dupont, pour un contrôle. "
             "Sa fille, Claire Dupont, l'accompagnait. M. Dupont reverra "
             "le Dr Paul Haldi, endocrinologue. DUPONT Jean est diabétique.")

    def _table(self):
        t = self.TEXTE
        spans = [_span(t, "M. Jean Dupont"),
                 _span(t, "12.03.1961", "date de naissance"),
                 _span(t, "Monsieur Jean Dupont"), _span(t, "Claire Dupont"),
                 _span(t, "M. Dupont"), _span(t, "Dr Paul Haldi"),
                 _span(t, "DUPONT Jean")]
        table = TablePseudonymes()
        table.qualifier(qualifier_personnes([(t, spans)]))
        table.enregistrer(spans)
        return table

    def test_le_patient_est_reconnu_sous_sa_forme_la_plus_longue(self):
        """Le role etait note sur « M. Jean Dupont », le pseudonyme cree sur
        « Monsieur Jean Dupont » : le patient devenait PERSONNE_1."""
        table = self._table()
        assert table.pseudonyme("Monsieur Jean Dupont", "personne") == "PATIENT_1"
        assert table.pseudonyme("M. Jean Dupont", "personne") == "PATIENT_1"

    def test_un_proche_du_meme_nom_garde_son_propre_pseudonyme(self):
        """La fille recevait le pseudonyme de son pere : le texte disait que
        le patient s'accompagnait lui-meme."""
        assert self._table().pseudonyme("Claire Dupont", "personne") == "PERSONNE_1"

    def test_le_patronyme_seul_designe_le_patient(self):
        assert self._table().pseudonyme("M. Dupont", "personne") == "PATIENT_1"

    def test_l_ordre_inverse_est_la_meme_personne(self):
        assert self._table().pseudonyme("DUPONT Jean", "personne") == "PATIENT_1"

    def test_un_homonyme_de_prenom_n_est_pas_confondu(self):
        table = self._table()
        assert table.pseudonyme("Jean Martin", "personne") == "PERSONNE_2"
