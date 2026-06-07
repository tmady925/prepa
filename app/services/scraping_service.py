"""
Service de scraping des offres d'emploi.
Sources : senjob.com (httpx direct) + emploidakar.com (via Zyte API)
"""
import hashlib
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.settings import get_settings
from app.models.job_opportunity import JobOpportunity

settings = get_settings()


# ── Helpers ────────────────────────────────────────────────────────────

def clean_text(s: str | None) -> str | None:
    """
    Nettoie un texte scrapé pour qu'il soit sûr en DB et dans un prompt LLM :
    - supprime les caractères de contrôle / invisibles (zero-width, etc.)
    - normalise les guillemets typographiques en apostrophe simple
    - remplace retours/tabulations par des espaces, compacte les espaces
    """
    if not s:
        return s
    # Supprime les caractères de contrôle (catégorie Unicode "C*") sauf espace usuel
    s = "".join(ch for ch in s if ch in ("\n", "\t", " ") or unicodedata.category(ch)[0] != "C")
    s = s.replace(" ", " ")  # espace insécable
    s = re.sub(r"[\r\n\t]+", " ", s)
    # Guillemets / apostrophes typographiques → apostrophe simple (sûr pour JSON)
    s = re.sub(r"[“”„‟″\"]", "'", s)
    s = re.sub(r"[‘’‚‛′`]", "'", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize(text: str) -> str:
    """Normalise un texte pour le hash : minuscules, sans accents, sans ponctuation."""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _content_hash(titre: str, entreprise: str, localisation: str, annee: int) -> str:
    """Hash de déduplication incluant l'année pour permettre les rééditions annuelles."""
    content = f"{_normalize(titre)}|{_normalize(entreprise)}|{_normalize(localisation)}|{annee}"
    return hashlib.sha256(content.encode()).hexdigest()


def _extract_annee(text: str) -> int:
    """Extrait l'année d'un texte ou retourne l'année courante."""
    match = re.search(r'20\d{2}', text)
    if match:
        return int(match.group())
    return datetime.now().year


def _detect_type_contrat(text: str) -> str | None:
    text_up = text.upper()
    if "CDI" in text_up:
        return "CDI"
    if "CDD" in text_up:
        return "CDD"
    if "STAGE" in text_up:
        return "Stage"
    if "FREELANCE" in text_up or "PRESTATION" in text_up:
        return "Freelance"
    return None


# ── Scraping pages détail ─────────────────────────────────────────────

async def _fetch_senjob_detail(url: str) -> dict:
    """Scrape la page détail d'une offre Senjob via httpx."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {}
            soup = BeautifulSoup(resp.text, "html.parser")

            meta = soup.find("meta", attrs={"name": "description"})
            if not meta:
                return {}
            content = meta.get("content", "")

            result = {}

            secteur_match = re.search(r"Secteur\s*:\s*([^\n\r<]+)", content)
            if secteur_match:
                result["secteur"] = secteur_match.group(1).strip()

            loc_match = re.search(r"Localisation\s*:\s*([^\n\r<]+)", content)
            if loc_match:
                result["localisation"] = loc_match.group(1).strip()

            desc = re.sub(r"(Secteur|Localisation)\s*:[^\n]*\n?", "", content)
            result["description"] = desc.strip()[:500]

            h1 = soup.find("h1")
            if h1:
                result["titre"] = h1.get_text(strip=True)[:100]

            og_title = soup.find("meta", property="og:title")
            if og_title:
                og = og_title.get("content", "")
                parts = og.split(" - ")
                if len(parts) >= 2:
                    result["entreprise"] = parts[1].strip()

            # Extraction structurée complète via LLM (titre, entreprise, tâches, conditions...)
            if result.get("description"):
                details = await _extract_job_details_llm(result["description"], url)
                if details.get("titre"):
                    result["titre"] = details["titre"][:100]
                if details.get("entreprise") and details["entreprise"] not in ("Non précisé", "", None):
                    result["entreprise"] = details["entreprise"][:100]
                for f in ("localisation", "type_contrat", "secteur", "email_candidature"):
                    if details.get(f) and not result.get(f):
                        result[f] = details[f]
                result["taches"] = details.get("taches") or []
                result["conditions_requises"] = details.get("conditions_requises") or []
                result["avantages"] = details.get("avantages") or []
                result["experience_min_annees"] = details.get("experience_min_annees")
                result["description_complete"] = result["description"]

            return result
    except Exception as e:
        print(f"  ⚠️ Senjob detail error {url}: {e}")
        return {}


async def _fetch_emploidakar_detail(url: str, zyte_api_key: str) -> dict:
    """Scrape la page détail d'une offre EmploiDakar via Zyte."""
    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.post(
                "https://api.zyte.com/v1/extract",
                auth=(zyte_api_key, ""),
                json={"url": url, "browserHtml": True},
            )
            data = resp.json()
            html = data.get("browserHtml", "")
            if not html:
                return {}

            soup = BeautifulSoup(html, "html.parser")
            result = {}

            h1 = soup.find("h1")
            if h1:
                result["titre"] = h1.get_text(strip=True)[:100]

            for tag in soup.find_all(["span", "div", "p", "li"]):
                text = tag.get_text(strip=True)
                if not text or len(text) > 150:
                    continue
                tl = text.lower()
                if any(v in tl for v in ("dakar", "thiès", "thies", "saint-louis", "sénégal", "senegal")):
                    if not result.get("localisation") and len(text) < 50:
                        result["localisation"] = text
                if text in ("CDI", "CDD", "Stage", "Freelance", "Prestation de Services"):
                    result["type_contrat"] = "Freelance" if text == "Prestation de Services" else text

            meta = soup.find("meta", attrs={"name": "description"})
            if meta:
                result["description"] = meta.get("content", "")[:500]

            # Extraction structurée complète via LLM (h1 emploidakar toujours pollué)
            if result.get("description"):
                _h1_brut = result.get("titre", "")
                details = await _extract_job_details_llm(result["description"], url)
                if details.get("titre"):
                    result["titre"] = details["titre"][:100]  # LLM remplace toujours
                elif not result.get("titre"):
                    result["titre"] = _h1_brut
                if details.get("entreprise") and details["entreprise"] not in ("Non précisé", "", None):
                    result["entreprise"] = details["entreprise"][:100]
                for f in ("localisation", "type_contrat", "secteur", "email_candidature"):
                    if details.get(f) and not result.get(f):
                        result[f] = details[f]
                result["taches"] = details.get("taches") or []
                result["conditions_requises"] = details.get("conditions_requises") or []
                result["avantages"] = details.get("avantages") or []
                result["experience_min_annees"] = details.get("experience_min_annees")
                result["description_complete"] = result["description"]

            if result.get("description") and not result.get("secteur"):
                secteur = await _detect_secteur_llm(result["description"])
                if secteur:
                    result["secteur"] = secteur

            return result
    except Exception as e:
        print(f"  ⚠️ EmploiDakar detail error {url}: {e}")
        return {}


async def _extract_titre_entreprise_llm(description: str, url: str) -> dict:
    """Extrait titre du poste et entreprise depuis le texte via Mistral."""
    if not settings.mistral_api_key or not description:
        return {}
    import json
    import re as _re
    try:
        async with httpx.AsyncClient(timeout=10.0) as _c:
            _r = await _c.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": "mistral-small-latest",
                    "messages": [{"role": "user", "content":
                        f"Extrait le titre du poste et l'entreprise depuis ce texte.\n"
                        f"Texte: {description[:300]}\n"
                        f"URL: {url}\n"
                        f'Réponds UNIQUEMENT: {{"titre": "...", "entreprise": "..."}}'
                    }],
                    "temperature": 0.1,
                    "max_tokens": 60,
                },
            )
            _d = _r.json()
            if "choices" in _d:
                _txt = _re.sub(r"```json|```", "", _d["choices"][0]["message"]["content"]).strip()
                _parsed = json.loads(_txt)
                out = {}
                if _parsed.get("titre"):
                    out["titre"] = _parsed["titre"][:100]
                if _parsed.get("entreprise") and _parsed["entreprise"] not in ("Non précisé", ""):
                    out["entreprise"] = _parsed["entreprise"][:100]
                return out
    except Exception:
        pass
    return {}


async def _detect_secteur_llm(description: str) -> str | None:
    """Détecte le secteur d'activité via Mistral (appel léger)."""
    if not settings.mistral_api_key:
        return None
    secteurs = [
        "Informatique/Tech", "Finance/Comptabilité", "Marketing/Communication",
        "Santé", "Éducation", "BTP/Ingénierie", "Commerce/Vente",
        "RH/Recrutement", "Juridique", "Agriculture", "Transport/Logistique",
        "Humanitaire/ONG", "Hôtellerie/Restauration", "Autre",
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": "mistral-small-latest",
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Quel est le secteur d'activité de cette offre d'emploi ?\n"
                            f"Description: {description[:200]}\n\n"
                            f"Choisis UNIQUEMENT parmi: {', '.join(secteurs)}\n"
                            f"Réponds avec juste le nom du secteur, rien d'autre."
                        ),
                    }],
                    "temperature": 0.1,
                    "max_tokens": 20,
                },
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
    except Exception:
        pass
    return None


async def _extract_job_details_llm(description_text: str, url: str) -> dict:
    """Extrait tâches, conditions, avantages depuis la description complète."""
    if not settings.mistral_api_key or not description_text:
        return {}
    import json as _json
    import re as _re
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": "mistral-small-latest",
                    "messages": [{"role": "user", "content":
                        f"Extrais les informations structurées de cette offre d'emploi sénégalaise.\n"
                        f"Description : {description_text[:2000]}\n\n"
                        f'Retourne UNIQUEMENT ce JSON :\n'
                        f'{{"titre": "...", "entreprise": "...", "localisation": "...", '
                        f'"type_contrat": "CDI|CDD|Stage|Freelance|null", '
                        f'"secteur": "...", '
                        f'"taches": ["mission 1", "mission 2"], '
                        f'"conditions_requises": ["Bac+3 requis", "2 ans expérience"], '
                        f'"avantages": ["salaire compétitif"], '
                        f'"experience_min_annees": 0, '
                        f'"email_candidature": "email@entreprise.com ou null"}}'
                    }],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
            )
            data = resp.json()
            if "choices" in data:
                txt = _re.sub(r"```json|```", "", data["choices"][0]["message"]["content"]).strip()
                return _json.loads(txt)
    except Exception as e:
        print(f"  ⚠️ job details LLM error: {e}")
    return {}


# ── Scraper EmploiDakar (via Zyte API) ────────────────────────────────

async def _fetch_emploidakar_jobs() -> list[dict]:
    """Scrape emploidakar.com via Zyte API (browserHtml)."""
    if not settings.zyte_api_key:
        print("  ⚠️ ZYTE_API_KEY manquant — emploidakar.com ignoré")
        return []

    # Pages de navigation / catégories à exclure (ce ne sont pas des offres)
    _NAV_EXCLUDE = (
        "/offres-demploi", "/publier", "/gerer", "/deposer", "/pourquoi",
        "/liste-des", "/les-entreprises", "/recrutement-centre", "/emploidakar/",
        "/alertes", "/nous-contacter", "/exemple-de-cv", "/modeles-lettres",
        "/candidature-spontanee", "/service-de-redaction", "/acceder-a-la-cvtheque",
        "/mon-espace", "/formation-en-ligne", "/actualites", "/boutique", "/panier",
        "/a-propos", "/conditions-generales", "/politique-de", "/fiches-metier",
        "/sourcing-premium", "/stages-bourses", "/top-10", "/cgu", "wp-", "xmlrpc",
        "#", "/categorie", "/category", "/tag/", "/auth", "/connexion", "/inscription",
    )

    jobs = []
    seen = set()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.zyte.com/v1/extract",
                auth=(settings.zyte_api_key, ""),
                json={
                    "url": "https://www.emploidakar.com/offres-demploi-au-senegal/",
                    "browserHtml": True,
                },
            )
            if resp.status_code != 200:
                print(f"  ⚠️ EmploiDakar: Zyte HTTP {resp.status_code} — {resp.text[:200]}")
                return []
            data = resp.json()
            html = data.get("browserHtml", "")
            if not html:
                print(f"  ⚠️ EmploiDakar: HTML vide depuis Zyte — réponse: {str(data)[:200]}")
                return []

            soup = BeautifulSoup(html, "html.parser")
            all_links = soup.find_all("a", href=True)

            matched_strict = 0
            for link in all_links:
                href = link.get("href", "")
                text = link.get_text(strip=True)

                # URL absolue
                if href.startswith("/"):
                    href = f"https://www.emploidakar.com{href}"
                if "emploidakar.com" not in href:
                    continue

                # Motif strict (WP Job Manager : offre individuelle)
                is_strict = "/offre-demploi/" in href
                if is_strict:
                    matched_strict += 1

                # Heuristique de repli : lien interne à 1 segment, hors navigation,
                # avec un texte d'intitulé suffisamment long → probablement une offre
                path = href.split("emploidakar.com", 1)[-1]
                is_nav = any(n in path for n in _NAV_EXCLUDE)
                seg_count = len([p for p in path.split("/") if p])
                is_heuristic = (
                    not is_nav and seg_count == 1 and text and len(text) >= 12
                )

                if not (is_strict or is_heuristic):
                    continue
                if href in seen:
                    continue
                seen.add(href)

                jobs.append({
                    "titre": text[:100].strip(),
                    "entreprise": "",
                    "localisation": "Dakar, Sénégal",
                    "type_contrat": _detect_type_contrat(text),
                    "source_url": href,
                    "source": "scraping",
                    "annee_publication": _extract_annee(text),
                    "email_candidature": None,
                    "description": None,
                    "secteur": None,
                })

            print(
                f"  → EmploiDakar: {len(jobs)} offres "
                f"(liens totaux={len(all_links)}, motif strict /offre-demploi/={matched_strict})"
            )
            if len(jobs) == 0 and all_links:
                # Diagnostic : échantillon de hrefs pour identifier le vrai motif
                sample = [l.get("href", "") for l in all_links[:40] if l.get("href")]
                print(f"  🔎 EmploiDakar échantillon hrefs: {sample}")
    except Exception as e:
        print(f"  ⚠️ EmploiDakar scraping error: {e}")

    return jobs


# ── Scraper Senjob (httpx direct) ─────────────────────────────────────

async def _fetch_senjob_jobs() -> list[dict]:
    """Scrape senjob.com via httpx direct."""
    jobs = []
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            resp = await client.get("https://www.senjob.com/offres-d-emploi.php")
            if resp.status_code != 200:
                print(f"  ⚠️ Senjob: HTTP {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                text = link.get_text(strip=True)

                if "/jobseekers/" not in href or "_e_" not in href:
                    continue
                if not text or len(text) < 5:
                    continue

                if not href.startswith("http"):
                    href = f"https://www.senjob.com{href}"

                type_contrat = _detect_type_contrat(text)
                annee = datetime.now().year

                jobs.append({
                    "titre": text[:100].strip(),
                    "entreprise": "",
                    "localisation": "Sénégal",
                    "type_contrat": type_contrat,
                    "source_url": href,
                    "source": "scraping",
                    "annee_publication": annee,
                    "email_candidature": None,
                    "description": None,
                    "secteur": None,
                })

        print(f"  → Senjob: {len(jobs)} offres scrapées")
    except Exception as e:
        print(f"  ⚠️ Senjob scraping error: {e}")

    return jobs


# ── Service principal ──────────────────────────────────────────────────

class ScrapingService:

    async def scrape_all(self, db: AsyncSession) -> dict:
        """
        Lance le scraping de toutes les sources et sauvegarde en DB.
        Déduplication à 2 niveaux : URL exacte → hash contenu+année.
        Expire les offres non revues depuis 3 jours.
        """
        now = datetime.now(timezone.utc)
        new_count = 0
        duplicate_count = 0
        expired_count = 0

        # Charge les offres scraping actives existantes
        result = await db.execute(
            select(JobOpportunity).where(
                JobOpportunity.source == "scraping",
                JobOpportunity.statut == "active",
            )
        )
        existing_jobs = result.scalars().all()
        existing_urls = {j.source_url for j in existing_jobs if j.source_url}
        existing_hashes = {j.content_hash: j for j in existing_jobs if j.content_hash}

        # Nettoyage en DB des offres existantes aux titres/desc mal parsés
        cleaned_existing = 0
        for j in existing_jobs:
            new_titre = clean_text(j.titre)
            new_entr = clean_text(j.entreprise)
            new_desc = clean_text(j.description)
            if new_titre != j.titre or new_entr != j.entreprise or new_desc != j.description:
                j.titre = new_titre
                if new_entr is not None:
                    j.entreprise = new_entr
                j.description = new_desc
                cleaned_existing += 1
        if cleaned_existing:
            await db.flush()
            print(f"  → {cleaned_existing} offres existantes nettoyées en DB")

        # Scrape toutes les sources (séparément pour suivre leur santé)
        # EmploiDakar désactivé du scraping auto (JS dynamique, Cloudflare) :
        # ajout manuel via le dashboard (URL / manuel / PDF).
        ed_jobs = []
        sj_jobs = await _fetch_senjob_jobs()
        all_scraped: list[dict] = list(ed_jobs) + list(sj_jobs)

        # Domaines ayant renvoyé des résultats ce run → seuls leurs offres
        # absentes peuvent être expirées. Si une source échoue (Zyte KO),
        # on NE touche PAS à ses offres existantes.
        healthy_domains = set()
        if ed_jobs:
            healthy_domains.add("emploidakar.com")
        if sj_jobs:
            healthy_domains.add("senjob.com")

        # Enrichit les offres via scraping des pages détail
        if all_scraped:
            print(f"  → Scraping détail de {len(all_scraped)} offres...")
            import asyncio as _asyncio

            async def _enrich_with_detail(job: dict) -> None:
                """Enrichit une offre avec les données de sa page détail."""
                url = job.get("source_url", "")
                if not url:
                    return
                detail = {}
                if "senjob.com" in url:
                    detail = await _fetch_senjob_detail(url)
                elif "emploidakar.com" in url:
                    detail = await _fetch_emploidakar_detail(url, settings.zyte_api_key)
                # Enrichit seulement les champs vides
                for field in ["titre", "entreprise", "localisation", "type_contrat", "secteur", "description", "email_candidature"]:
                    if detail.get(field) and not job.get(field):
                        job[field] = detail[field]
                # Champs détail toujours copiés (n'existent pas dans la liste de base)
                for field in ["taches", "conditions_requises", "avantages",
                              "experience_min_annees", "description_complete"]:
                    if detail.get(field) is not None:
                        job[field] = detail[field]

            # Batch de 5 (Zyte est plus lent que httpx direct)
            batch_size_detail = 5
            for i in range(0, len(all_scraped), batch_size_detail):
                batch = all_scraped[i:i + batch_size_detail]
                await _asyncio.gather(*[_enrich_with_detail(j) for j in batch])
                if i + batch_size_detail < len(all_scraped):
                    await _asyncio.sleep(2.0)

            print("  → Enrichissement détail terminé")

        scraped_urls: set[str] = set()
        batch_urls: set[str] = set()  # URLs déjà traitées dans ce batch

        for job_data in all_scraped:
            source_url = job_data.get("source_url", "")
            # Nettoyage des champs texte (caractères de contrôle, guillemets…)
            titre = clean_text(job_data.get("titre", ""))
            entreprise = clean_text(job_data.get("entreprise", "")) or ""
            localisation = clean_text(job_data.get("localisation", "")) or ""
            annee = job_data.get("annee_publication", datetime.now().year)

            if not titre or not source_url:
                continue

            # Réinjecte les valeurs nettoyées pour l'insert
            job_data["titre"] = titre
            job_data["entreprise"] = entreprise
            job_data["localisation"] = localisation
            job_data["secteur"] = clean_text(job_data.get("secteur"))
            job_data["description"] = clean_text(job_data.get("description"))
            job_data["description_complete"] = clean_text(job_data.get("description_complete"))

            scraped_urls.add(source_url)

            # Niveau 0 — Doublon dans ce même batch (même URL vue deux fois)
            if source_url in batch_urls:
                duplicate_count += 1
                continue
            batch_urls.add(source_url)

            # Niveau 1 — URL déjà en DB
            if source_url in existing_urls:
                for j in existing_jobs:
                    if j.source_url == source_url:
                        j.last_seen_at = now
                        break
                duplicate_count += 1
                continue

            # Niveau 2 — Hash contenu + année déjà en DB
            c_hash = _content_hash(titre, entreprise, localisation, annee)
            if c_hash in existing_hashes:
                existing_hashes[c_hash].last_seen_at = now
                duplicate_count += 1
                continue

            # Nouvelle offre — INSERT ON CONFLICT DO NOTHING sur source_url
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(JobOpportunity).values(
                id=__import__('uuid').uuid4(),
                titre=titre,
                entreprise=entreprise or "Non précisé",
                secteur=job_data.get("secteur"),
                localisation=localisation,
                description=job_data.get("description"),
                description_complete=job_data.get("description_complete"),
                taches=job_data.get("taches"),
                conditions_requises=job_data.get("conditions_requises"),
                avantages=job_data.get("avantages"),
                experience_min_annees=job_data.get("experience_min_annees"),
                type_contrat=job_data.get("type_contrat"),
                email_candidature=job_data.get("email_candidature"),
                source="scraping",
                source_url=source_url,
                statut="active",
                content_hash=c_hash,
                annee_publication=annee,
                last_seen_at=now,
                expires_at=now + timedelta(days=30),
            ).on_conflict_do_nothing(index_elements=["source_url"])
            await db.execute(stmt)
            new_count += 1

        # Expire les offres non revues depuis 3 jours — UNIQUEMENT pour les
        # sources qui ont répondu ce run (évite d'expirer EmploiDakar si Zyte a échoué)
        cutoff = now - timedelta(days=3)
        for job in existing_jobs:
            if job.source_url in scraped_urls:
                continue
            # Le domaine de cette offre a-t-il répondu ce run ?
            job_domain = next((d for d in ("emploidakar.com", "senjob.com")
                               if job.source_url and d in job.source_url), None)
            if job_domain and job_domain not in healthy_domains:
                continue  # source en échec → on préserve ses offres
            if job.last_seen_at and job.last_seen_at < cutoff:
                job.statut = "expired"
                expired_count += 1

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"  ⚠️ scrape_all commit error: {e}")
            raise

        # Génère/rattrape les embeddings de TOUTE offre active sans embedding
        # (couvre aussi les offres dont l'embedding avait échoué un run précédent)
        try:
            from app.services.rag.embedding_service import embedding_service
            result2 = await db.execute(
                select(JobOpportunity).where(
                    JobOpportunity.source == "scraping",
                    JobOpportunity.statut == "active",
                    JobOpportunity.embedding.is_(None),
                )
            )
            jobs_sans_embedding = result2.scalars().all()
            if jobs_sans_embedding:
                for job in jobs_sans_embedding:
                    taches_str = " ".join(job.taches or [])
                    embed_text = (
                        f"{job.titre} {job.entreprise} "
                        f"{job.secteur or ''} {job.localisation} "
                        f"{job.type_contrat or ''} {taches_str} "
                        f"{(job.description_complete or job.description or '')[:300]}"
                    )
                    embedding = await embedding_service.embed_text(embed_text)
                    if embedding:
                        job.embedding = embedding
                await db.commit()
                print(f"  → {len(jobs_sans_embedding)} embeddings générés")
        except Exception as e:
            print(f"  ⚠️ Embedding error: {e}")

        print(f"Scraping terminé: {new_count} nouvelles, {duplicate_count} doublons, {expired_count} expirées")
        return {
            "new": new_count,
            "duplicates": duplicate_count,
            "expired": expired_count,
            "total_scraped": len(all_scraped),
        }

    async def check_duplicate_manual(
        self,
        db: AsyncSession,
        titre: str,
        entreprise: str,
        localisation: str,
        annee: int,
        source_url: str = None,
    ) -> dict | None:
        """
        Vérifie si une offre ajoutée manuellement existe déjà.
        Niveau 1 : URL exacte. Niveau 2 : hash contenu + année.
        Retourne les infos du doublon ou None.
        """
        # Niveau 1 — URL
        if source_url:
            result = await db.execute(
                select(JobOpportunity).where(JobOpportunity.source_url == source_url)
            )
            existing = result.scalar_one_or_none()
            if existing:
                return {
                    "type": "url",
                    "message": f"URL déjà indexée ({existing.source})",
                    "existing": {
                        "id": str(existing.id),
                        "titre": existing.titre,
                        "source": existing.source,
                        "statut": existing.statut,
                        "created_at": existing.created_at.isoformat() if existing.created_at else None,
                    },
                }

        # Niveau 2 — Hash contenu + année
        c_hash = _content_hash(titre, entreprise, localisation, annee)
        result = await db.execute(
            select(JobOpportunity).where(JobOpportunity.content_hash == c_hash)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return {
                "type": "content",
                "message": f"Offre similaire déjà publiée en {existing.annee_publication} ({existing.source})",
                "existing": {
                    "id": str(existing.id),
                    "titre": existing.titre,
                    "source": existing.source,
                    "statut": existing.statut,
                    "annee_publication": existing.annee_publication,
                    "created_at": existing.created_at.isoformat() if existing.created_at else None,
                },
            }

        return None


scraping_service = ScrapingService()
