"""
Script d'indexation en masse des documents depuis un dossier local.
Lit les metadata.json générés par organize_documents.py si disponibles.

Usage :
    python scripts/index_documents.py --folder documents_organises/
    python scripts/index_documents.py --file cours.pdf --exam bac_senegal --series S2 --subject maths --type cours
"""
import asyncio
import argparse
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import AsyncSessionLocal
from app.db.redis import connect_redis
from app.services.rag.indexing_service import indexing_service


def load_metadata_json(filepath: str) -> dict | None:
    """
    Cherche un fichier .json à côté du document.
    Ex: cours.pdf → cours.json
    Retourne le dict si trouvé et valide, sinon None.
    """
    json_path = os.path.splitext(filepath)[0] + ".json"
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"  ⚠ JSON invalide {json_path}: {e}")
    return None


def parse_folder_path(path: str) -> dict:
    """
    Déduit exam_type, series, subject, doc_type, annee depuis le chemin.
    Fallback si pas de metadata.json.
    """
    parts = path.lower().replace("\\", "/").split("/")

    exam_types = ["bac_senegal", "bfem", "concours"]
    series_types = ["s1", "s2", "s3", "l1", "l2", "t", "steg"]
    subject_types = ["maths", "physique", "svt", "francais", "philosophie",
                     "histoire_geo", "anglais", "chimie", "informatique", "physique_chimie"]
    doc_type_map = {
        "annales": "annale", "annale": "annale",
        "corrections": "correction", "correction": "correction",
        "corriges": "correction", "corrige": "correction",
        "series": "serie", "serie": "serie",
        "devoirs": "devoir", "devoir": "devoir",
        "fiches": "fiche", "fiche": "fiche",
        "exercices": "exercice", "exercice": "exercice",
        "cours": "cours",
    }

    result = {
        "exam_type": None, "series": None, "subject": None,
        "doc_type": "cours", "chapitre": None, "annee": None,
        "niveau": 2, "title": None,
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
    title: str = None,
):
    """Indexe un seul fichier."""
    filename = os.path.basename(filepath)
    if not title:
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
    """
    Indexe tous les documents d'un dossier récursivement.
    Priorité : metadata.json → chemin du dossier.
    """
    supported = {".pdf", ".docx", ".doc"}
    files = []

    for root, dirs, filenames in os.walk(folder):
        for filename in filenames:
            if any(filename.lower().endswith(ext) for ext in supported):
                files.append(os.path.join(root, filename))

    print(f"\n🔍 {len(files)} fichiers trouvés dans {folder}")
    print("=" * 60)

    success = 0
    errors = 0
    skipped = 0

    for filepath in files:
        # 1. Essaie de lire le metadata.json
        meta = load_metadata_json(filepath)

        if meta:
            # Utilise les métadonnées du JSON
            result = await index_file(
                filepath=filepath,
                exam_type=meta.get("exam_type"),
                series=meta.get("serie"),
                subject=meta.get("matiere"),
                doc_type=doc_type or meta.get("doc_type", "cours"),
                chapitre=meta.get("chapitre"),
                annee=meta.get("annee"),
                niveau=meta.get("niveau", 2),
                pays=meta.get("pays", pays),
                title=meta.get("title"),
            )
        else:
            # Fallback : déduit depuis le chemin
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
                niveau=namespace["niveau"],
                pays=pays,
            )

        if result.get("success"):
            success += 1
        else:
            errors += 1

    print("\n" + "=" * 60)
    print(f"✅ Succès  : {success}")
    print(f"❌ Erreurs : {errors}")
    print(f"📊 Total   : {len(files)}")


async def main():
    parser = argparse.ArgumentParser(
        description="Indexation de documents pour Prepa RAG",
    )
    parser.add_argument("--folder", help="Dossier à indexer récursivement")
    parser.add_argument("--file", help="Fichier unique à indexer")
    parser.add_argument("--exam", help="Type d'examen : bac_senegal | bfem | concours")
    parser.add_argument("--series", help="Série : S1 | S2 | S3 | L1 | L2 | T")
    parser.add_argument("--subject", help="Matière : maths | physique_chimie | svt | francais...")
    parser.add_argument("--type", default="cours", help="Type : cours | annale | correction | serie | exercice | fiche")
    parser.add_argument("--chapitre", help="Chapitre : derivees | probabilites | genetique...")
    parser.add_argument("--annee", type=int, help="Année (ex: 2023)")
    parser.add_argument("--niveau", type=int, default=2, help="Difficulté : 1 | 2 | 3")
    parser.add_argument("--source", help="Source : INEADE | FASTEF...")
    parser.add_argument("--pays", default="senegal", help="Pays (défaut: senegal)")

    args = parser.parse_args()

    await connect_redis()

    if args.file:
        # Essaie de lire le JSON si disponible
        meta = load_metadata_json(args.file)
        await index_file(
            filepath=args.file,
            exam_type=args.exam or (meta.get("exam_type") if meta else None),
            series=args.series or (meta.get("serie") if meta else None),
            subject=args.subject or (meta.get("matiere") if meta else None),
            doc_type=args.type or (meta.get("doc_type") if meta else "cours"),
            chapitre=args.chapitre or (meta.get("chapitre") if meta else None),
            annee=args.annee or (meta.get("annee") if meta else None),
            niveau=args.niveau,
            source=args.source,
            pays=args.pays,
            title=meta.get("title") if meta else None,
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