"""
Cerveau évolutif de Prepa.
Analyse automatiquement le corpus indexé pour extraire :
- Les types d'exercices récurrents
- Les méthodologies officielles
- Les pièges fréquents
- La fréquence réelle de chaque type au BAC
"""
import json
import re
import httpx
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.document import Document, DocumentChunk
from app.models.exercise import ExerciseType, KnowledgeBase
from app.core.settings import get_settings

settings = get_settings()


class BrainService:

    async def analyze_corpus(
        self,
        db: AsyncSession,
        exam_type: str,
        serie: str,
        matiere: str,
        force: bool = False,
    ) -> dict:
        """
        Analyse le corpus indexé pour une matière donnée.
        Détecte automatiquement les types d'exercices et méthodes.
        """
        print(f"\n🧠 Analyse corpus : {exam_type}/{serie}/{matiere}")

        # 1. Récupère tous les chunks de cette matière
        chunks = await self._get_chunks(db, exam_type, serie, matiere)
        if not chunks:
            return {"status": "no_chunks", "matiere": matiere}

        print(f"  → {len(chunks)} chunks trouvés")

        # 2. Sépare par type de document
        cours_chunks = [c for c in chunks if c.doc_type == "cours"]
        annale_chunks = [c for c in chunks if c.doc_type == "annale"]
        serie_chunks = [c for c in chunks if c.doc_type in ("serie", "exercice")]
        corrige_chunks = [c for c in chunks if c.doc_type == "correction"]
        devoir_chunks = [c for c in chunks if c.doc_type == "devoir"]
        fiche_chunks = [c for c in chunks if c.doc_type == "fiche"]

        print(f"  → Cours: {len(cours_chunks)} | Annales: {len(annale_chunks)} | Séries: {len(serie_chunks)} | Corrigés: {len(corrige_chunks)}")

        # 3. Extrait les types d'exercices depuis les annales + séries
        exercise_sources = annale_chunks + serie_chunks + devoir_chunks
        if exercise_sources:
            exercise_types = await self._extract_exercise_types(
                exercise_sources, exam_type, serie, matiere
            )
            # Sauvegarde en base
            saved = await self._save_exercise_types(db, exercise_types, exam_type, serie, matiere)
            print(f"  → {saved} types d'exercices extraits")

        # 4. Extrait les méthodes depuis les corrigés
        if corrige_chunks:
            methods = await self._extract_methods(corrige_chunks, matiere)
            await self._enrich_exercise_types(db, methods, matiere)
            print(f"  → {len(methods)} méthodes extraites des corrigés")

        # 5. Extrait les formules clés depuis les cours et fiches
        formula_sources = cours_chunks + fiche_chunks
        if formula_sources:
            formulas = await self._extract_formulas(formula_sources, matiere)
            await self._save_knowledge_items(db, formulas, matiere)
            print(f"  → {len(formulas)} formules/définitions extraites")

        # 6. Calcule les fréquences depuis les annales
        if annale_chunks:
            await self._compute_frequencies(db, annale_chunks, matiere)
            print(f"  → Fréquences calculées")

        await db.commit()
        print(f"✅ Analyse terminée pour {matiere}")

        return {
            "status": "ok",
            "matiere": matiere,
            "chunks_analyzed": len(chunks),
        }

    async def analyze_document(
        self,
        db: AsyncSession,
        document_id: str,
    ) -> dict:
        """
        Analyse un document spécifique après indexation.
        Appelé automatiquement après chaque upload.
        """
        from sqlalchemy import text
        import uuid

        result = await db.execute(
            select(Document).where(Document.id == uuid.UUID(document_id))
        )
        doc = result.scalar_one_or_none()
        if not doc:
            return {"status": "not_found"}

        if not doc.matiere or not doc.exam_type:
            return {"status": "no_metadata"}

        return await self.analyze_corpus(
            db=db,
            exam_type=doc.exam_type,
            serie=doc.serie or "",
            matiere=doc.matiere,
        )

    async def _get_chunks(
        self,
        db: AsyncSession,
        exam_type: str,
        serie: str,
        matiere: str,
    ) -> list:
        """Récupère tous les chunks d'une matière."""
        filters = [
            DocumentChunk.embedding.isnot(None),
            DocumentChunk.matiere == matiere,
        ]
        if exam_type:
            filters.append(DocumentChunk.exam_type == exam_type)
        if serie:
            filters.append(DocumentChunk.serie == serie)

        result = await db.execute(
            select(DocumentChunk).where(and_(*filters)).limit(500)
        )
        return result.scalars().all()

    async def _extract_exercise_types(
        self,
        chunks: list,
        exam_type: str,
        serie: str,
        matiere: str,
    ) -> list:
        """
        Utilise le LLM pour détecter les types d'exercices
        dans un ensemble de chunks.
        """
        # Prend un échantillon représentatif
        sample_size = min(20, len(chunks))
        sample = chunks[:sample_size]
        corpus = "\n\n---\n\n".join([c.content[:300] for c in sample])

        prompt = f"""Tu analyses des exercices de {matiere} pour le {exam_type} série {serie} au Sénégal.

Voici des extraits de {len(sample)} exercices :

{corpus}

Identifie les TYPES D'EXERCICES distincts présents. Pour chaque type, fournis un JSON.

Réponds UNIQUEMENT avec ce JSON valide (tableau) :
[
  {{
    "slug": "identifiant_unique_snake_case",
    "nom": "Nom du type d'exercice",
    "description": "Description courte",
    "methode_steps": [
      {{"step": 1, "action": "Première étape", "conseil": "Conseil pratique"}},
      {{"step": 2, "action": "Deuxième étape", "conseil": "Conseil pratique"}}
    ],
    "formules_cles": ["formule1", "formule2"],
    "pieges": ["piège classique 1", "piège classique 2"],
    "difficulte": 2,
    "frequence_exam": "fréquent"
  }}
]

frequence_exam doit être : rare | fréquent | très_fréquent | quasi_certain
difficulte : 1 (facile), 2 (moyen), 3 (difficile)
Maximum 5 types. Sois précis et concis."""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 2000,
                        "temperature": 0.2,
                    },
                )
                data = response.json()
                raw = data["choices"][0]["message"]["content"].strip()

                # Extrait le JSON
                json_match = re.search(r'\[.*\]', raw, re.DOTALL)
                if json_match:
                    types = json.loads(json_match.group())
                    # Ajoute le préfixe de namespace au slug
                    prefix = f"{exam_type}_{serie}_{matiere}_"
                    for t in types:
                        if not t["slug"].startswith(prefix):
                            t["slug"] = prefix + t["slug"]
                        t["pays"] = "senegal"
                        t["exam_type"] = exam_type
                        t["serie"] = serie
                        t["matiere"] = matiere
                        t["chapitre"] = t.get("chapitre", "general")
                    return types

        except Exception as e:
            print(f"Erreur extraction types: {e}")

        return []

    async def _extract_methods(
        self,
        corrige_chunks: list,
        matiere: str,
    ) -> list:
        """Extrait les méthodes officielles depuis les corrigés."""
        sample = corrige_chunks[:10]
        corpus = "\n\n---\n\n".join([c.content[:400] for c in sample])

        prompt = f"""Tu analyses des corrigés officiels de {matiere} au BAC sénégalais.

Extrais les méthodes de résolution utilisées. Pour chaque méthode :

Réponds UNIQUEMENT avec ce JSON :
[
  {{
    "type_exercice": "nom du type d'exercice concerné",
    "methode": "Description de la méthode étape par étape",
    "formules": ["formule utilisée 1", "formule utilisée 2"],
    "redaction_type": "Exemple de rédaction officielle attendue"
  }}
]

Corpus :
{corpus}"""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1500,
                        "temperature": 0.2,
                    },
                )
                data = response.json()
                raw = data["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\[.*\]', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        except Exception as e:
            print(f"Erreur extraction méthodes: {e}")

        return []

    async def _extract_formulas(
        self,
        chunks: list,
        matiere: str,
    ) -> list:
        """Extrait les formules et définitions clés."""
        sample = chunks[:15]
        corpus = "\n\n---\n\n".join([c.content[:300] for c in sample])

        prompt = f"""Tu analyses des cours de {matiere} au BAC sénégalais.

Extrais les formules et définitions importantes.

Réponds UNIQUEMENT avec ce JSON :
[
  {{
    "chapitre": "nom du chapitre",
    "type": "formule ou definition ou theoreme",
    "contenu": "La formule ou définition exacte",
    "explication": "Explication simple en français"
  }}
]

Corpus :
{corpus}

Maximum 10 éléments."""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.mistral.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                    json={
                        "model": "mistral-small-latest",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 1000,
                        "temperature": 0.1,
                    },
                )
                data = response.json()
                raw = data["choices"][0]["message"]["content"].strip()
                json_match = re.search(r'\[.*\]', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
        except Exception as e:
            print(f"Erreur extraction formules: {e}")

        return []

    async def _save_exercise_types(
        self,
        db: AsyncSession,
        types: list,
        exam_type: str,
        serie: str,
        matiere: str,
    ) -> int:
        """Sauvegarde les types d'exercices détectés."""
        saved = 0
        for t in types:
            # Vérifie si le slug existe déjà
            result = await db.execute(
                select(ExerciseType).where(ExerciseType.slug == t.get("slug", ""))
            )
            existing = result.scalar_one_or_none()

            if existing:
                # Met à jour si nouvelles infos
                if t.get("methode_steps") and not existing.methode_steps:
                    existing.methode_steps = t["methode_steps"]
                if t.get("formules_cles") and not existing.formules_cles:
                    existing.formules_cles = t["formules_cles"]
                if t.get("pieges") and not existing.pieges:
                    existing.pieges = t["pieges"]
            else:
                exercise = ExerciseType(
                    slug=t.get("slug", f"{matiere}_{saved}"),
                    nom=t.get("nom", "Type d'exercice"),
                    description=t.get("description", ""),
                    pays="senegal",
                    exam_type=exam_type,
                    serie=serie,
                    matiere=matiere,
                    chapitre=t.get("chapitre", "general"),
                    methode_steps=t.get("methode_steps", []),
                    formules_cles=t.get("formules_cles", []),
                    pieges=t.get("pieges", []),
                    prerequis=t.get("prerequis", []),
                    frequence_exam=t.get("frequence_exam", "fréquent"),
                    difficulte=t.get("difficulte", 2),
                )
                db.add(exercise)
                saved += 1

        await db.flush()
        return saved

    async def _enrich_exercise_types(
        self,
        db: AsyncSession,
        methods: list,
        matiere: str,
    ) -> None:
        """Enrichit les types d'exercices avec les méthodes extraites."""
        for method in methods:
            type_nom = method.get("type_exercice", "")
            if not type_nom:
                continue

            # Cherche le type d'exercice correspondant
            result = await db.execute(
                select(ExerciseType).where(
                    and_(
                        ExerciseType.matiere == matiere,
                        ExerciseType.nom.ilike(f"%{type_nom[:20]}%"),
                    )
                )
            )
            exercise_type = result.scalar_one_or_none()

            if exercise_type and method.get("redaction_type"):
                exercise_type.exemple_resolu = method["redaction_type"]

        await db.flush()

    async def _save_knowledge_items(
        self,
        db: AsyncSession,
        formulas: list,
        matiere: str,
    ) -> None:
        """Sauvegarde les formules et définitions dans la knowledge base."""
        for f in formulas:
            contenu = f.get("contenu", "")
            if not contenu or len(contenu) < 5:
                continue

            # Vérifie si existe déjà
            result = await db.execute(
                select(KnowledgeBase).where(
                    and_(
                        KnowledgeBase.matiere == matiere,
                        KnowledgeBase.question == contenu[:200],
                    )
                )
            )
            existing = result.scalar_one_or_none()

            if not existing:
                item = KnowledgeBase(
                    matiere=matiere,
                    chapitre=f.get("chapitre"),
                    question=contenu[:200],
                    reponse=f.get("explication", contenu),
                    methode_utilisee=f.get("type", "formule"),
                    is_validated=False,
                )
                db.add(item)

        await db.flush()

    async def _compute_frequencies(
        self,
        db: AsyncSession,
        annale_chunks: list,
        matiere: str,
    ) -> None:
        """Calcule la fréquence réelle des types d'exercices dans les annales."""
        # Récupère tous les types d'exercices pour cette matière
        result = await db.execute(
            select(ExerciseType).where(ExerciseType.matiere == matiere)
        )
        exercise_types = result.scalars().all()

        if not exercise_types:
            return

        # Pour chaque type, compte les annales qui le mentionnent
        total_annales = len(set(c.document_id for c in annale_chunks))
        if total_annales == 0:
            return

        for ex_type in exercise_types:
            count = 0
            keywords = [ex_type.nom.lower()] + (ex_type.formules_cles or [])[:3]

            for chunk in annale_chunks:
                content_lower = chunk.content.lower()
                if any(kw.lower() in content_lower for kw in keywords if len(kw) > 3):
                    count += 1

            frequency = count / total_annales
            if frequency >= 0.8:
                ex_type.frequence_exam = "quasi_certain"
            elif frequency >= 0.5:
                ex_type.frequence_exam = "très_fréquent"
            elif frequency >= 0.2:
                ex_type.frequence_exam = "fréquent"
            else:
                ex_type.frequence_exam = "rare"

        await db.flush()

    async def get_exercise_context(
        self,
        db: AsyncSession,
        matiere: str,
        chapitre: str,
        query: str,
    ) -> str:
        """
        Récupère le contexte des types d'exercices pour enrichir le prompt.
        Injecté dans call_llm pour des réponses ultra-précises.
        """
        result = await db.execute(
            select(ExerciseType).where(
                and_(
                    ExerciseType.matiere == matiere,
                    ExerciseType.chapitre == chapitre,
                    ExerciseType.is_active == True,
                )
            ).order_by(ExerciseType.difficulte)
        )
        types = result.scalars().all()

        if not types:
            return ""

        context_parts = []
        for t in types[:3]:
            part = f"*Type : {t.nom}* ({t.frequence_exam})\n"

            if t.methode_steps:
                part += "Méthode :\n"
                for step in t.methode_steps[:4]:
                    part += f"  {step['step']}. {step['action']}\n"

            if t.formules_cles:
                part += f"Formules : {' | '.join(t.formules_cles[:3])}\n"

            if t.pieges:
                part += f"Pièges : {' | '.join(t.pieges[:2])}\n"

            context_parts.append(part)

        return "\n\n".join(context_parts)


brain_service = BrainService()