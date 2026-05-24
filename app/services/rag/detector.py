"""
Détection intelligente de la matière, chapitre et type d'exercice
depuis le message de l'élève — via LLM avec fallback keywords.
"""
import json
import re
import httpx
from app.core.settings import get_settings

settings = get_settings()

# ── Fallback keywords (utilisé si LLM échoue) ─────────────────────────

MATIERE_KEYWORDS = {
    "maths": [
        "derive", "dérive", "dérivée", "fonction", "limite", "integrale",
        "probabilite", "probabilité", "suite", "vecteur", "equation", "équation",
        "logarithme", "exponentielle", "trigonometrie", "sin", "cos", "tan",
        "geometrie", "géométrie", "statistique", "complexe", "polynome",
    ],
    "physique": [
        "vitesse", "acceleration", "force", "mouvement", "newton", "energie",
        "tension", "courant", "resistance", "circuit", "ohm", "volt", "ampere",
        "condensateur", "bobine", "champ", "optique", "lumiere", "lentille",
        "oscillation", "pendule", "onde", "thermodynamique",
    ],
    "chimie": [
        "acide", "base", "ph", "reaction", "mol", "concentration", "oxyde",
        "ion", "molecule", "atome", "oxydation", "titrage", "dosage",
        "alcool", "amine", "ester", "alcane",
    ],
    "svt": [
        "cellule", "adn", "gene", "chromosome", "mitose", "meiose",
        "photosynthese", "photosynthèse", "respiration", "digestion",
        "neurone", "hormone", "anticorps", "evolution", "ecosysteme",
        "genetique", "proteine", "enzyme",
    ],
    "physique_chimie": [
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

    async def detect(self, text: str, user_context: dict = None) -> dict:
        """
        Détecte matière, chapitre et type depuis le message.
        Essaie d'abord le LLM, fallback sur keywords si échec.
        """
        user_context = user_context or {}

        # Essaie le LLM en premier
        result = await self._detect_with_llm(text, user_context)
        # N'accepter le résultat LLM que s'il a une matière ET confiance suffisante
        if result and result.get("confiance", 0) >= 0.5 and result.get("matiere"):
            print(f"Détection LLM: {result['matiere']}/{result['chapitre']} ({result['confiance']})")
            return result

        # Fallback keywords (si LLM échoue ou retourne matiere=None)
        if result and result.get("matiere") is None:
            print(f"Fallback keywords (LLM retourna matiere=None avec confiance {result.get('confiance')})")
        else:
            print("Fallback détection keywords")
        return self._detect_with_keywords(text, user_context)

    async def _detect_with_llm(self, text: str, user_context: dict) -> dict | None:
        """Détection intelligente via Mistral."""
        if not settings.mistral_api_key:
            return None

        exam = user_context.get("exam_type", "bac_senegal")
        serie = user_context.get("serie", "")

        prompt = f"""Analyse ce message d'un élève africain francophone et extrais les informations en JSON.

Message : "{text}"
Contexte : Examen={exam}, Série={serie}

Réponds UNIQUEMENT avec ce JSON valide, sans texte avant ou après :
{{
  "matiere": "maths ou physique ou chimie ou svt ou physique_chimie ou francais ou philosophie ou histoire_geo ou anglais ou null",
  "chapitre": "nom_chapitre_snake_case ou null",
  "type_demande": "cours ou exercice ou correction ou methode ou revision ou null",
  "niveau_question": "debutant ou intermediaire ou avance",
  "mots_cles": ["concept1", "concept2"],
  "confiance": 0.9
}}

Exemples de chapitres : derivees, probabilites, suites, equations, mecanique, electricite, optique, nucleaire, genetique, photosynthese, acides_bases"""

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
                raw = data["choices"][0]["message"]["content"].strip()

                # Extrait le JSON
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group())
                    return result

        except Exception as e:
            print(f"Erreur détection LLM: {e}")

        return None

    def _detect_with_keywords(self, text: str, user_context: dict) -> dict:
        """Fallback — détection par mots-clés."""
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
        if any(kw in text for kw in ["exercice", "entraîne", "pratique", "donne moi"]):
            return "exercice"
        if any(kw in text for kw in ["corrige", "correction", "j'ai trouvé", "ma réponse"]):
            return "correction"
        if any(kw in text for kw in ["methode", "méthode", "comment faire", "étapes"]):
            return "methode"
        return "cours"


subject_detector = SubjectDetector()