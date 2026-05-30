"""
Détection intelligente de la matière, chapitre et type d'exercice.

Stratégie hybride en 4 niveaux :
1. Embeddings + cosine sur labels enrichis (rapide, pas cher)
   → similarity >= SEUIL_MATIERE : label direct
2. LLM Mistral (précis, plus lent)
   → si embeddings insuffisants
3. Keywords (fallback robuste)
   → si LLM échoue
4. user_context (dernier recours)
   → utilise les matières de l'élève

Robustesse :
- Labels et messages embeddés avec le MÊME provider (Mistral)
- Si Mistral embed rate → skip embed → LLM directement
- Seuils calibrés : matière 0.72, chapitre 0.78
- Cache des embeddings de labels (calculés une seule fois)
"""
import json
import re
import math
import httpx
from app.core.settings import get_settings

settings = get_settings()

# ── Seuils de confiance ───────────────────────────────────────────────
SEUIL_MATIERE  = 0.72   # Plus permissif
SEUIL_CHAPITRE = 0.78   # Plus strict

# ── Labels enrichis pour la détection par embeddings ─────────────────
# Chaque label = liste de phrases descriptives
# Plus les phrases sont riches, plus la similarité est précise

LABELS_MATIERE = {
    "maths": [
        "calculer dériver intégrer fonction mathématique",
        "dérivée limite probabilité suite géométrique arithmétique",
        "équation discriminant vecteur géométrie statistiques",
        "logarithme exponentielle trigonométrie sinus cosinus",
        "polynôme complexe matrice algèbre analyse",
    ],
    "physique_chimie": [
        "vitesse accélération force mouvement mécanique Newton",
        "tension courant résistance circuit électrique condensateur",
        "lumière optique lentille réfraction réflexion",
        "oscillation pendule ressort onde thermodynamique",
        "champ électrique magnétique énergie cinétique potentielle",
        "acide base pH neutralisation titrage dosage",
        "réaction chimique oxydation réduction équilibrer",
        "alcool aldéhyde cétone ester amine chimie organique",
        "mole concentration solution molaire",
        "atome molécule ion électron liaison covalente",
        "noyau nucléaire radioactivité désintégration fission fusion",
        "isotope demi-vie activité rayonnement alpha bêta gamma",
        "réactions nucléaires équation bilan masse énergie",
    ],
    "svt": [
        "cellule ADN gène chromosome mitose méiose génétique",
        "photosynthèse chlorophylle respiration échange gazeux",
        "neurone synapse réflexe cerveau système nerveux",
        "hormone enzyme protéine métabolisme",
        "écosystème évolution espèce biodiversité",
    ],
    "francais": [
        "dissertation commentaire texte littéraire analyse",
        "grammaire conjugaison orthographe syntaxe",
        "figure de style métaphore narration argumentation",
        "roman poème théâtre lecture compréhension",
    ],
    "philosophie": [
        "conscience liberté vérité justice bonheur",
        "morale éthique devoir droit nature culture",
        "Descartes Kant Platon Aristote Nietzsche",
        "raison connaissance existence société",
    ],
    "histoire_geo": [
        "colonisation indépendance guerre révolution empire",
        "population migration urbanisation développement",
        "Afrique Sénégal carte géographie territoire",
        "histoire contemporaine première seconde guerre mondiale",
    ],
    "anglais": [
        "english grammar vocabulary tense verb",
        "reading comprehension essay writing",
        "present past future conditional",
    ],
}

LABELS_CHAPITRE = {
    "derivees": [
        "calculer la dérivée d'une fonction f prime",
        "règle dérivation produit quotient composée",
        "tableau de variations extremum maximum minimum tangente",
        "étude de fonction dérivée nulle croissante décroissante",
    ],
    "probabilites": [
        "probabilité événement variable aléatoire loi binomiale",
        "espérance mathématique variance écart-type",
        "dénombrement combinaison arrangement permutation",
    ],
    "suites": [
        "suite arithmétique géométrique raison terme général",
        "récurrence convergence limite suite numérique",
        "somme des termes suite croissante décroissante",
    ],
    "fonctions": [
        "étude de fonction domaine définition image antécédent",
        "courbe représentative graphe tracé",
        "fonction affine parabolique logarithme exponentielle",
    ],
    "equations": [
        "résoudre équation inéquation discriminant delta",
        "racines solutions polynôme second degré",
        "système équations substitution combinaison",
    ],
    "vecteurs": [
        "vecteur coordonnées produit scalaire colinéaire",
        "translation rotation géométrie vectorielle",
        "norme direction sens vecteur unitaire",
    ],
    "geometrie": [
        "triangle cercle angle théorème Pythagore",
        "aire périmètre volume surface solide",
        "droites parallèles perpendiculaires intersection",
    ],
    "statistiques": [
        "moyenne médiane mode variance écart-type",
        "effectif fréquence histogramme diagramme",
        "quartile boîte moustache données statistiques",
    ],
    "mecanique": [
        "mouvement vitesse accélération trajectoire référentiel",
        "forces Newton équilibre principe inertie",
        "énergie cinétique potentielle travail puissance",
    ],
    "electricite": [
        "circuit électrique tension courant résistance loi Ohm",
        "condensateur bobine dipôle générateur récepteur",
        "puissance électrique énergie kilowattheure",
    ],
    "optique": [
        "lumière réfraction réflexion lentille convergente divergente",
        "image réelle virtuelle foyer distance focale",
        "miroir prisme spectre couleurs",
    ],
    "nucleaire": [
        "noyau radioactivité désintégration alpha bêta gamma",
        "demi-vie période constante radioactive activité",
        "équation nucléaire conservation charge masse",
    ],
    "oscillations": [
        "oscillation pendule ressort amplitude période fréquence",
        "régime libre amorti forcé résonance",
        "équation différentielle oscillateur harmonique",
    ],
    "acides_bases": [
        "acide base pH neutralisation réaction acido-basique",
        "titrage dosage indicateur coloré",
        "constante acidité Ka pKa solution tampon",
    ],
    "reactions_chimiques": [
        "réaction chimique bilan oxydation réduction",
        "oxydoréduction couple redox transfert électrons",
        "cinétique vitesse réaction catalyseur",
    ],
    "chimie_organique": [
        "alcool aldéhyde cétone acide carboxylique ester",
        "amine amide liaison covalente carbone",
        "alcane alcène alkyle nomenclature IUPAC",
    ],
    "genetique": [
        "ADN gène chromosome allèle dominant récessif",
        "mitose méiose division cellulaire",
        "génotype phénotype hérédité croisement",
    ],
    "photosynthese": [
        "photosynthèse chlorophylle chloroplaste lumière",
        "CO2 eau glucose dioxygène échanges gazeux",
        "phase lumineuse obscure Calvin cycle",
    ],
    "neurologie": [
        "neurone synapse influx nerveux réflexe",
        "cerveau moelle épinière système nerveux central",
        "neuromédiateur potentiel action dépolarisation",
    ],
}

# ── Keywords fallback ─────────────────────────────────────────────────

MATIERE_KEYWORDS = {
    "maths": [
        "derive", "dérive", "dérivée", "fonction", "limite", "integrale",
        "probabilite", "probabilité", "suite", "vecteur", "equation", "équation",
        "logarithme", "exponentielle", "trigonometrie", "sin", "cos", "tan",
        "geometrie", "géométrie", "statistique", "complexe", "polynome",
    ],
    "svt": [
        "cellule", "adn", "gene", "chromosome", "mitose", "meiose",
        "photosynthese", "photosynthèse", "respiration", "digestion",
        "neurone", "hormone", "anticorps", "evolution", "ecosysteme",
        "genetique", "proteine", "enzyme",
    ],
    "physique_chimie": [
        "vitesse", "acceleration", "force", "mouvement", "newton", "energie",
        "tension", "courant", "resistance", "circuit", "ohm", "volt", "ampere",
        "condensateur", "bobine", "champ", "optique", "lumiere", "lentille",
        "oscillation", "pendule", "onde", "thermodynamique",
        "acide", "base", "ph", "reaction", "mol", "concentration", "oxyde",
        "ion", "molecule", "atome", "oxydation", "titrage", "dosage",
        "alcool", "amine", "ester", "alcane",
        "nucleaire", "radioactivite", "fission", "fusion", "desintegration",
        "isotope", "noyau", "rayonnement",
    ],
    "francais": [
        "dissertation", "commentaire", "texte", "grammaire", "conjugaison",
        "figure", "metaphore", "narration", "argumentation",
    ],
    "philosophie": [
        "conscience", "liberte", "liberté", "verite", "vérité", "justice",
        "bonheur", "morale", "ethique", "éthique", "descartes", "kant", "platon",
    ],
    "histoire_geo": [
        "colonisation", "independance", "guerre", "revolution", "empire",
        "population", "migration", "urbanisation", "afrique", "senegal",
    ],
}

CHAPITRE_KEYWORDS = {
    "derivees": ["derive", "dérivée", "derivation", "f prime", "tableau de variation", "extremum", "tangente"],
    "probabilites": ["probabilite", "probabilité", "binomiale", "esperance", "variance"],
    "suites": ["suite", "arithmetique", "geometrique", "recurrence", "convergence"],
    "fonctions": ["fonction", "image", "antecedent", "courbe", "graphe", "domaine"],
    "equations": ["equation", "équation", "solution", "discriminant", "delta", "racine"],
    "vecteurs": ["vecteur", "coordonnee", "scalaire", "colineaire", "translation"],
    "geometrie": ["triangle", "cercle", "angle", "aire", "perimetre", "volume", "pythagore"],
    "statistiques": ["moyenne", "mediane", "mode", "effectif", "histogramme", "quartile"],
    "mecanique": ["vitesse", "acceleration", "force", "mouvement", "trajectoire", "newton"],
    "electricite": ["tension", "courant", "resistance", "circuit", "ohm", "condensateur", "bobine"],
    "optique": ["lumiere", "refraction", "reflection", "lentille", "miroir", "prisme"],
    "nucleaire": ["nucleaire", "radioactivite", "fission", "fusion", "desintegration", "demi-vie"],
    "oscillations": ["oscillation", "pendule", "ressort", "amplitude", "resonance"],
    "acides_bases": ["acide", "base", "ph", "neutralisation", "titrage", "dosage"],
    "reactions_chimiques": ["reaction", "oxydation", "reduction", "oxydoreduction"],
    "chimie_organique": ["alcool", "aldehyde", "cetone", "ester", "amine", "alcane"],
    "genetique": ["adn", "gene", "chromosome", "allele", "mitose", "meiose", "phenotype"],
    "photosynthese": ["photosynthese", "chlorophylle", "chloroplaste", "co2", "glucose"],
    "neurologie": ["neurone", "synapse", "influx", "reflexe", "cerveau"],
}


class SubjectDetector:

    def __init__(self):
        # Cache des embeddings de labels — calculés une seule fois
        self._matiere_embeddings: dict[str, list[float]] | None = None
        self._chapitre_embeddings: dict[str, list[float]] | None = None
        self._embed_provider = None  # Provider utilisé pour les labels

    # ── Entrée principale ─────────────────────────────────────────────

    async def detect(self, text: str, user_context: dict = None) -> dict:
        """
        Détection hybride en 4 niveaux.
        """
        user_context = user_context or {}

        # Niveau 1 — Embeddings + cosine
        embed_result = await self._detect_with_embeddings(text, user_context)
        if embed_result and embed_result.get("confiance", 0) >= SEUIL_MATIERE:
            print(f"Détection embed: {embed_result['matiere']}/{embed_result['chapitre']} ({embed_result['confiance']:.2f})")
            return embed_result

        # Niveau 2 — LLM Mistral
        if embed_result:
            print(f"Embed insuffisant ({embed_result.get('confiance', 0):.2f}) — fallback LLM")
        llm_result = await self._detect_with_llm(text, user_context)
        if llm_result and llm_result.get("confiance", 0) >= 0.5 and llm_result.get("matiere"):
            print(f"Détection LLM: {llm_result['matiere']}/{llm_result['chapitre']} ({llm_result['confiance']})")
            return llm_result

        # Niveau 3 — Keywords
        print("Fallback keywords")
        kw_result = self._detect_with_keywords(text, user_context)
        if kw_result.get("matiere"):
            return kw_result

        # Niveau 4 — user_context
        print("Fallback user_context")
        return {
            "matiere": user_context.get("matiere"),
            "chapitre": None,
            "type_demande": self._detect_type(text.lower()),
            "niveau_question": "intermediaire",
            "mots_cles": [],
            "confiance": 0.1,
        }

    # ── Niveau 1 — Embeddings ─────────────────────────────────────────

    async def _detect_with_embeddings(self, text: str, user_context: dict) -> dict | None:
        """
        Détection par similarité cosinus sur labels enrichis.
        Utilise le même provider que les chunks indexés (Mistral).
        """
        # Embed le message
        msg_embedding = await self._embed_text(text)
        if not msg_embedding:
            return None  # Provider indisponible → skip vers LLM

        # Charge les embeddings de labels (avec le même provider)
        matiere_embs = await self._get_matiere_embeddings()
        chapitre_embs = await self._get_chapitre_embeddings()

        if not matiere_embs:
            return None

        # Trouve la matière la plus proche
        matiere_scores = {}
        for matiere, emb in matiere_embs.items():
            if emb:
                matiere_scores[matiere] = self._cosine(msg_embedding, emb)

        if not matiere_scores:
            return None

        best_matiere = max(matiere_scores, key=matiere_scores.get)
        best_matiere_score = matiere_scores[best_matiere]

        # Si score matière insuffisant → LLM
        if best_matiere_score < SEUIL_MATIERE:
            return {
                "matiere": None,
                "chapitre": None,
                "confiance": best_matiere_score,
                "type_demande": self._detect_type(text.lower()),
                "niveau_question": "intermediaire",
                "mots_cles": [],
            }

        # Trouve le chapitre le plus proche
        best_chapitre = None
        best_chapitre_score = 0.0

        if chapitre_embs:
            chapitre_scores = {}
            for chapitre, emb in chapitre_embs.items():
                if emb:
                    chapitre_scores[chapitre] = self._cosine(msg_embedding, emb)

            if chapitre_scores:
                best_chapitre = max(chapitre_scores, key=chapitre_scores.get)
                best_chapitre_score = chapitre_scores[best_chapitre]

                # Seuil plus strict pour le chapitre
                if best_chapitre_score < SEUIL_CHAPITRE:
                    best_chapitre = None

        return {
            "matiere": best_matiere,
            "chapitre": best_chapitre,
            "confiance": best_matiere_score,
            "type_demande": self._detect_type(text.lower()),
            "niveau_question": self._detect_niveau(text.lower()),
            "mots_cles": [],
        }

    async def _embed_text(self, text: str) -> list[float] | None:
        """Embed via Mistral uniquement — cohérence avec les labels."""
        if not settings.mistral_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={"model": "mistral-embed", "input": [text]},
                )
                data = response.json()
                if "data" not in data:
                    return None
                return data["data"][0]["embedding"]
        except Exception as e:
            print(f"Detector embed error: {e}")
            return None

    async def _get_matiere_embeddings(self) -> dict[str, list[float]]:
        """
        Retourne les embeddings des labels matière.
        Calculés une seule fois puis mis en cache.
        """
        if self._matiere_embeddings is not None:
            return self._matiere_embeddings

        print("Calcul embeddings labels matière (une seule fois)...")
        self._matiere_embeddings = {}

        for matiere, phrases in LABELS_MATIERE.items():
            # Concatène toutes les phrases du label en un seul texte riche
            label_text = " ".join(phrases)
            emb = await self._embed_text(label_text)
            if emb:
                self._matiere_embeddings[matiere] = emb
            else:
                print(f"⚠️ Embedding label {matiere} échoué")
                self._matiere_embeddings = None
                return {}

        print(f"Labels matière prêts: {len(self._matiere_embeddings)} matières ✅")
        return self._matiere_embeddings

    async def _get_chapitre_embeddings(self) -> dict[str, list[float]]:
        """
        Retourne les embeddings des labels chapitre.
        Calculés une seule fois puis mis en cache.
        """
        if self._chapitre_embeddings is not None:
            return self._chapitre_embeddings

        print("Calcul embeddings labels chapitre (une seule fois)...")
        self._chapitre_embeddings = {}

        for chapitre, phrases in LABELS_CHAPITRE.items():
            label_text = " ".join(phrases)
            emb = await self._embed_text(label_text)
            if emb:
                self._chapitre_embeddings[chapitre] = emb
            else:
                print(f"⚠️ Embedding label {chapitre} échoué")
                self._chapitre_embeddings = None
                return {}

        print(f"Labels chapitre prêts: {len(self._chapitre_embeddings)} chapitres ✅")
        return self._chapitre_embeddings

    # ── Cosine ────────────────────────────────────────────────────────

    def _cosine(self, vec1: list[float], vec2: list[float]) -> float:
        """Similarité cosinus."""
        if len(vec1) != len(vec2):
            return 0.0
        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)

    # ── Niveau 2 — LLM ───────────────────────────────────────────────

    async def _detect_with_llm(self, text: str, user_context: dict) -> dict | None:
        """Détection via Mistral LLM — précis mais plus lent."""
        if not settings.mistral_api_key:
            return None

        exam = user_context.get("exam_type", "bac_senegal")
        serie = user_context.get("serie", "")

        prompt = f"""Analyse ce message d'un élève africain francophone et extrais les informations en JSON.

Message : "{text}"
Contexte : Examen={exam}, Série={serie}

RÈGLE IMPORTANTE pour type_demande :
- "exercice" si l'élève veut faire un exercice, s'entraîner, avoir un problème à résoudre
  Exemples : "donne moi un exo", "je veux un exercice", "donne un exo de chimie",
             "je veux m'entraîner", "propose moi un problème", "donne un exercice de maths"
- "cours" si l'élève pose une question de cours ou veut une explication
- "correction" si l'élève soumet une réponse à corriger

Réponds UNIQUEMENT avec ce JSON valide :
{{
  "matiere": "maths|physique_chimie|svt|francais|philosophie|histoire_geo|anglais|null",
  "chapitre": "nom_chapitre_snake_case ou null",
  "type_demande": "cours|exercice|correction|methode|revision|null",
  "niveau_question": "debutant|intermediaire|avance",
  "mots_cles": ["concept1", "concept2"],
  "confiance": 0.9
}}

Exemples chapitres : derivees, probabilites, suites, equations, mecanique, electricite, optique, nucleaire, genetique, photosynthese, acides_bases, oscillations, vecteurs, geometrie"""

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 200,
                        "temperature": 0.1,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    return None
                raw = data["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        except Exception as e:
            print(f"Erreur détection LLM: {e}")

        return None

    # ── Niveau 3 — Keywords ───────────────────────────────────────────

    def _detect_with_keywords(self, text: str, user_context: dict) -> dict:
        """Fallback keywords robuste."""
        text_lower = text.lower()
        matiere = self._detect_matiere_kw(text_lower, user_context.get("matiere"))
        chapitre = self._detect_chapitre_kw(text_lower)

        return {
            "matiere": matiere,
            "chapitre": chapitre,
            "type_demande": self._detect_type(text_lower),
            "niveau_question": "intermediaire",
            "mots_cles": [],
            "confiance": 0.6 if (matiere and chapitre) else 0.3,
        }

    def _detect_matiere_kw(self, text: str, user_matiere: str = None) -> str | None:
        scores = {}
        for matiere, keywords in MATIERE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scores[matiere] = score
        if not scores:
            return user_matiere
        best = max(scores, key=scores.get)
        if scores[best] == 1 and user_matiere:
            return user_matiere
        return best

    def _detect_chapitre_kw(self, text: str) -> str | None:
        scores = {}
        for chapitre, keywords in CHAPITRE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text)
            if score > 0:
                scores[chapitre] = score
        if not scores:
            return None
        return max(scores, key=scores.get)

    def _detect_type(self, text: str) -> str:
        if any(kw in text for kw in [
            "exercice", "exercices", "exo", "exos",
            "entraîne", "entraine", "entraînement",
            "pratique", "pratiquer",
            "donne", "propose", "fais moi", "donne-moi",
            "je veux", "je voudrais", "j'aimerais",
            "annale", "série", "serie",
            "problème", "probleme", "question",
            "m'entraîner", "m'entrainer", "travailler",
        ]):
            return "exercice"
        if any(kw in text for kw in [
            "corrige", "correction", "j'ai trouvé", "ma réponse",
            "est-ce correct", "est ce que c'est juste", "vérifie"
        ]):
            return "correction"
        if any(kw in text for kw in [
            "methode", "méthode", "comment faire", "étapes",
            "comment résoudre", "démarche", "procédure"
        ]):
            return "methode"
        if any(kw in text for kw in [
            "révise", "révision", "résumé", "fiche", "récapitulatif"
        ]):
            return "revision"
        return "cours"

    def _detect_niveau(self, text: str) -> str:
        if any(kw in text for kw in [
            "je comprends pas", "je ne comprends pas", "c'est quoi",
            "c'est quoi", "débutant", "facile", "simple", "expliquer"
        ]):
            return "debutant"
        if any(kw in text for kw in [
            "difficile", "compliqué", "avancé", "approfondi",
            "démonstration", "preuve", "théorème"
        ]):
            return "avance"
        return "intermediaire"


subject_detector = SubjectDetector()