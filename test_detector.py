import asyncio
from app.services.rag.detector import subject_detector

async def test():
    tests = [
        "Explique moi comment calculer la derivee d un produit",
        "Je comprends pas la radioactivite et la desintegration alpha",
        "Donne moi un exercice sur les probabilites",
        "Comment resoudre une equation du second degre",
        "La photosynthese comment ca marche",
        "Explique la loi d ohm et les circuits electriques",
        "Waw les dérivées man je comprends rien du tout",
    ]

    for t in tests:
        result = await subject_detector.detect(t, {"exam_type": "bac_senegal", "serie": "S2"})
        print(f"Texte: {t[:55]}")
        print(f"  Matiere: {result['matiere']} | Chapitre: {result['chapitre']} | Confiance: {result['confiance']}")
        print()

asyncio.run(test())