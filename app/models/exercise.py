import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, Float, DateTime, func, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class ExerciseType(Base, TimestampMixin):
    """Cartographie de tous les types d'exercices possibles."""
    __tablename__ = "exercise_types"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Namespace
    pays: Mapped[str] = mapped_column(String(50), default="senegal", index=True)
    exam_type: Mapped[str] = mapped_column(String(50), index=True)
    serie: Mapped[str | None] = mapped_column(String(20), index=True)
    matiere: Mapped[str] = mapped_column(String(50), index=True)
    chapitre: Mapped[str] = mapped_column(String(100), index=True)

    # Identification
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    # ex: bac_s2_maths_derivees_produit
    nom: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)

    # Méthodologie
    methode_steps: Mapped[list | None] = mapped_column(JSONB)
    # [{"step": 1, "action": "Identifier u et v", "conseil": "..."}]
    formules_cles: Mapped[list | None] = mapped_column(JSONB)
    pieges: Mapped[list | None] = mapped_column(JSONB)
    prerequis: Mapped[list | None] = mapped_column(JSONB)
    # slugs des types prérequis

    # Stats BAC
    frequence_exam: Mapped[str] = mapped_column(String(30), default="fréquent")
    # rare | fréquent | très_fréquent | quasi_certain
    difficulte: Mapped[int] = mapped_column(Integer, default=2)
    # 1=facile, 2=moyen, 3=difficile

    # Contenu généré
    exemple_resolu: Mapped[str | None] = mapped_column(Text)
    exercice_type: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StudentChapterMastery(Base):
    """Profil cognitif détaillé par élève et chapitre."""
    __tablename__ = "student_chapter_mastery"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    matiere: Mapped[str] = mapped_column(String(50), index=True)
    chapitre: Mapped[str] = mapped_column(String(100), index=True)

    # Niveau de maîtrise global 0.0 → 1.0
    mastery_level: Mapped[float] = mapped_column(Float, default=0.0)

    # Types d'exercices
    types_vus: Mapped[list | None] = mapped_column(JSONB, default=list)
    types_maitrises: Mapped[list | None] = mapped_column(JSONB, default=list)
    types_faibles: Mapped[list | None] = mapped_column(JSONB, default=list)

    # Analyse
    weak_points: Mapped[list | None] = mapped_column(JSONB, default=list)
    # ["Oublie de v² au dénominateur", "Confusion u' et v'"]
    learning_style: Mapped[str] = mapped_column(String(30), default="balanced")
    # visual | example_based | formula_based | balanced

    # Stats
    practice_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    last_practiced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeBase(Base, TimestampMixin):
    """Base de connaissances évolutive — bonnes réponses archivées."""
    __tablename__ = "knowledge_base"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Namespace
    matiere: Mapped[str] = mapped_column(String(50), index=True)
    chapitre: Mapped[str | None] = mapped_column(String(100), index=True)
    exercise_type_slug: Mapped[str | None] = mapped_column(String(100), index=True)

    # Contenu
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reponse: Mapped[str] = mapped_column(Text, nullable=False)
    methode_utilisee: Mapped[str | None] = mapped_column(String(100))

    # Embedding de la question pour recherche
    question_embedding: Mapped[list | None] = mapped_column(JSONB)

    # Stats
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=1.0)

    is_validated: Mapped[bool] = mapped_column(Boolean, default=False)


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Document source
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    source_filename: Mapped[str | None] = mapped_column(String(255))

    # Métadonnées pédagogiques
    title: Mapped[str | None] = mapped_column(Text)
    exam_type: Mapped[str | None] = mapped_column(String(50), index=True)
    serie: Mapped[str | None] = mapped_column(String(20), index=True)
    matiere: Mapped[str | None] = mapped_column(String(50), index=True)
    chapitre: Mapped[str | None] = mapped_column(String(100), index=True)
    niveau: Mapped[int] = mapped_column(Integer, default=2)
    annee: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    bareme: Mapped[dict | None] = mapped_column(JSONB)

    # Position dans le document source
    exercise_number: Mapped[int | None] = mapped_column(Integer)
    page_debut: Mapped[int | None] = mapped_column(Integer)
    page_fin: Mapped[int | None] = mapped_column(Integer)

    # Fichiers générés (chemins locaux sur le serveur)
    exercise_path: Mapped[str | None] = mapped_column(Text)
    correction_path: Mapped[str | None] = mapped_column(Text)
    correction_generated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Statut
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )