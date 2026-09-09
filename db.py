from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///truck_repair.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_kwargs = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Service(Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    category: Mapped[str] = mapped_column(String(80), default="Truck Repair")
    phone: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(String(250), default="")
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, default="")
    languages: Mapped[str] = mapped_column(String(250), default="")
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hours: Mapped[str] = mapped_column(String(150), default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    submitted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

def _add_missing_columns() -> None:
    existing = {c["name"] for c in inspect(engine).get_columns("services")}
    additions = {
        "languages": "VARCHAR(250) DEFAULT ''",
        "rating": "FLOAT",
        "review_count": "INTEGER",
        "hours": "VARCHAR(150) DEFAULT ''",
        "website": "VARCHAR(300) DEFAULT ''",
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE services ADD COLUMN {name} {ddl}"))

def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()

def add_service(*, name: str, category: str, phone: str, address: str, latitude: float, longitude: float,
                notes: str = "", languages: str = "", rating: float | None = None,
                review_count: int | None = None, hours: str = "", website: str = "",
                submitted_by: int | None, approved: bool = False) -> Service:
    with SessionLocal() as session:
        item = Service(name=name.strip(), category=(category or "Truck Repair").strip(), phone=(phone or "").strip(),
            address=(address or "").strip(), latitude=latitude, longitude=longitude, notes=(notes or "").strip(),
            languages=(languages or "").strip(), rating=rating, review_count=review_count,
            hours=(hours or "").strip(), website=(website or "").strip(), submitted_by=submitted_by, approved=approved)
        session.add(item); session.commit(); session.refresh(item); return item

def get_approved_services():
    with SessionLocal() as session:
        return list(session.scalars(select(Service).where(Service.approved.is_(True)).order_by(Service.name.asc())))

def get_pending_services():
    with SessionLocal() as session:
        return list(session.scalars(select(Service).where(Service.approved.is_(False)).order_by(Service.created_at.asc())))

def get_user_services(user_id: int):
    with SessionLocal() as session:
        return list(session.scalars(select(Service).where(Service.submitted_by == user_id).order_by(Service.created_at.desc())))

def get_service(service_id: int):
    with SessionLocal() as session: return session.get(Service, service_id)

def approve_service(service_id: int) -> bool:
    with SessionLocal() as session:
        item=session.get(Service, service_id)
        if not item: return False
        item.approved=True; session.commit(); return True

def delete_service(service_id: int) -> bool:
    with SessionLocal() as session:
        item=session.get(Service, service_id)
        if not item: return False
        session.delete(item); session.commit(); return True
