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


# ── Enrichissement LLM ────────────────────────────────────────────────

async def _parse_job_with_llm(raw_text: str, source_url: str) -> dict:
    """
    Utilise Mistral pour extraire proprement les champs d'une offre
    depuis le texte brut scrappé.
    """
    import json
    import re as _re
    if not settings.mistral_api_key:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                json={
                    "model": "mistral-small-latest",
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Extrais les informations de cette offre d'emploi sénégalaise.\n"
                            f"Texte brut : {raw_text[:300]}\n"
                            f"URL : {source_url}\n\n"
                            f"Retourne UNIQUEMENT ce JSON valide :\n"
                            f'{{"titre": "...", "entreprise": "...", '
                            f'"localisation": "...", "type_contrat": "CDI|CDD|Stage|Freelance|null", '
                            f'"secteur": "...", "description_courte": "..."}}'
                        ),
                    }],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
            )
            data = resp.json()
            if "choices" in data:
                text = data["choices"][0]["message"]["content"].strip()
                text = _re.sub(r"```json|```", "", text).strip()
                return json.loads(text)
    except Exception as e:
        print(f"  ⚠️ LLM parse error: {e}")
    return {}


# ── Scraper EmploiDakar (via Zyte API) ────────────────────────────────

async def _fetch_emploidakar_jobs() -> list[dict]:
    """Scrape emploidakar.com via Zyte API (browserHtml)."""
    if not settings.zyte_api_key:
        print("  ⚠️ ZYTE_API_KEY manquant — emploidakar.com ignoré")
        return []

    jobs = []
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
            data = resp.json()
            html = data.get("browserHtml", "")
            if not html:
                print("  ⚠️ EmploiDakar: HTML vide depuis Zyte")
                return []

            soup = BeautifulSoup(html, "html.parser")

            for link in soup.find_all("a", href=True):
                href = link.get("href", "")
                text = link.get_text(strip=True)

                if "/offre-demploi/" not in href:
                    continue
                if not text or len(text) < 5:
                    continue

                # URL absolue
                if not href.startswith("http"):
                    href = f"https://www.emploidakar.com{href}"

                type_contrat = _detect_type_contrat(text)
                annee = _extract_annee(text)

                jobs.append({
                    "titre": text[:100].strip(),
                    "entreprise": "",
                    "localisation": "Dakar, Sénégal",
                    "type_contrat": type_contrat,
                    "source_url": href,
                    "source": "scraping",
                    "annee_publication": annee,
                    "email_candidature": None,
                    "description": None,
                    "secteur": None,
                })

        print(f"  → EmploiDakar: {len(jobs)} offres scrapées")
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

        # Scrape toutes les sources
        all_scraped: list[dict] = []
        all_scraped += await _fetch_emploidakar_jobs()
        all_scraped += await _fetch_senjob_jobs()

        # Enrichit les offres via LLM (batch de 10, anti rate-limit)
        if all_scraped:
            print(f"  → Enrichissement LLM de {len(all_scraped)} offres...")
            import asyncio as _asyncio

            async def _enrich_batch(jobs_batch: list[dict]) -> None:
                tasks = [
                    _parse_job_with_llm(j["titre"], j["source_url"])
                    for j in jobs_batch
                ]
                results = await _asyncio.gather(*tasks, return_exceptions=True)
                for job, result in zip(jobs_batch, results):
                    if isinstance(result, dict) and result:
                        job["titre"]       = result.get("titre")       or job["titre"]
                        job["entreprise"]  = result.get("entreprise")  or job["entreprise"]
                        job["localisation"]= result.get("localisation")or job["localisation"]
                        job["type_contrat"]= result.get("type_contrat")or job["type_contrat"]
                        job["secteur"]     = result.get("secteur")     or job["secteur"]
                        job["description"] = result.get("description_courte") or job["description"]

            batch_size_llm = 10
            for i in range(0, len(all_scraped), batch_size_llm):
                await _enrich_batch(all_scraped[i:i + batch_size_llm])
                if i + batch_size_llm < len(all_scraped):
                    await _asyncio.sleep(1.0)  # anti rate-limit Mistral

            print("  → Enrichissement LLM terminé")

        scraped_urls: set[str] = set()
        batch_urls: set[str] = set()  # URLs déjà traitées dans ce batch

        for job_data in all_scraped:
            source_url = job_data.get("source_url", "")
            titre = job_data.get("titre", "")
            entreprise = job_data.get("entreprise", "")
            localisation = job_data.get("localisation", "")
            annee = job_data.get("annee_publication", datetime.now().year)

            if not titre or not source_url:
                continue

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

            # Nouvelle offre
            new_job = JobOpportunity(
                titre=titre,
                entreprise=entreprise or "Non précisé",
                secteur=job_data.get("secteur"),
                localisation=localisation,
                description=job_data.get("description"),
                type_contrat=job_data.get("type_contrat"),
                email_candidature=job_data.get("email_candidature"),
                source="scraping",
                source_url=source_url,
                statut="active",
                content_hash=c_hash,
                annee_publication=annee,
                last_seen_at=now,
                expires_at=now + timedelta(days=30),
            )
            db.add(new_job)
            new_count += 1

        # Expire les offres non revues depuis 3 jours
        cutoff = now - timedelta(days=3)
        for job in existing_jobs:
            if job.source_url not in scraped_urls:
                if job.last_seen_at and job.last_seen_at < cutoff:
                    job.statut = "expired"
                    expired_count += 1

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"  ⚠️ scrape_all commit error: {e}")
            raise

        # Génère les embeddings pour les nouvelles offres sans embedding
        if new_count > 0:
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
                for job in jobs_sans_embedding:
                    embed_text = (
                        f"{job.titre} {job.entreprise} "
                        f"{job.secteur or ''} {job.localisation} "
                        f"{job.type_contrat or ''}"
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
