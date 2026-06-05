"""
Service de traitement des CVs envoyés par WhatsApp.

Flux :
  1. save_cv()        — sauvegarde sur disque /home/prepa/app/cvs/{user_id}/
  2. extract_profile() — extrait le profil via fitz (PDF) ou Mistral Vision (image)
  3. process_cv()     — orchestre save + extract + embedding + upsert DB
"""
import re
import json
import base64
import httpx
from datetime import datetime
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.settings import get_settings
from app.services.rag.embedding_service import embedding_service

settings = get_settings()

CVS_DIR = Path("/home/prepa/app/cvs")


class CVProcessorService:

    # ─────────────────────────────────────────────────────────────
    # 1. Sauvegarde du fichier
    # ─────────────────────────────────────────────────────────────

    async def save_cv(self, user_id: str, file_bytes: bytes, filename: str) -> str:
        """
        Sauvegarde le CV sur disque.
        Retourne le chemin local absolu.
        """
        user_dir = CVS_DIR / str(user_id)
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"  ⚠️ cv_processor: impossible de créer {user_dir}: {e}")
            raise

        timestamp = int(datetime.now().timestamp())
        ext = Path(filename).suffix.lower() or ".pdf"
        dest = user_dir / f"cv_{timestamp}{ext}"

        dest.write_bytes(file_bytes)
        dest.chmod(0o644)
        print(f"  → CV sauvegardé : {dest}")
        return str(dest)

    # ─────────────────────────────────────────────────────────────
    # 2. Extraction du profil
    # ─────────────────────────────────────────────────────────────

    async def extract_profile(self, file_bytes: bytes, filename: str) -> dict:
        """
        Extrait le profil professionnel du CV.
        - PDF  → fitz (extraction texte native)
        - Image → Mistral Vision (transcription)
        Retourne un dict {competences, secteurs_interets, niveau_etudes,
                          annees_experience, localisation, disponibilite, cv_text}.
        """
        ext = Path(filename).suffix.lower()
        cv_text = ""

        if ext in (".pdf",):
            cv_text = self._extract_pdf_text(file_bytes)
        elif ext in (".jpg", ".jpeg", ".png", ".webp"):
            image_b64 = base64.b64encode(file_bytes).decode()
            cv_text = await self._transcribe_image(image_b64, ext)
        else:
            # Essaie PDF en premier, image en fallback
            cv_text = self._extract_pdf_text(file_bytes)
            if not cv_text.strip():
                image_b64 = base64.b64encode(file_bytes).decode()
                cv_text = await self._transcribe_image(image_b64, ".jpg")

        if not cv_text.strip():
            print("  ⚠️ cv_processor: texte CV vide — profil minimal retourné")
            return self._empty_profile()

        profil = await self._analyze_cv_with_llm(cv_text)
        profil["cv_text"] = cv_text[:8000]  # limite stockage
        return profil

    def _extract_pdf_text(self, file_bytes: bytes) -> str:
        """Extraction texte native via fitz."""
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            text = ""
            for page in doc:
                text += page.get_text("text")
            doc.close()
            return text.strip()
        except Exception as e:
            print(f"  ⚠️ cv_processor fitz: {e}")
            return ""

    async def _transcribe_image(self, image_b64: str, ext: str) -> str:
        """Transcription image via Mistral Vision."""
        mime = "image/jpeg" if ext in (".jpg", ".jpeg", ".webp") else "image/png"
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
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
                                    "image_url": f"data:{mime};base64,{image_b64}",
                                },
                                {
                                    "type": "text",
                                    "text": "Transcris intégralement le texte de ce CV. Garde la structure (titres, sections, bullet points). Retourne uniquement le texte brut.",
                                },
                            ],
                        }],
                        "max_tokens": 3000,
                        "temperature": 0.0,
                    },
                )
                data = response.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"  ⚠️ cv_processor vision: {e}")
        return ""

    async def _analyze_cv_with_llm(self, cv_text: str) -> dict:
        """Analyse le texte du CV avec Mistral pour extraire le profil structuré."""
        prompt = f"""Extrais le profil professionnel de ce CV sénégalais/africain.

IMPORTANT :
- "competences" = compétences TECHNIQUES et hard skills uniquement (ex: Comptabilité, Laravel, Excel, Python) — PAS les soft skills comme "leadership", "communication"
- "competences_normalisees" = même compétences mais standardisées (ex: "SAARI SAGE" → "Comptabilité", "Laravel PHP" → "Développement web")
- "secteurs_interets" = secteurs basés sur EXPÉRIENCE et FORMATION uniquement (pas les centres d'intérêt personnels)
- "metiers_cibles" = postes que cette personne peut occuper maintenant
- "metiers_proches" = postes connexes auxquels elle peut prétendre
- "annees_experience" = nombre total d'années d'expérience professionnelle

Retourne UNIQUEMENT ce JSON valide (sans markdown) :
{{
  "competences": ["Comptabilité", "Développement web", "Excel"],
  "competences_normalisees": ["Comptabilité", "Développement web", "Bureautique"],
  "secteurs_interets": ["Finance/Comptabilité", "Informatique/Tech"],
  "metiers_cibles": ["Assistant comptable", "Développeur web"],
  "metiers_proches": ["Contrôleur de gestion", "Chargé de clientèle"],
  "niveau_etudes": "bac|bac+2|bac+3|bac+5|doctorat",
  "annees_experience": 3,
  "localisation": "ville ou pays",
  "disponibilite": "immediate|1_mois|3_mois|non_precise",
  "resume_profil": "Résumé professionnel 2-3 phrases pour le matching"
}}

CV :
----
{cv_text[:4000]}
----"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 400,
                        "temperature": 0.1,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    print(f"  ⚠️ cv_processor LLM: {data}")
                    return self._empty_profile()
                raw = data["choices"][0]["message"]["content"].strip()
                raw = re.sub(r"```json|```", "", raw).strip()
                profil = json.loads(raw)
                # Valide et nettoie les champs critiques
                profil.setdefault("competences", [])
                profil.setdefault("secteurs_interets", [])
                profil.setdefault("niveau_etudes", None)
                profil.setdefault("annees_experience", 0)
                profil.setdefault("localisation", None)
                profil.setdefault("disponibilite", "non_precise")
                profil.setdefault("metiers_cibles", [])
                profil.setdefault("metiers_proches", [])
                profil.setdefault("competences_normalisees", [])
                profil.setdefault("resume_profil", "")
                if not isinstance(profil["competences"], list):
                    profil["competences"] = []
                if not isinstance(profil["secteurs_interets"], list):
                    profil["secteurs_interets"] = []
                try:
                    profil["annees_experience"] = int(profil["annees_experience"])
                except (TypeError, ValueError):
                    profil["annees_experience"] = 0
                return profil
        except json.JSONDecodeError as e:
            print(f"  ⚠️ cv_processor JSON invalide: {e}")
            return self._empty_profile()
        except Exception as e:
            print(f"  ⚠️ cv_processor LLM exception: {e}")
            return self._empty_profile()

    def _empty_profile(self) -> dict:
        return {
            "competences": [],
            "competences_normalisees": [],
            "secteurs_interets": [],
            "metiers_cibles": [],
            "metiers_proches": [],
            "niveau_etudes": None,
            "annees_experience": 0,
            "localisation": None,
            "disponibilite": "non_precise",
            "resume_profil": "",
            "cv_text": "",
        }

    # ─────────────────────────────────────────────────────────────
    # 3. Orchestration complète
    # ─────────────────────────────────────────────────────────────

    async def process_cv(
        self,
        db: AsyncSession,
        user,
        file_bytes: bytes,
        filename: str,
    ) -> dict:
        """
        Pipeline complet :
          save_cv → extract_profile → embedding → upsert CandidateProfile → update user
        Retourne {"success": bool, "profil": dict, "cv_url": str}
        """
        from app.models.candidate_profile import CandidateProfile

        # 1. Sauvegarde fichier
        try:
            cv_url = await self.save_cv(str(user.id), file_bytes, filename)
        except Exception as e:
            print(f"  ⚠️ process_cv save_cv failed: {e}")
            cv_url = None

        # 2. Extraction profil
        profil = await self.extract_profile(file_bytes, filename)

        # 3. Embedding basé sur resume_profil + compétences normalisées + métiers
        cv_text = profil.get("cv_text", "")
        embedding = None
        embed_text = (
            f"{profil.get('resume_profil', '')} "
            f"{' '.join(profil.get('competences_normalisees', []))} "
            f"{' '.join(profil.get('metiers_cibles', []))}"
        ).strip()
        if not embed_text and cv_text.strip():
            embed_text = cv_text[:500]  # fallback si LLM n'a pas produit de résumé
        if embed_text:
            embedding = await embedding_service.embed_text(embed_text)

        secteur_prioritaire = (profil.get("secteurs_interets") or [None])[0]

        # 4. Upsert CandidateProfile
        result = await db.execute(
            select(CandidateProfile).where(CandidateProfile.user_id == user.id)
        )
        candidate = result.scalar_one_or_none()

        if candidate:
            candidate.competences = profil.get("competences")
            candidate.secteurs_interets = profil.get("secteurs_interets")
            candidate.niveau_etudes = profil.get("niveau_etudes")
            candidate.annees_experience = profil.get("annees_experience", 0)
            candidate.localisation = profil.get("localisation")
            candidate.disponibilite = profil.get("disponibilite")
            candidate.cv_text = cv_text[:8000] if cv_text else None
            candidate.cv_url = cv_url
            candidate.metiers_cibles = profil.get("metiers_cibles", [])
            candidate.metiers_proches = profil.get("metiers_proches", [])
            candidate.competences_normalisees = profil.get("competences_normalisees", [])
            candidate.resume_profil = profil.get("resume_profil", "")
            candidate.type_contrat_souhaite = user.type_contrat_souhaite
            candidate.secteur_prioritaire = secteur_prioritaire
            if embedding:
                candidate.embedding = embedding
        else:
            candidate = CandidateProfile(
                user_id=user.id,
                competences=profil.get("competences"),
                secteurs_interets=profil.get("secteurs_interets"),
                niveau_etudes=profil.get("niveau_etudes"),
                annees_experience=profil.get("annees_experience", 0),
                localisation=profil.get("localisation"),
                disponibilite=profil.get("disponibilite"),
                cv_text=cv_text[:8000] if cv_text else None,
                cv_url=cv_url,
                embedding=embedding,
                metiers_cibles=profil.get("metiers_cibles", []),
                metiers_proches=profil.get("metiers_proches", []),
                competences_normalisees=profil.get("competences_normalisees", []),
                resume_profil=profil.get("resume_profil", ""),
                type_contrat_souhaite=user.type_contrat_souhaite,
                secteur_prioritaire=secteur_prioritaire,
            )
            db.add(candidate)

        # 5. Met à jour user.niveau_etudes depuis le profil
        if profil.get("niveau_etudes"):
            user.niveau_etudes = profil["niveau_etudes"]

        await db.flush()
        print(f"  → CandidateProfile upsert OK pour user {user.id}")

        return {
            "success": True,
            "profil": {k: v for k, v in profil.items() if k != "cv_text"},
            "cv_url": cv_url,
            "has_embedding": embedding is not None,
        }


cv_processor_service = CVProcessorService()
