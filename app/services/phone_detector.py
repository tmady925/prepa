"""
Détection du pays par indicatif téléphonique.
Utilise la librairie phonenumbers — couvre 250+ pays automatiquement.
"""
import phonenumbers
from phonenumbers import geocoder, region_code_for_number

# Mapping code pays ISO → données locales
PAYS_MAP = {
    "SN": {"pays": "senegal", "nom": "Sénégal", "flag": "🇸🇳"},
    "CI": {"pays": "cote_ivoire", "nom": "Côte d'Ivoire", "flag": "🇨🇮"},
    "ML": {"pays": "mali", "nom": "Mali", "flag": "🇲🇱"},
    "BF": {"pays": "burkina_faso", "nom": "Burkina Faso", "flag": "🇧🇫"},
    "BJ": {"pays": "benin", "nom": "Bénin", "flag": "🇧🇯"},
    "TG": {"pays": "togo", "nom": "Togo", "flag": "🇹🇬"},
    "CM": {"pays": "cameroun", "nom": "Cameroun", "flag": "🇨🇲"},
    "GN": {"pays": "guinee", "nom": "Guinée", "flag": "🇬🇳"},
    "MR": {"pays": "mauritanie", "nom": "Mauritanie", "flag": "🇲🇷"},
    "NE": {"pays": "niger", "nom": "Niger", "flag": "🇳🇪"},
    "MA": {"pays": "maroc", "nom": "Maroc", "flag": "🇲🇦"},
    "TN": {"pays": "tunisie", "nom": "Tunisie", "flag": "🇹🇳"},
    "DZ": {"pays": "algerie", "nom": "Algérie", "flag": "🇩🇿"},
    "FR": {"pays": "france", "nom": "France", "flag": "🇫🇷"},
    "GH": {"pays": "ghana", "nom": "Ghana", "flag": "🇬🇭"},
    "NG": {"pays": "nigeria", "nom": "Nigéria", "flag": "🇳🇬"},
}


def detect_pays(phone: str) -> dict | None:
    """
    Détecte le pays depuis le numéro de téléphone.
    Ex: 221763307511 → {"pays": "senegal", "nom": "Sénégal", "flag": "🇸🇳"}
    """
    try:
        phone = phone.strip().lstrip("+")
        parsed = phonenumbers.parse("+" + phone)
        region = region_code_for_number(parsed)
        return PAYS_MAP.get(region)
    except Exception:
        return None


def get_pays_name(pays_code: str) -> str:
    """Retourne le nom du pays depuis son code interne."""
    for data in PAYS_MAP.values():
        if data["pays"] == pays_code:
            return f"{data['flag']} {data['nom']}"
    return pays_code
