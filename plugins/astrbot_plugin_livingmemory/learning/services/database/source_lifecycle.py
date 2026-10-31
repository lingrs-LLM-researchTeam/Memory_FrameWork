"""Source-aware learning reads and idempotent invalidation for authority mode."""
import time

from sqlalchemy import event, exists, select, update
from sqlalchemy.orm import Session, with_loader_criteria

from ...models.orm.base import Base
from ...models.orm.message import RawMessage, FilteredMessage
from ...models.orm.learning import PersonaLearningReview, StyleLearningReview
from ...models.orm.source_lifecycle import LearningSource, InvalidatedLearningGroup


def active_raw(model):
    return exists().where(
        LearningSource.raw_message_id == model.id, LearningSource.state == "active"
    )


@event.listens_for(Session, "do_orm_execute")
def source_scoped_reads(state):
    if (not state.is_select or not state.session.info.get("require_memory_authority")
        or state.execution_options.get("include_invalidated_sources")):
        return
    statement = state.statement.options(
        with_loader_criteria(RawMessage, active_raw, include_aliases=True),
        with_loader_criteria(FilteredMessage, lambda model: exists().where(
            LearningSource.raw_message_id == model.raw_message_id,
            LearningSource.state == "active",
        ), include_aliases=True),
    )
    # Old derived records do not carry per-message provenance. Conservatively
    # quarantine that group's local derivatives, including in-flight writes.
    # Governed LLM evidence continues to come from LivingMemory's live sources.
    excluded = {RawMessage, FilteredMessage, LearningSource, InvalidatedLearningGroup}
    for mapper in Base.registry.mappers:
        model = mapper.class_
        if model not in excluded and hasattr(model, "group_id"):
            statement = statement.options(with_loader_criteria(
                model, ~exists().where(InvalidatedLearningGroup.group_id == model.group_id),
                include_aliases=True,
            ))
    state.statement = statement


async def consume_source_event(manager, source_event):
    if source_event.get("state") == "active":
        return
    if not manager or not manager.is_ready:
        raise RuntimeError("self_learning database is not ready")
    scope, uid = source_event["session_id"], source_event["source_uid"]
    async with manager.engine.get_session() as session:
        key = (scope, uid)
        record = await session.get(LearningSource, key)
        revision = int(source_event["revision"])
        if record is not None and record.state == "revoked":
            return
        if record is not None and record.revision >= revision and record.state != "active":
            return
        group_id = record.group_id if record is not None else scope.split(":", 2)[-1]
        if record is None:
            record = LearningSource(source_scope=scope, source_uid=uid, group_id=group_id)
            session.add(record)
        record.state = "revoked" if source_event["state"] == "revoked" else "revised"
        record.revision = max(revision, record.revision or 0)
        record.updated_at = time.time()
        if record.raw_message_id is not None:
            await session.execute(update(RawMessage).where(
                RawMessage.id == record.raw_message_id
            ).values(processed=True))
            await session.execute(update(FilteredMessage).where(
                FilteredMessage.raw_message_id == record.raw_message_id
            ).values(processed=True))
        # Unknown sources need a tombstone for late delivery, but cannot have
        # contributed to our derived records. Only a linked sample justifies
        # quarantining this group's derivatives.
        if record.raw_message_id is not None:
            invalidated = await session.get(InvalidatedLearningGroup, group_id)
            if invalidated is None:
                session.add(InvalidatedLearningGroup(
                    group_id=group_id, source_scope=scope, invalidated_at=time.time(),
                ))
            for model in (PersonaLearningReview, StyleLearningReview):
                await session.execute(update(model).where(model.group_id == group_id).values(
                    status="source_invalidated", reviewer_comment="Source revoked; local provenance requires revalidation",
                ))
        await session.commit()
