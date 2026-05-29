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

            prompt = f"""Tu es un professeur expert qui corrige une copie d'élève.

Élève : {student_name}
Matière : {matiere}
Chapitre : {chapitre or 'général'}
Niveau : {niveau}

Énoncé de l'exercice :
---
{exercise_text[:2000]}
---

Correction officielle :
---
{correction_text[:2000] if correction_text else "Non disponible — évalue selon tes connaissances du programme sénégalais."}
---

Analyse la copie manuscrite de l'élève et retourne UNIQUEMENT ce JSON :
{{
  "score": 75,
  "mention": "Bien",
  "points_forts": ["Démarche correcte", "Résultat juste"],
  "erreurs": ["Unité manquante", "Calcul intermédiaire faux"],
  "methodologie": "commentaire sur la méthode",
  "conseils": ["Conseil 1", "Conseil 2"],
  "notions_a_revoir": ["notion1", "notion2"],
  "encouragement": "Message d'encouragement personnalisé"
}}

score = 0-100
mention = Excellent/Très bien/Bien/Assez bien/Insuffisant/À revoir"""

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
        points_forts = analysis.get("points_forts", [])
        erreurs = analysis.get("erreurs", [])
        conseils = analysis.get("conseils", [])
        notions = analysis.get("notions_a_revoir", [])
        encouragement = analysis.get("encouragement", "Continue ! 💪")
        methodologie = analysis.get("methodologie", "")

        # Emoji score
        if score >= 80:
            score_emoji = "🟢"
        elif score >= 60:
            score_emoji = "🟡"
        elif score >= 40:
            score_emoji = "🟠"
        else:
            score_emoji = "🔴"

        msg = f"{score_emoji} *Correction de ta copie, {student_name}*\n\n"
        msg += f"*Score : {score}/100 — {mention}*\n\n"

        if points_forts:
            msg += "*✅ Points forts :*\n"
            for p in points_forts[:3]:
                msg += f"- {p}\n"
            msg += "\n"

        if erreurs:
            msg += "*❌ Points à corriger :*\n"
            for e in erreurs[:3]:
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
            msg += f"*📚 Notions à revoir :* {', '.join(notions[:3])}\n\n"

        msg += encouragement

        return msg


copy_analyzer_service = CopyAnalyzerService()
