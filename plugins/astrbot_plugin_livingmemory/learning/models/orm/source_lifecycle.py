"""Persistent source links; revocation never requires deleting audit records."""
from sqlalchemy import Column, String, Integer, Float
from .base import Base


class LearningSource(Base):
    __tablename__ = "learning_sources"
    source_scope = Column(String(255), primary_key=True)
    source_uid = Column(String(255), primary_key=True)
    raw_message_id = Column(Integer, nullable=True, index=True)
    group_id = Column(String(255), nullable=False, index=True)
    state = Column(String(32), nullable=False)
    revision = Column(Integer, nullable=False, default=0)
    updated_at = Column(Float, nullable=False)


class InvalidatedLearningGroup(Base):
    __tablename__ = "invalidated_learning_groups"
    group_id = Column(String(255), primary_key=True)
    source_scope = Column(String(255), nullable=False)
    invalidated_at = Column(Float, nullable=False)
