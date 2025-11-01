from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .db import Base

class Tasting(Base):
    __tablename__ = "tastings"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    title = Column(String(255), nullable=False)
    category = Column(String(64))
    aromas = Column(Text)
    aftertaste = Column(Text)
    note = Column(Text)
    tz = Column(String(64), default="Europe/Amsterdam")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    photos = relationship("Photo", back_populates="tasting", cascade="all, delete-orphan")

class Photo(Base):
    __tablename__ = "photos"
    id = Column(Integer, primary_key=True)
    tasting_id = Column(Integer, ForeignKey("tastings.id", ondelete="CASCADE"), index=True)
    tg_file_id = Column(String(256))   # file_id из Telegram
    s3_key = Column(String(512))       # ключ в S3
    filename = Column(String(255))
    width = Column(Integer)
    height = Column(Integer)
    size_bytes = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    tasting = relationship("Tasting", back_populates="photos")
