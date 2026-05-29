"""
Service d'extraction et de génération des PDFs d'exercices et corrections.
"""
import json
import re
import uuid
from pathlib import Path
from datetime import datetime
import httpx
import fitz  # PyMuPDF
from app.core.settings import get_settings

settings = get_settings()

# Dossiers de stockage sur le serveur
EXERCISES_DIR = Path("/home/prepa/app/exercises")
CORRECTIONS_DIR = Path("/home/prepa/app/corrections")

# Crée les dossiers s'ils n'existent pas
EXERCISES_DIR.mkdir(parents=True, exist_ok=True)
CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)


class ExerciseExtractorService:

    def extract_pdf_pages(
        self,
        file_bytes: bytes,
        page_debut: int,
        page_fin: int,
    ) -> bytes | None:
        """
        Extrait les pages page_debut à page_fin d'un PDF.
        Retourne les bytes du nouveau PDF.
        """
        try:
            src = fitz.open(stream=file_bytes, filetype="pdf")
            dst = fitz.open()

            start = max(0, page_debut - 1)
            end = min(len(src), page_fin)

            dst.insert_pdf(src, from_page=start, to_page=end - 1)

            result = dst.tobytes()
            dst.close()
            src.close()
            return result

        except Exception as e:
            print(f"Erreur extraction pages {page_debut}-{page_fin}: {e}")
            return None

    def save_pdf(
        self,
        pdf_bytes: bytes,
        folder: Path,
        filename: str,
    ) -> str | None:
        """Sauvegarde un PDF sur le serveur. Retourne le chemin."""
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / filename
            path.write_bytes(pdf_bytes)
            return str(path)
        except Exception as e:
            print(f"Erreur sauvegarde PDF: {e}")
            return None

    async def generate_correction(
        self,
        exercise_text: str,
        exercise_title: str,
        matiere: str,
        chapitre: str,
        niveau: int,
        exam_type: str,
        serie: str,
        annee: int = None,
    ) -> str | None:
        """
        Génère une correction complète avec le LLM.
        Retourne la correction en texte markdown.
        """
        prompt = f"""Tu es un professeur expert du programme scolaire sénégalais.

Génère une correction complète et détaillée de cet exercice.

*Informations :*
- Matière : {matiere}
- Chapitre : {chapitre or 'général'}
- Niveau : {niveau} (1=facile, 2=moyen, 3=difficile)
- Examen : {exam_type} {serie or ''} {annee or ''}

*Exercice à corriger :*
---
{exercise_text}
---

La correction doit :
1. Répondre à chaque question dans l'ordre
2. Montrer toutes les étapes de calcul
3. Justifier chaque étape
4. Donner le résultat final encadré
5. Indiquer les erreurs classiques à éviter
6. Être au niveau du programme sénégalais

Format : correction structurée avec numéros de questions clairs."""

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-large-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 3000,
                    },
                )
                data = response.json()
                if "choices" not in data:
                    return None
                return data["choices"][0]["message"]["content"].strip()

        except Exception as e:
            print(f"Erreur génération correction: {e}")
            return None

    def correction_to_pdf(
        self,
        correction_text: str,
        title: str,
        matiere: str,
        exercise_number: int,
    ) -> bytes | None:
        """
        Convertit une correction texte en PDF propre.
        """
        try:
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)  # A4

            # Couleurs
            color_title = (0.2, 0.4, 0.8)      # Bleu
            color_header = (0.1, 0.6, 0.3)      # Vert
            color_text = (0.1, 0.1, 0.1)        # Noir
            color_bg = (0.95, 0.97, 1.0)        # Bleu très clair

            # Fond header
            page.draw_rect(
                fitz.Rect(0, 0, 595, 80),
                color=None,
                fill=color_bg,
            )

            # Titre principal
            page.insert_text(
                (30, 30),
                f"CORRECTION — {title.upper()}",
                fontsize=14,
                color=color_title,
                fontname="helv",
            )

            # Sous-titre
            page.insert_text(
                (30, 55),
                f"Matière : {matiere.upper()} | Exercice {exercise_number}",
                fontsize=10,
                color=color_header,
                fontname="helv",
            )

            # Ligne séparatrice
            page.draw_line(
                fitz.Point(30, 75),
                fitz.Point(565, 75),
                color=color_title,
                width=1,
            )

            # Contenu de la correction
            y = 95
            lines = correction_text.split("\n")

            for line in lines:
                if y > 800:
                    # Nouvelle page
                    page = doc.new_page(width=595, height=842)
                    y = 30

                line = line.strip()
                if not line:
                    y += 8
                    continue

                # Détecte les titres (lignes commençant par # ou **)
                if line.startswith("##") or line.startswith("**"):
                    line = line.replace("##", "").replace("**", "").strip()
                    page.insert_text(
                        (30, y),
                        line,
                        fontsize=11,
                        color=color_header,
                        fontname="helv",
                    )
                    y += 16
                elif line.startswith("#"):
                    line = line.replace("#", "").strip()
                    page.insert_text(
                        (30, y),
                        line,
                        fontsize=12,
                        color=color_title,
                        fontname="helv",
                    )
                    y += 18
                else:
                    # Texte normal — gère les longues lignes
                    max_chars = 90
                    while len(line) > max_chars:
                        # Coupe à un espace
                        cut = line[:max_chars].rfind(" ")
                        if cut == -1:
                            cut = max_chars
                        page.insert_text(
                            (30, y),
                            line[:cut],
                            fontsize=10,
                            color=color_text,
                            fontname="helv",
                        )
                        y += 14
                        line = line[cut:].strip()
                        if y > 800:
                            page = doc.new_page(width=595, height=842)
                            y = 30

                    if line:
                        page.insert_text(
                            (30, y),
                            line,
                            fontsize=10,
                            color=color_text,
                            fontname="helv",
                        )
                        y += 14

            result = doc.tobytes()
            doc.close()
            return result

        except Exception as e:
            print(f"Erreur génération PDF correction: {e}")
            return None

    async def process_exercise(
        self,
        file_bytes: bytes,
        exercise_data: dict,
        source_filename: str,
        matiere: str,
        exam_type: str,
        serie: str,
        annee: int = None,
    ) -> dict:
        """
        Traite un exercice individuel :
        1. Extrait les pages PDF
        2. Génère ou extrait la correction
        3. Sauvegarde les fichiers
        Retourne les chemins des fichiers générés.
        """
        exercise_number = exercise_data.get("number", 1)
        title = exercise_data.get("title", f"Exercice {exercise_number}")
        page_debut = exercise_data.get("page_debut", 1)
        page_fin = exercise_data.get("page_fin", 1)
        chapitre = exercise_data.get("chapitre")
        niveau = exercise_data.get("niveau", 2)
        has_correction = exercise_data.get("has_correction", False)
        correction_page_debut = exercise_data.get("correction_page_debut")
        correction_page_fin = exercise_data.get("correction_page_fin")

        result = {
            "exercise_number": exercise_number,
            "title": title,
            "exercise_path": None,
            "correction_path": None,
            "correction_generated": False,
            "error": None,
        }

        # Nom de base pour les fichiers
        base_name = f"{exam_type or 'bac'}_{serie or 'gen'}_{matiere}_{chapitre or 'general'}_ex{exercise_number}"
        if annee:
            base_name += f"_{annee}"

        # 1. Extrait le PDF de l'exercice
        print(f"  → Extraction exercice {exercise_number} (pages {page_debut}-{page_fin})")
        exercise_pdf = self.extract_pdf_pages(file_bytes, page_debut, page_fin)

        if exercise_pdf:
            path = self.save_pdf(
                exercise_pdf,
                EXERCISES_DIR / matiere,
                f"{base_name}.pdf",
            )
            result["exercise_path"] = path
            print(f"  → Exercice sauvegardé : {path}")
        else:
            result["error"] = f"Extraction pages {page_debut}-{page_fin} échouée"
            return result

        # 2. Correction
        if has_correction and correction_page_debut and correction_page_fin:
            # Extrait la correction depuis le document
            print(f"  → Extraction correction (pages {correction_page_debut}-{correction_page_fin})")
            correction_pdf = self.extract_pdf_pages(
                file_bytes,
                correction_page_debut,
                correction_page_fin,
            )
            if correction_pdf:
                path = self.save_pdf(
                    correction_pdf,
                    CORRECTIONS_DIR / matiere,
                    f"{base_name}_correction.pdf",
                )
                result["correction_path"] = path
                print(f"  → Correction extraite : {path}")

        else:
            # Génère la correction avec le LLM
            print(f"  → Génération correction LLM pour exercice {exercise_number}...")

            # Extrait le texte de l'exercice pour le LLM
            exercise_text = ""
            try:
                src = fitz.open(stream=exercise_pdf, filetype="pdf")
                for page in src:
                    exercise_text += page.get_text("text")
                src.close()
            except Exception:
                pass

            if exercise_text.strip():
                correction_text = await self.generate_correction(
                    exercise_text=exercise_text,
                    exercise_title=title,
                    matiere=matiere,
                    chapitre=chapitre,
                    niveau=niveau,
                    exam_type=exam_type,
                    serie=serie,
                    annee=annee,
                )

                if correction_text:
                    correction_pdf_bytes = self.correction_to_pdf(
                        correction_text=correction_text,
                        title=title,
                        matiere=matiere,
                        exercise_number=exercise_number,
                    )
                    if correction_pdf_bytes:
                        path = self.save_pdf(
                            correction_pdf_bytes,
                            CORRECTIONS_DIR / matiere,
                            f"{base_name}_correction_gen.pdf",
                        )
                        result["correction_path"] = path
                        result["correction_generated"] = True
                        print(f"  → Correction générée : {path}")

        return result


exercise_extractor_service = ExerciseExtractorService()
