"""
Détection intelligente des choix dans les menus WhatsApp.
Couvre : ID bouton, numéro, texte libre, partiel, avec/sans accents.
"""
import unicodedata
import re


def normalize(text: str) -> str:
    """Normalise le texte : minuscules, sans accents, sans ponctuation."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_choice(text: str, choices: list[dict]) -> dict | None:
    """
    Détecte le choix de l'élève parmi une liste.

    choices = [
        {"id": "exam_bac_senegal", "title": "BAC Sénégal", "value": "bac_senegal"},
        {"id": "exam_bfem", "title": "BFEM", "value": "bfem"},
        ...
    ]

    Retourne le dict du choix trouvé ou None.
    """
    if not text or not choices:
        return None

    text_norm = normalize(text)

    # 1. Correspondance exacte sur l'ID
    for choice in choices:
        if text.strip() == choice.get("id", ""):
            return choice

    # 2. Correspondance par numéro (1, 2, 3...)
    if text.strip().isdigit():
        idx = int(text.strip()) - 1
        if 0 <= idx < len(choices):
            return choices[idx]

    # 3. Correspondance exacte sur la valeur
    for choice in choices:
        if text.strip() == choice.get("value", ""):
            return choice

    # 4. Correspondance exacte normalisée sur le titre
    for choice in choices:
        title_norm = normalize(choice.get("title", ""))
        if text_norm == title_norm:
            return choice

    # 5. Correspondance exacte normalisée sur la valeur
    for choice in choices:
        value_norm = normalize(choice.get("value", "").replace("_", " "))
        if text_norm == value_norm:
            return choice

    # 6. Le texte contient le titre (ou inverse)
    for choice in choices:
        title_norm = normalize(choice.get("title", ""))
        if title_norm and (title_norm in text_norm or text_norm in title_norm):
            return choice

    # 7. Le texte contient la valeur normalisée
    for choice in choices:
        value_norm = normalize(choice.get("value", "").replace("_", " "))
        if value_norm and (value_norm in text_norm or text_norm in value_norm):
            return choice

    # 8. Correspondance partielle sur les mots clés du titre
    for choice in choices:
        title_words = normalize(choice.get("title", "")).split()
        text_words = text_norm.split()
        # Si au moins 1 mot significatif (>2 chars) correspond
        matches = [w for w in title_words if len(w) > 2 and w in text_words]
        if matches:
            return choice

    return None
