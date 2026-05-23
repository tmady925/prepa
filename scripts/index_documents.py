"""
Script d'indexation en masse des documents depuis un dossier local.

Usage :
    python scripts/index_documents.py --folder documents/
    python scripts/index_documents.py --file cours_maths.pdf --exam bac_senegal --series S2 --subject maths
"""
import asyncio
import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import AsyncSessionLocal
from app.db.redis import connect_redis
from app.services.rag.indexing_service import indexing_service


def parse_folder_path(path: str) -> dict:
    """
    Déduit exam_type, series, subject depuis le chemin du dossier.
    Ex: documents/bac_senegal/s2/maths/ → exam=bac_senegal, series=S2, subject=maths
    """
    parts = path.lower().replace("\\", "/").split("/")
    exam_type = None
    series = None
    subject = None

    exam_types = ["bac_senegal", "bfem", "concours"]
    series_types = ["s1", "s2", "s3", "l1", "l2", "t", "steg"]
    subject_types = ["maths", "physique", "svt", "francais", "philosophie",
                     "histoire_geo", "anglais", "chimie", "informatique"]

    for part in parts:
        if part in exam_types:
            exam_type = part
        elif part in series_types:
            series = part.upper()
        elif part in subject_types:
            subject = part

    return {"exam_type": exam_type, "series": series, "subject": subject}


async def index_file(filepath: str, exam_type: str = None, series: str = None,
                     subject: str = None, doc_type: str = "cours"):
    """Indexe un seul fichier."""
    filename = os.path.basename(filepath)
    title = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ").title()

    print(f"\n📄 {filename}")
    print(f"   Exam: {exam_type} | Série: {series} | Matière: {subject}")

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
        )

    if result["success"]:
        print(f"   ✅ {result['chunks']} chunks | {result['pages']} pages | OCR: {result['has_ocr']}")
    else:
        print(f"   ❌ Erreur: {result['error']}")

    return result


async def index_folder(folder: str, doc_type: str = "cours"):
    """Indexe tous les documents d'un dossier récursivement."""
    supported = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"}
    files = []

    for root, dirs, filenames in os.walk(folder):
        for filename in filenames:
            if any(filename.lower().endswith(ext) for ext in supported):
                files.append(os.path.join(root, filename))

    print(f"\n🔍 {len(files)} fichiers trouvés dans {folder}")
    print("=" * 50)

    success = 0
    errors = 0

    for filepath in files:
        # Déduit le namespace depuis le chemin
        folder_path = os.path.dirname(filepath)
        namespace = parse_folder_path(folder_path)

        result = await index_file(
            filepath=filepath,
            exam_type=namespace["exam_type"],
            series=namespace["series"],
            subject=namespace["subject"],
            doc_type=doc_type,
        )

        if result.get("success"):
            success += 1
        else:
            errors += 1

    print("\n" + "=" * 50)
    print(f"✅ Succès : {success}")
    print(f"❌ Erreurs : {errors}")
    print(f"📊 Total  : {len(files)}")


async def main():
    parser = argparse.ArgumentParser(description="Indexation de documents pour Prepa RAG")
    parser.add_argument("--folder", help="Dossier à indexer récursivement")
    parser.add_argument("--file", help="Fichier unique à indexer")
    parser.add_argument("--exam", help="Type d'examen (bac_senegal, bfem, concours)")
    parser.add_argument("--series", help="Série (S1, S2, L1...)")
    parser.add_argument("--subject", help="Matière (maths, physique, svt...)")
    parser.add_argument("--type", default="cours", help="Type de document (cours, annale, fiche)")

    args = parser.parse_args()

    await connect_redis()

    if args.file:
        await index_file(
            filepath=args.file,
            exam_type=args.exam,
            series=args.series,
            subject=args.subject,
            doc_type=args.type,
        )
    elif args.folder:
        await index_folder(args.folder, doc_type=args.type)
    else:
        print("Usage:")
        print("  python scripts/index_documents.py --folder documents/")
        print("  python scripts/index_documents.py --file cours.pdf --exam bac_senegal --series S2 --subject maths")


if __name__ == "__main__":
    asyncio.run(main())