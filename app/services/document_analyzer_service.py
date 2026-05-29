"""
Service d'analyse intelligente des documents pédagogiques.
Détecte les exercices, extraits les métadonnées, identifie les corrections.
"""
import json
import re
import httpx
from app.core.settings import get_settings

settings = get_settings()


class DocumentAnalyzerService:

    async def analyze_document(
        self,
        file_bytes: bytes,
        filename: str,
        exam_type: str = None,
        matiere: str = None,
        serie: str = None,
    ) -> dict:
        """
        Analyse un document PDF et détecte les exercices individuels.
        Retourne la structure complète du document.
        """
        # 1. Extrait le texte du PDF
        text_by_page = self._extract_text_by_page(file_bytes)
        total_pages = len(text_by_page)

        if total_pages == 0:
            return {"success": False, "error": "Document vide ou illisible"}

        # 2. Construit le sommaire pour le LLM
        summary = self._build_summary(text_by_page)

        # 3. Analyse avec Mistral
        analysis = await self._analyze_with_llm(
            summary=summary,
            filename=filename,
            total_pages=total_pages,
            exam_type=exam_type,
            matiere=matiere,
            serie=serie,
        )

        if not analysis:
            return {"success": False, "error": "Analyse LLM échouée"}

        return {
            "success": True,
            "total_pages": total_pages,
            "text_by_page": text_by_page,
            "exercises": analysis.get("exercises", []),
            "has_corrections": analysis.get("has_corrections", False),
            "matiere": analysis.get("matiere") or matiere,
            "exam_type": analysis.get("exam_type") or exam_type,
            "serie": analysis.get("serie") or serie,
            "annee": analysis.get("annee"),
        }

    def _extract_text_by_page(self, file_bytes: bytes) -> dict:
        """Extrait le texte page par page."""
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            pages = {}
            for i, page in enumerate(doc):
                text = page.get_text("text").strip()
                # Extrait aussi les titres (grande police)
                titles = []
                try:
                    blocks = page.get_text("dict")["blocks"]
                    for block in blocks:
                        if block.get("type") == 0:
                            for line in block.get("lines", []):
                                for span in line.get("spans", []):
                                    if span.get("size", 0) > 13:
                                        t = span.get("text", "").strip()
                                        if t:
                                            titles.append(t)
                except Exception:
                    pass
                pages[i + 1] = {
                    "text": text[:500],
                    "titles": titles[:5],
                    "word_count": len(text.split()),
                }
            doc.close()
            return pages
        except Exception as e:
            print(f"Erreur extraction texte: {e}")
            return {}

    def _build_summary(self, text_by_page: dict) -> str:
        """Construit un sommaire du document pour le LLM."""
        lines = []
        for page_num, data in text_by_page.items():
            if data.get("titles"):
                lines.append(f"Page {page_num} — Titres: {', '.join(data['titles'][:3])}")
            if data.get("text"):
                lines.append(f"Page {page_num} — Extrait: {data['text'][:200]}")
        return "\n".join(lines[:80])

    async def _analyze_with_llm(
        self,
        summary: str,
        filename: str,
        total_pages: int,
        exam_type: str = None,
        matiere: str = None,
        serie: str = None,
    ) -> dict | None:
        """Analyse le document avec Mistral pour détecter les exercices."""

        prompt = f"""Tu analyses un document scolaire sénégalais pour en extraire les exercices.

Fichier : {filename}
Pages totales : {total_pages}
Matière : {matiere or 'à détecter'}
Examen : {exam_type or 'à détecter'}
Série : {serie or 'à détecter'}

Contenu du document :
---
{summary}
---

Retourne UNIQUEMENT ce JSON valide (sans markdown) :
{{
  "matiere": "maths|physique_chimie|svt|francais|philosophie|histoire_geo|anglais",
  "exam_type": "bac_senegal|bfem|concours|null",
  "serie": "S1|S2|S3|L1|L2|T|STEG|null",
  "annee": 2023,
  "has_corrections": true,
  "exercises": [
    {{
      "number": 1,
      "title": "Exercice 1 : Les dérivées",
      "page_debut": 1,
      "page_fin": 3,
      "chapitre": "derivees",
      "niveau": 2,
      "has_correction": false,
      "correction_page_debut": null,
      "correction_page_fin": null,
      "bareme": {{"total": 20, "parties": []}},
      "tags": ["derivees", "terminale", "bac"]
    }}
  ]
}}

Règles :
- Détecte CHAQUE exercice séparément (Exercice 1, Exercice 2, etc.)
- page_debut et page_fin sont les pages réelles du document (1-indexé)
- has_correction = true si la correction de CET exercice est dans le document
- correction_page_debut/fin = pages de la correction si présente
- chapitre en minuscules sans accents (ex: derivees, genetique)
- niveau : 1=facile, 2=moyen, 3=difficile
- Si le document EST une correction, has_corrections=true et chaque exercice a has_correction=true"""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-large-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    print(f"Mistral error: {data}")
                    return None

                text = data["choices"][0]["message"]["content"].strip()
                text = re.sub(r"```json|```", "", text).strip()
                return json.loads(text)

        except json.JSONDecodeError as e:
            print(f"JSON invalide: {e}")
            return None
        except Exception as e:
            print(f"Erreur LLM: {e}")
            return None


document_analyzer_service = DocumentAnalyzerService()
