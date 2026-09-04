from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    series: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    era: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dpi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parser: Mapped[str] = mapped_column(String(32), default="vision_pdf", nullable=False)
    vision_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    jobs: Mapped[list["OcrJob"]] = relationship(back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_documents_title", "title"),)


class OcrJob(Base):
    __tablename__ = "ocr_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    current_page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pages_requested: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(back_populates="jobs")


class ScanJob(Base):
    __tablename__ = "scan_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    current: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OcrPageJob(Base):
    """Page-level OCR task for the multiprocess worker pool (crash-recoverable)."""

    __tablename__ = "ocr_page_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    page_num: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    dpi: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    pdf_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    keep_page_images: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_ocr_page_jobs_job_status", "job_id", "status"),
        Index("ix_ocr_page_jobs_pending", "status", "created_at"),
    )
