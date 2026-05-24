"""
Script d'indexation en masse des documents depuis un dossier local.

Usage :
    python scripts/index_documents.py --folder documents/
    python scripts/index_documents.py --file cours.pdf --exam bac_senegal --series S2 --subject maths --type cours
    python scripts/index_documents.py --file annale_2023.pdf --exam bac_senegal --series S2 --subject maths --type annale --annee 2023
    python scripts/index_documents.py --file corrige.pdf --exam bac_senegal --series S2 --subject maths --type correction
"""
import asyncio
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import AsyncSessionLocal
from app.db.redis import connect_redis
from app.services.rag.indexing_service import indexing_service

# Types de documents supportés
DOC_TYPES = [
    "cours",       # Cours officiel
    "annale",      # Sujet d'examen passé
    "correction",  # Corrigé officiel
    "serie",       # Série d'exercices
    "devoir",      # Devoir surveillé
    "fiche",       # Fiche de révision
    "exercice",    # Exercices d'application
]


def parse_folder_path(path: str) -> dict:
    """
    Déduit exam_type, series, subject, doc_type, annee depuis le chemin.
    Ex: documents/bac_senegal/s2/maths/annales/2023/
    → exam=bac_senegal, series=S2, subject=maths, doc_type=annale, annee=2023
    """
    parts = path.lower().replace("\\", "/").split("/")

    exam_types = ["bac_senegal", "bfem", "concours"]
    series_types = ["s1", "s2", "s3", "l1", "l2", "t", "steg"]
    subject_types = ["maths", "physique", "svt", "francais", "philosophie",
                     "histoire_geo", "anglais", "chimie", "informatique", "physique_chimie"]
    doc_type_map = {
        "annales": "annale",
        "annale": "annale",
        "corrections": "correction",
        "correction": "correction",
        "corriges": "correction",
        "corrige": "correction",
        "series": "serie",
        "serie": "serie",
        "devoirs": "devoir",
        "devoir": "devoir",
        "fiches": "fiche",
        "fiche": "fiche",
        "exercices": "exercice",
        "exercice": "exercice",
        "cours": "cours",
    }
    chapitre_types = [
        "derivees", "probabilites", "suites", "fonctions", "equations",
        "vecteurs", "geometrie", "statistiques", "mecanique", "electricite",
        "optique", "nucleaire", "oscillations", "acides_bases",
        "reactions_chimiques", "chimie_organique", "genetique",
        "photosynthese", "neurologie",
    ]

    result = {
        "exam_type": None,
        "series": None,
        "subject": None,
        "doc_type": "cours",
        "chapitre": None,
        "annee": None,
    }

    for part in parts:
        if part in exam_types:
            result["exam_type"] = part
        elif part in series_types:
            result["series"] = part.upper()
        elif part in subject_types:
            result["subject"] = part
        elif part in doc_type_map:
            result["doc_type"] = doc_type_map[part]
        elif part in chapitre_types:
            result["chapitre"] = part
        elif part.isdigit() and len(part) == 4:
            result["annee"] = int(part)

    return result


async def index_file(
    filepath: str,
    exam_type: str = None,
    series: str = None,
    subject: str = None,
    doc_type: str = "cours",
    chapitre: str = None,
    annee: int = None,
    niveau: int = 2,
    source: str = None,
    pays: str = "senegal",
):
    """Indexe un seul fichier."""
    filename = os.path.basename(filepath)
    title = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ").title()

    print(f"\n📄 {filename}")
    print(f"   Exam: {exam_type} | Série: {series} | Matière: {subject} | Type: {doc_type}")
    if chapitre:
        print(f"   Chapitre: {chapitre}")
    if annee:
        print(f"   Année: {annee}")

    with open(filepath, "rb") as f:
        file_bytes = f.read()

    async with AsyncSessionLocal() as db:
        result = await indexing_service.index_document(
            db=db,
            file_bytes=file_bytes,
            filename=filename,
            title=title,
            exam_type=exam_type,
            series=series,
            subject=subject,
            doc_type=doc_type,
            chapitre=chapitre,
            annee=annee,
            niveau=niveau,
            source=source,
            pays=pays,
        )

    if result["success"]:
        print(f"   ✅ {result['chunks']} chunks | {result['pages']} pages | OCR: {result['has_ocr']}")
    else:
        print(f"   ❌ Erreur: {result['error']}")

    return result


async def index_folder(folder: str, doc_type: str = None, pays: str = "senegal"):
    """Indexe tous les documents d'un dossier récursivement."""
    supported = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"}
    files = []

    for root, dirs, filenames in os.walk(folder):
        for filename in filenames:
            if any(filename.lower().endswith(ext) for ext in supported):
                files.append(os.path.join(root, filename))

    print(f"\n🔍 {len(files)} fichiers trouvés dans {folder}")
    print("=" * 60)

    success = 0
    errors = 0

    for filepath in files:
        folder_path = os.path.dirname(filepath)
        namespace = parse_folder_path(folder_path)

        result = await index_file(
            filepath=filepath,
            exam_type=namespace["exam_type"],
            series=namespace["series"],
            subject=namespace["subject"],
            doc_type=doc_type or namespace["doc_type"],
            chapitre=namespace["chapitre"],
            annee=namespace["annee"],
            pays=pays,
        )

        if result.get("success"):
            success += 1
        else:
            errors += 1

    print("\n" + "=" * 60)
    print(f"✅ Succès : {success}")
    print(f"❌ Erreurs : {errors}")
    print(f"📊 Total  : {len(files)}")


async def main():
    parser = argparse.ArgumentParser(
        description="Indexation de documents pour Prepa RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  # Cours
  python scripts/index_documents.py --file cours_derivees.pdf --exam bac_senegal --series S2 --subject maths --type cours --chapitre derivees

  # Annale avec année
  python scripts/index_documents.py --file bac_s2_maths_2023.pdf --exam bac_senegal --series S2 --subject maths --type annale --annee 2023

  # Corrigé
  python scripts/index_documents.py --file corrige_2023.pdf --exam bac_senegal --series S2 --subject maths --type correction --annee 2023

  # Série d'exercices
  python scripts/index_documents.py --file series_derivees.pdf --exam bac_senegal --series S2 --subject maths --type serie --chapitre derivees

  # Dossier complet (structure auto-détectée)
  python scripts/index_documents.py --folder documents/

Types de documents : cours | annale | correction | serie | devoir | fiche | exercice
        """
    )

    parser.add_argument("--folder", help="Dossier à indexer récursivement")
    parser.add_argument("--file", help="Fichier unique à indexer")
    parser.add_argument("--exam", help="Type d'examen : bac_senegal | bfem | concours")
    parser.add_argument("--series", help="Série : S1 | S2 | S3 | L1 | L2 | T")
    parser.add_argument("--subject", help="Matière : maths | physique | svt | chimie | francais...")
    parser.add_argument("--type", default="cours", help="Type : cours | annale | correction | serie | devoir | fiche | exercice")
    parser.add_argument("--chapitre", help="Chapitre : derivees | probabilites | nucleaire | genetique...")
    parser.add_argument("--annee", type=int, help="Année de l'examen (ex: 2023)")
    parser.add_argument("--niveau", type=int, default=2, help="Difficulté : 1=facile | 2=moyen | 3=difficile")
    parser.add_argument("--source", help="Source : INEADE | FASTEF | etc.")
    parser.add_argument("--pays", default="senegal", help="Pays (défaut: senegal)")

    args = parser.parse_args()

    await connect_redis()

    if args.file:
        await index_file(
            filepath=args.file,
            exam_type=args.exam,
            series=args.series,
            subject=args.subject,
            doc_type=args.type,
            chapitre=args.chapitre,
            annee=args.annee,
            niveau=args.niveau,
            source=args.source,
            pays=args.pays,
        )
    elif args.folder:
        await index_folder(
            folder=args.folder,
            doc_type=args.type if args.type != "cours" else None,
            pays=args.pays,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())