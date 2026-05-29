"""
Service d'extraction et de génération des PDFs d'exercices et corrections.
Lit le texte de chaque exercice et génère un PDF propre avec ReportLab.
"""
import re
from pathlib import Path
from datetime import datetime
import httpx
import fitz  # PyMuPDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from app.core.settings import get_settings

settings = get_settings()

EXERCISES_DIR = Path("/home/prepa/app/exercises")
CORRECTIONS_DIR = Path("/home/prepa/app/corrections")
EXERCISES_DIR.mkdir(parents=True, exist_ok=True)
CORRECTIONS_DIR.mkdir(parents=True, exist_ok=True)


class ExerciseExtractorService:

    def extract_text_from_pages(
        self,
        file_bytes: bytes,
        page_debut: int,
        page_fin: int,
    ) -> str:
        """Extrait le texte brut des pages page_debut à page_fin."""
        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            text = ""
            total = len(doc)
            start = max(0, page_debut - 1)
            end = min(total, page_fin)
            for i in range(start, end):
                text += doc[i].get_text("text") + "\n"
            doc.close()
            return text.strip()
        except Exception as e:
            print(f"Erreur extraction texte pages {page_debut}-{page_fin}: {e}")
            return ""

    def generate_exercise_pdf(
        self,
        exercise_text: str,
        title: str,
        matiere: str,
        serie: str,
        exam_type: str,
        exercise_number: int,
        annee: int = None,
        chapitre: str = None,
        bareme: dict = None,
    ) -> bytes | None:
        """Génère un PDF propre pour un exercice avec ReportLab."""
        try:
            from io import BytesIO
            buffer = BytesIO()

            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                rightMargin=2*cm,
                leftMargin=2*cm,
                topMargin=2*cm,
                bottomMargin=2*cm,
            )

            styles = getSampleStyleSheet()

            # Styles personnalisés
            style_title = ParagraphStyle(
                'ExoTitle',
                parent=styles['Heading1'],
                fontSize=14,
                textColor=colors.HexColor('#1a3a6b'),
                spaceAfter=6,
                spaceBefore=0,
                alignment=TA_CENTER,
            )
            style_subtitle = ParagraphStyle(
                'ExoSubtitle',
                parent=styles['Normal'],
                fontSize=10,
                textColor=colors.HexColor('#2d6a4f'),
                spaceAfter=12,
                alignment=TA_CENTER,
            )
            style_body = ParagraphStyle(
                'ExoBody',
                parent=styles['Normal'],
                fontSize=11,
                leading=16,
                spaceAfter=8,
                alignment=TA_JUSTIFY,
                fontName='Helvetica',
            )
            style_question = ParagraphStyle(
                'ExoQuestion',
                parent=styles['Normal'],
                fontSize=11,
                leading=16,
                spaceAfter=6,
                leftIndent=20,
                fontName='Helvetica-Bold',
            )

            story = []

            # En-tête
            annee_str = f" — {annee}" if annee else ""
            serie_str = f" Série {serie}" if serie else ""
            exam_str = exam_type.replace("_", " ").upper() if exam_type else "BAC"

            story.append(Paragraph(
                f"EXERCICE {exercise_number} — {matiere.upper().replace('_', '-')}",
                style_title
            ))
            story.append(Paragraph(
                f"{exam_str}{serie_str}{annee_str}",
                style_subtitle
            ))
            if chapitre:
                story.append(Paragraph(
                    f"Chapitre : {chapitre.replace('_', ' ').title()}",
                    style_subtitle
                ))
            if bareme and bareme.get("total"):
                story.append(Paragraph(
                    f"Barème : {bareme['total']} points",
                    style_subtitle
                ))

            story.append(HRFlowable(
                width="100%",
                thickness=2,
                color=colors.HexColor('#1a3a6b'),
                spaceAfter=16,
            ))

            # Contenu de l'exercice
            lines = exercise_text.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    story.append(Spacer(1, 6))
                    continue

                # Détecte les questions (lignes commençant par 1., 2., a), b)...)
                if re.match(r'^[\d]+[\.\)]\s+', line) or re.match(r'^[a-zA-Z][\.\)]\s+', line):
                    # Échappe les caractères spéciaux ReportLab
                    line_safe = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(line_safe, style_question))
                else:
                    line_safe = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(line_safe, style_body))

            # Pied de page
            story.append(Spacer(1, 20))
            story.append(HRFlowable(
                width="100%",
                thickness=1,
                color=colors.HexColor('#94a3b8'),
                spaceAfter=8,
            ))
            story.append(Paragraph(
                f"<i>Prepa — Révisions {exam_str}{annee_str}</i>",
                ParagraphStyle('Footer', parent=styles['Normal'],
                              fontSize=8, textColor=colors.grey,
                              alignment=TA_CENTER)
            ))

            doc.build(story)
            return buffer.getvalue()

        except Exception as e:
            print(f"Erreur génération PDF exercice: {e}")
            return None

    def generate_correction_pdf(
        self,
        correction_text: str,
        title: str,
        matiere: str,
        serie: str,
        exam_type: str,
        exercise_number: int,
        annee: int = None,
        chapitre: str = None,
    ) -> bytes | None:
        """Génère un PDF propre pour une correction avec ReportLab."""
        try:
            from io import BytesIO
            buffer = BytesIO()

            doc = SimpleDocTemplate(
                buffer,
                pagesize=A4,
                rightMargin=2*cm,
                leftMargin=2*cm,
                topMargin=2*cm,
                bottomMargin=2*cm,
            )

            styles = getSampleStyleSheet()

            style_title = ParagraphStyle(
                'CorrTitle',
                parent=styles['Heading1'],
                fontSize=14,
                textColor=colors.HexColor('#065f46'),
                spaceAfter=6,
                alignment=TA_CENTER,
            )
            style_subtitle = ParagraphStyle(
                'CorrSubtitle',
                parent=styles['Normal'],
                fontSize=10,
                textColor=colors.HexColor('#1a3a6b'),
                spaceAfter=12,
                alignment=TA_CENTER,
            )
            style_body = ParagraphStyle(
                'CorrBody',
                parent=styles['Normal'],
                fontSize=11,
                leading=16,
                spaceAfter=8,
                alignment=TA_JUSTIFY,
            )
            style_step = ParagraphStyle(
                'CorrStep',
                parent=styles['Normal'],
                fontSize=11,
                leading=16,
                spaceAfter=6,
                leftIndent=20,
                textColor=colors.HexColor('#065f46'),
                fontName='Helvetica-Bold',
            )
            style_result = ParagraphStyle(
                'CorrResult',
                parent=styles['Normal'],
                fontSize=11,
                leading=16,
                spaceAfter=8,
                leftIndent=20,
                backColor=colors.HexColor('#d1fae5'),
                fontName='Helvetica-Bold',
            )

            story = []

            annee_str = f" — {annee}" if annee else ""
            serie_str = f" Série {serie}" if serie else ""
            exam_str = exam_type.replace("_", " ").upper() if exam_type else "BAC"

            story.append(Paragraph(
                f"CORRECTION — EXERCICE {exercise_number} — {matiere.upper().replace('_', '-')}",
                style_title
            ))
            story.append(Paragraph(
                f"{exam_str}{serie_str}{annee_str}",
                style_subtitle
            ))

            story.append(HRFlowable(
                width="100%",
                thickness=2,
                color=colors.HexColor('#065f46'),
                spaceAfter=16,
            ))

            # Contenu correction
            lines = correction_text.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    story.append(Spacer(1, 6))
                    continue

                line_safe = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

                # Résultats encadrés
                if line.startswith("**") and line.endswith("**"):
                    line_safe = line_safe.replace('**', '')
                    story.append(Paragraph(line_safe, style_result))
                # Étapes numérotées
                elif re.match(r'^[\d]+[\.\)]\s+', line) or re.match(r'^[a-zA-Z][\.\)]\s+', line):
                    story.append(Paragraph(line_safe, style_step))
                else:
                    story.append(Paragraph(line_safe, style_body))

            story.append(Spacer(1, 20))
            story.append(HRFlowable(
                width="100%",
                thickness=1,
                color=colors.HexColor('#94a3b8'),
                spaceAfter=8,
            ))
            story.append(Paragraph(
                f"<i>Prepa — Correction officielle {exam_str}{annee_str}</i>",
                ParagraphStyle('Footer', parent=styles['Normal'],
                              fontSize=8, textColor=colors.grey,
                              alignment=TA_CENTER)
            ))

            doc.build(story)
            return buffer.getvalue()

        except Exception as e:
            print(f"Erreur génération PDF correction: {e}")
            return None

    def save_pdf(self, pdf_bytes: bytes, folder: Path, filename: str) -> str | None:
        """Sauvegarde un PDF sur le serveur."""
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / filename
            path.write_bytes(pdf_bytes)
            # Assure les permissions Nginx
            path.chmod(0o644)
            return str(path)
        except Exception as e:
            print(f"Erreur sauvegarde PDF: {e}")
            return None

    async def generate_correction_llm(
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
        """Génère une correction complète avec Mistral."""
        prompt = f"""Tu es un professeur expert du programme scolaire sénégalais.

Génère une correction complète et détaillée de cet exercice.

Informations :
- Matière : {matiere}
- Chapitre : {chapitre or 'général'}
- Niveau : {niveau}
- Examen : {exam_type} {serie or ''} {annee or ''}

Exercice :
---
{exercise_text}
---

La correction doit :
1. Répondre à chaque question dans l'ordre avec le numéro de question
2. Montrer toutes les étapes de calcul
3. Justifier chaque étape
4. Encadrer le résultat final avec **résultat**
5. Être au niveau du programme sénégalais BAC"""

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
            print(f"Erreur génération correction LLM: {e}")
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
        Traite un exercice :
        1. Extrait le texte des pages
        2. Génère un PDF propre avec ReportLab
        3. Génère ou extrait la correction
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
        bareme = exercise_data.get("bareme")

        result = {
            "exercise_number": exercise_number,
            "title": title,
            "exercise_path": None,
            "correction_path": None,
            "correction_generated": False,
            "error": None,
        }

        # Nom de base
        serie_clean = re.sub(r'[^A-Z0-9]', '', (serie or 'gen').upper())
        base_name = f"{exam_type or 'bac'}_{serie_clean}_{matiere}_{chapitre or 'general'}_ex{exercise_number}"
        if annee:
            base_name += f"_{annee}"

        # 1. Extrait le texte de l'exercice
        print(f"  → Extraction texte exercice {exercise_number} (pages {page_debut}-{page_fin})")
        exercise_text = self.extract_text_from_pages(file_bytes, page_debut, page_fin)

        if not exercise_text.strip():
            result["error"] = f"Texte vide pages {page_debut}-{page_fin}"
            return result

        print(f"  → {len(exercise_text)} caractères extraits")

        # 2. Génère le PDF exercice avec ReportLab
        print(f"  → Génération PDF exercice {exercise_number}...")
        exercise_pdf_bytes = self.generate_exercise_pdf(
            exercise_text=exercise_text,
            title=title,
            matiere=matiere,
            serie=serie,
            exam_type=exam_type,
            exercise_number=exercise_number,
            annee=annee,
            chapitre=chapitre,
            bareme=bareme,
        )

        if exercise_pdf_bytes:
            path = self.save_pdf(
                exercise_pdf_bytes,
                EXERCISES_DIR / matiere,
                f"{base_name}.pdf",
            )
            result["exercise_path"] = path
            print(f"  → Exercice PDF généré : {path}")
        else:
            result["error"] = "Génération PDF exercice échouée"
            return result

        # 3. Correction
        if has_correction and correction_page_debut and correction_page_fin:
            # Extrait le texte de la correction
            print(f"  → Extraction correction (pages {correction_page_debut}-{correction_page_fin})")
            correction_text = self.extract_text_from_pages(
                file_bytes, correction_page_debut, correction_page_fin
            )
            if correction_text.strip():
                correction_pdf_bytes = self.generate_correction_pdf(
                    correction_text=correction_text,
                    title=title,
                    matiere=matiere,
                    serie=serie,
                    exam_type=exam_type,
                    exercise_number=exercise_number,
                    annee=annee,
                    chapitre=chapitre,
                )
                if correction_pdf_bytes:
                    path = self.save_pdf(
                        correction_pdf_bytes,
                        CORRECTIONS_DIR / matiere,
                        f"{base_name}_correction.pdf",
                    )
                    result["correction_path"] = path
                    print(f"  → Correction PDF générée : {path}")
        else:
            # Génère la correction avec LLM
            print(f"  → Génération correction LLM...")
            correction_text = await self.generate_correction_llm(
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
                correction_pdf_bytes = self.generate_correction_pdf(
                    correction_text=correction_text,
                    title=title,
                    matiere=matiere,
                    serie=serie,
                    exam_type=exam_type,
                    exercise_number=exercise_number,
                    annee=annee,
                    chapitre=chapitre,
                )
                if correction_pdf_bytes:
                    path = self.save_pdf(
                        correction_pdf_bytes,
                        CORRECTIONS_DIR / matiere,
                        f"{base_name}_correction_gen.pdf",
                    )
                    result["correction_path"] = path
                    result["correction_generated"] = True
                    print(f"  → Correction LLM générée : {path}")

        return result


exercise_extractor_service = ExerciseExtractorService()
