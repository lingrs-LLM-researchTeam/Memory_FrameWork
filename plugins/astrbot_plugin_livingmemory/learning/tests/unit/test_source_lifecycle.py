"""Use actual SQLite facades to exercise capture, revocation, retry and read gates."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select

from self_learning_EterU.config import PluginConfig
from self_learning_EterU.core.database.engine import DatabaseEngine
from self_learning_EterU.services.database.facades.message_facade import MessageFacade
from self_learning_EterU.services.database.source_lifecycle import consume_source_event
from self_learning_EterU.models.orm.message import RawMessage
from self_learning_EterU.models.orm.learning import PersonaLearningReview
from self_learning_EterU.models.orm.source_lifecycle import LearningSource
from self_learning_EterU.services.hooks.llm_hook_handler import LLMHookHandler


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = DatabaseEngine("sqlite:///" + (tmp_path / "learning.db").as_posix())
    engine.require_memory_authority = True
    await engine.create_tables(enable_auto_migration=True)
    manager = SimpleNamespace(engine=engine, is_ready=True)
    facade = MessageFacade(engine, PluginConfig(require_memory_authority=True))
    try:
        yield manager, facade
    finally:
        await engine.close()


def raw(uid="one", group="42"):
    return dict(sender_id="alice", sender_name="Alice", message="synthetic preference",
                group_id=group, platform="qq", timestamp=time.time(), message_id=uid,
                source_scope=f"bot-a:GroupMessage:{group}", source_uid=uid)


def recalled(uid="one", group="42"):
    return dict(session_id=f"bot-a:GroupMessage:{group}", source_uid=uid, revision=1, state="revoked")


@pytest.mark.asyncio
async def test_revocation_hides_raw_and_reviews_but_preserves_audit_rows(db):
    manager, facade = db
    mid = await facade.save_raw_message(raw())
    assert mid > 0
    await facade.save_raw_message(raw("two", "88"))
    async with manager.engine.get_session() as session:
        session.add(PersonaLearningReview(group_id="42", timestamp=time.time(), update_type="style", status="approved"))
        await session.commit()
    assert len(await facade.get_unprocessed_messages(group_id="42")) == 1
    await consume_source_event(manager, recalled())
    await consume_source_event(manager, recalled())  # Idempotent redelivery.
    assert await facade.get_unprocessed_messages(group_id="42") == []
    assert await facade.get_recent_raw_messages("42") == []
    assert len(await facade.get_unprocessed_messages(group_id="88")) == 1
    async with manager.engine.get_session() as session:
        assert (await session.execute(select(PersonaLearningReview))).scalars().all() == []
        saved = (await session.execute(select(RawMessage).execution_options(include_invalidated_sources=True))).scalars().all()
        assert len(saved) == 2
        review = (await session.execute(select(PersonaLearningReview).execution_options(include_invalidated_sources=True))).scalar_one()
        assert review.status == "source_invalidated"
    assert await facade.save_raw_message(raw()) == 0


@pytest.mark.asyncio
async def test_recall_before_capture_and_restart_cannot_resurrect_source(db):
    manager, facade = db
    await consume_source_event(manager, recalled())
    assert await facade.save_raw_message(raw()) == 0
    async with manager.engine.get_session() as session:
        assert (await session.get(LearningSource, ("bot-a:GroupMessage:42", "one"))).state == "revoked"
    assert await facade.save_raw_message(raw("new")) > 0
    assert len(await facade.get_unprocessed_messages(group_id="42")) == 1


@pytest.mark.asyncio
async def test_hook_generates_candidates_and_always_passes_them_through_gate():
    producer = AsyncMock(return_value=[{"memory_id": 7}])
    gate = AsyncMock(return_value=["governed evidence"])
    authority = SimpleNamespace(find_evidence_candidates=producer, govern_external_context=gate)
    handler = LLMHookHandler(
        PluginConfig(require_memory_authority=True, enable_llm_hooks=True),
        Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), {},
        feature_delegation=SimpleNamespace(memory_plugin=lambda: SimpleNamespace(star_cls=authority)),
    )
    extras = {}
    event = SimpleNamespace(get_extra=lambda key, default=None: extras.get(key, default),
                            set_extra=lambda key, value: extras.__setitem__(key, value))
    req = SimpleNamespace(prompt="tea", system_prompt="", extra_user_content_parts=[])
    await handler.handle(event, req)
    producer.assert_awaited_once_with(event, "tea")
    gate.assert_awaited_once_with(event, [{"memory_id": 7}])
    assert len(req.extra_user_content_parts) == 1
    handler._social_context_injector.format_complete_context.assert_not_called()
