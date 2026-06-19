"""
Échelle de qualification (diplômes du Sénégal) — source de vérité du matching niveau.
=====================================================================================

OBJECTIF : comparer le niveau d'un candidat à l'exigence d'une offre, peu importe
comment c'est écrit (« j'ai un BTS », « bac +2 », « niveau Licence », « Master min »…).

ÉCHELLE (rang 0 → 9), validée pour le contexte sénégalais :
  0  aucun / sans diplôme
  1  CFEE (fin élémentaire)
  2  CAP, CQP, DEP (pro court, après 3e/BFEM)
  3  BFEM, BEP (fin collège / pro)
  4  BT, BAC (général/technique), Bac Pro
  5  BTS, DTS, DUT, DEUG (bac+2)
  6  Licence, Licence pro (bac+3)
  7  Maîtrise, Master 1 (bac+4)
  8  Master 2, Diplôme d'ingénieur, DESS, DEA (bac+5)
  9  Doctorat / PhD (bac+8)

RÈGLE DE MATCHING : « sensiblement supérieur ou égal » → on tolère 1 cran en dessous.
  rang_candidat >= rang_exigé - 1   ⇒ éligible
  (un BFEM(3) reste bloqué pour une Licence(6) ; un BTS(5) passe.)

API :
  normalize_niveau(texte) -> (libellé_canonique | None, rang | None)
  meets_requirement(niveau_candidat, niveau_exigé, tolerance=1) -> bool
  rank_label(rang) -> str
"""

import re
import unicodedata


def _norm(s: str | None) -> str:
    """minuscule, sans accents, espaces normalisés."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Libellé lisible par rang (pour les messages).
_RANK_LABELS = {
    0: "aucun diplôme",
    1: "CFEE",
    2: "CAP",
    3: "BFEM",
    4: "BAC",
    5: "BTS / bac+2",
    6: "Licence / bac+3",
    7: "Maîtrise / bac+4",
    8: "Master / bac+5",
    9: "Doctorat",
}

# Rang → libellé canonique (pour bac+N).
_RANK_CANON = {
    0: "aucun", 1: "cfee", 2: "cap", 3: "bfem", 4: "bac",
    5: "bac+2", 6: "bac+3", 7: "bac+4", 8: "bac+5", 9: "doctorat",
}

# bac+N → rang.
_BACPLUS_TO_RANK = {0: 4, 1: 4, 2: 5, 3: 6, 4: 7, 5: 8, 6: 8, 7: 8, 8: 9}

# Patterns ORDONNÉS du plus spécifique/élevé au plus général.
# Le PREMIER qui matche gagne → l'ordre est crucial (ex. "bts" avant "bt"/"bac",
# "brevet de technicien superieur" avant "brevet de technicien").
_PATTERNS: list[tuple[int, str]] = [
    (9, r"\b(doctorat|docteur|phd|ph\.?d\.?|these de doctorat)\b"),
    (8, r"\b(master\s*2|master2|\bm2\b|dess|dea|ingenieur|diplome d'?ingenieur)\b"),
    (7, r"\b(maitrise|master\s*1|master1|\bm1\b)\b"),
    (8, r"\b(master|mastere)\b"),
    (6, r"\b(licence pro\w*|licence professionnelle|licence|\bl3\b)\b"),
    (5, r"\b(brevet de technicien superieur|technicien superieur|\bbts\b|\bdts\b|\bdut\b|deug)\b"),
    (4, r"\b(bac pro\w*|bac technique|baccalaureat|\bbac\b|brevet de technicien|\bbt\b)\b"),
    (3, r"\b(bfem|brevet de fin d'?etudes moyennes|\bbep\b|brevet d'?etudes professionnelles|brevet des colleges|\bbrevet\b|3eme|troisieme)\b"),
    (2, r"\b(certificat d'?aptitude professionnelle|\bcap\b|\bcqp\b|\bdep\b|certificat de qualification)\b"),
    (1, r"\b(cfee|certificat de fin d'?etudes elementaires|\bcep\b|fin d'?etudes elementaires|niveau primaire)\b"),
    (0, r"\b(aucun diplome|sans diplome|pas de diplome|non scolarise|aucun|neant)\b"),
]


def normalize_niveau(text: str | None) -> tuple[str | None, int | None]:
    """
    Mappe un texte libre de niveau vers (libellé_canonique, rang).
    Retourne (None, None) si rien de reconnaissable.
    """
    if not text:
        return (None, None)
    t = _norm(text)

    # 1) Forme explicite "bac+N" / "bac +N" / "bac + N"
    m = re.search(r"bac\s*\+\s*(\d+)", t)
    if m:
        n = int(m.group(1))
        rank = _BACPLUS_TO_RANK.get(n, 8 if n >= 5 else 4)
        return (_RANK_CANON[rank], rank)

    # 2) Patterns ordonnés
    for rank, rx in _PATTERNS:
        if re.search(rx, t):
            return (_RANK_CANON[rank], rank)

    return (None, None)


def rank_label(rank: int | None) -> str:
    if rank is None:
        return "niveau non précisé"
    return _RANK_LABELS.get(rank, "niveau non précisé")


def meets_requirement(
    niveau_candidat: str | None,
    niveau_exige: str | None,
    tolerance: int = 1,
) -> bool:
    """
    « Sensiblement supérieur ou égal ».

    - Offre sans exigence reconnaissable → True (pas de barrière).
    - Candidat sans niveau reconnaissable → True (on ne bloque pas sur l'inconnu ;
      le CV + le LLM jugeront). La sous-qualification ne bloque que si les DEUX
      niveaux sont connus.
    - Sinon : rang_candidat >= rang_exigé - tolerance.
    """
    _, req = normalize_niveau(niveau_exige)
    if req is None:
        return True
    _, cand = normalize_niveau(niveau_candidat)
    if cand is None:
        return True
    return cand >= req - tolerance
