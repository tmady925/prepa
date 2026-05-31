"""
Service d'analyse des copies manuscrites des élèves.
Utilise Wasender decrypt-media + Mistral Vision.
"""
import base64
import httpx
from app.core.settings import get_settings

settings = get_settings()


class CopyAnalyzerService:

    async def decrypt_media(self, message_data: dict) -> str | None:
        """
        Décrypte le média via Wasender API.
        Retourne l'URL publique temporaire de l'image.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{settings.wasender_base_url}/decrypt-media",
                    headers={
                        "Authorization": f"Bearer {settings.wasender_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"data": {"messages": message_data}},
                )
                result = response.json()
                print(f"  → Decrypt media response: {str(result)[:200]}")
                return result.get("publicUrl") or result.get("data", {}).get("publicUrl")
        except Exception as e:
            print(f"  → Erreur decrypt media: {e}")
            return None

    async def download_image(self, url: str) -> bytes | None:
        """Télécharge l'image depuis l'URL publique."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.content
                print(f"  → Erreur download image: {response.status_code}")
                return None
        except Exception as e:
            print(f"  → Erreur download image: {e}")
            return None

    async def analyze_copy(
        self,
        image_bytes: bytes,
        exercise_text: str,
        correction_text: str,
        matiere: str,
        chapitre: str,
        niveau: int,
        student_name: str,
    ) -> dict | None:
        """
        Analyse la copie manuscrite avec Mistral Vision.
        Compare avec la correction et génère un compte rendu.
        """
        try:
            image_b64 = base64.b64encode(image_bytes).decode()

            prompt = f"""Tu es un correcteur expert du programme BAC Sénégal.

Élève : {student_name}
Matière : {matiere}
Chapitre : {chapitre or 'général'}

SUJET DE L'EXERCICE :
---
{exercise_text[:3000]}
---

CORRECTION OFFICIELLE :
---
{correction_text[:3000] if correction_text else "Non disponible — évalue selon tes connaissances du programme sénégalais BAC."}
---

INSTRUCTIONS DE CORRECTION :
1. Identifie TOUTES les questions du sujet avec leur barème (points attribués)
2. Pour CHAQUE question, cherche la réponse de l'élève dans l'image de la copie
3. Évalue chaque réponse : correcte / partielle / incorrecte / non traitée
4. Calcule le score RÉEL basé sur les points obtenus vs points totaux

RÈGLES STRICTES :
- Si une question n'est pas traitée → 0 point pour cette question
- Si la réponse est partiellement correcte → attribue les points partiels
- Si le barème n'est pas mentionné dans le sujet → répartis 20 points équitablement entre les questions
- Le score final = (points obtenus / points totaux) × 100
- JAMAIS donner un score > 0 si la copie est vide ou illisible

Retourne UNIQUEMENT ce JSON valide :
{{
  "questions": [
    {{
      "numero": "1",
      "enonce_court": "résumé de la question en 10 mots max",
      "bareme": 4,
      "points_obtenus": 2,
      "statut": "partiel",
      "commentaire": "démarche correcte mais résultat faux"
    }}
  ],
  "score": 45,
  "score_detail": "9/20 points",
  "mention": "Insuffisant",
  "points_forts": ["ce que l'élève maîtrise"],
  "erreurs": ["erreur précise 1"],
  "methodologie": "commentaire sur la méthode globale",
  "conseils": ["conseil concret 1"],
  "notions_a_revoir": ["notion1"],
  "encouragement": "message personnalisé motivant pour {student_name}",
  "copie_lisible": true
}}

statut par question : "correct" | "partiel" | "incorrect" | "non_traite"
mention globale : "Excellent" (≥85) | "Très bien" (≥75) | "Bien" (≥65) | "Assez bien" (≥55) | "Passable" (≥45) | "Insuffisant" (<45)"""

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "pixtral-12b-2409",
                        "messages": [{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": f"data:image/jpeg;base64,{image_b64}",
                                },
                                {
                                    "type": "text",
                                    "text": prompt,
                                }
                            ],
                        }],
                        "temperature": 0.1,
                        "max_tokens": 1000,
                    },
                )
                data = response.json()
                print(f"  → Mistral Vision response: {str(data)[:200]}")

                if "choices" not in data:
                    return None

                import json, re
                text = data["choices"][0]["message"]["content"].strip()
                text = re.sub(r"```json|```", "", text).strip()
                return json.loads(text)

        except Exception as e:
            print(f"  → Erreur analyse copie: {e}")
            return None

    def format_feedback(self, analysis: dict, student_name: str) -> str:
        """Formate le compte rendu pour WhatsApp."""
        score = analysis.get("score", 0)
        mention = analysis.get("mention", "")
        score_detail = analysis.get("score_detail", f"{score}/100")
        points_forts = analysis.get("points_forts", [])
        erreurs = analysis.get("erreurs", [])
        conseils = analysis.get("conseils", [])
        notions = analysis.get("notions_a_revoir", [])
        encouragement = analysis.get("encouragement", "Continue ! 💪")
        methodologie = analysis.get("methodologie", "")
        questions = analysis.get("questions", [])
        copie_lisible = analysis.get("copie_lisible", True)

        if not copie_lisible:
            return (
                f"📸 *{student_name}*, ta copie est difficile à lire.\n\n"
                f"Prends une photo plus nette et renvoie-la. 📷"
            )

        # Emoji score
        if score >= 85:
            score_emoji = "🏆"
        elif score >= 75:
            score_emoji = "🟢"
        elif score >= 55:
            score_emoji = "🟡"
        elif score >= 45:
            score_emoji = "🟠"
        else:
            score_emoji = "🔴"

        msg = f"{score_emoji} *Correction de ta copie, {student_name}*\n\n"
        msg += f"*Score : {score}/100 — {score_detail} — {mention}*\n\n"

        # Détail par question
        if questions:
            msg += "*📋 Détail par question :*\n"
            for q in questions:
                statut = q.get("statut", "")
                pts_obtenus = q.get("points_obtenus", 0)
                bareme = q.get("bareme", 0)
                enonce = q.get("enonce_court", f"Q{q.get('numero', '?')}")
                commentaire = q.get("commentaire", "")

                if statut == "correct":
                    icon = "✅"
                elif statut == "partiel":
                    icon = "⚠️"
                elif statut == "non_traite":
                    icon = "⬜"
                else:
                    icon = "❌"

                msg += f"{icon} Q{q.get('numero', '?')} — {pts_obtenus}/{bareme}pt"
                if commentaire:
                    msg += f" _{commentaire}_"
                msg += "\n"
            msg += "\n"

        if points_forts:
            msg += "*✅ Points forts :*\n"
            for p in points_forts[:2]:
                msg += f"- {p}\n"
            msg += "\n"

        if erreurs:
            msg += "*❌ À corriger :*\n"
            for e in erreurs[:2]:
                msg += f"- {e}\n"
            msg += "\n"

        if methodologie:
            msg += f"*📐 Méthode :* {methodologie}\n\n"

        if conseils:
            msg += "*💡 Conseils :*\n"
            for c in conseils[:2]:
                msg += f"- {c}\n"
            msg += "\n"

        if notions:
            msg += f"*📚 À revoir :* {', '.join(notions[:3])}\n\n"

        msg += encouragement

        return msg


copy_analyzer_service = CopyAnalyzerService()
