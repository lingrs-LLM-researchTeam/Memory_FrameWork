"""Local provider doubles exercise the real retry/streaming and summary paths."""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_livingmemory.core.processors.llm_usage import normalize_usage
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.processors import memory_processor as processor_module
from astrbot_plugin_livingmemory.core.prompts import prompt_manager
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.claim_evidence import CLAIM_INSTRUCTION


def response(usage=None, text="ok"):
    return NS(completion_text=text, is_chunk=False,
              raw_completion=NS(model="test-model", usage=usage))


def provider(value):
    return NS(provider_config={"id":"private-provider-id", "model":"test-model", "api_key":"SECRET"},
              text_chat=AsyncMock(return_value=value))


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path, caplog):
    manager = prompt_manager.PromptManager(str(tmp_path))
    monkeypatch.setattr(prompt_manager, "get_prompt_manager", lambda: manager)
    caplog.set_level(logging.INFO)
    return manager


def events(caplog):
    marker = "[LivingMemoryUsage] "
    return [json.loads(r.message.split(marker, 1)[1]) for r in caplog.records if marker in r.message]


@pytest.mark.parametrize("shape", [dict, lambda **kw: NS(**kw)])
def test_deepseek_raw_counts(shape):
    row = normalize_usage(response(shape(prompt_tokens=100, completion_tokens=12,
        prompt_cache_hit_tokens=60, prompt_cache_miss_tokens=40,
        completion_tokens_details={"reasoning_tokens":8})))
    assert (row["input_tokens"], row["cached_input_tokens"], row["uncached_input_tokens"]) == (100,60,40)
    assert row["total_tokens"] == 112  # reasoning is already included in output
    assert row["reasoning_tokens"] == 8


def test_standard_explicit_zero_has_priority():
    row = normalize_usage(response(dict(prompt_tokens=100, completion_tokens=10,
        prompt_tokens_details={"cached_tokens":0}, prompt_cache_hit_tokens=60,
        prompt_cache_miss_tokens=40)))
    assert row["cached_input_tokens"] == 0
    assert row["uncached_input_tokens"] == 100


def test_responses_api_shape():
    row = normalize_usage(response(dict(input_tokens=200, output_tokens=20,
        input_tokens_details={"cached_tokens":150})))
    assert (row["cached_input_tokens"],row["uncached_input_tokens"],row["total_tokens"]) == (150,50,220)


def test_missing_cache_is_unknown_even_when_adapter_defaults_to_zero():
    r = response(dict(prompt_tokens=100, completion_tokens=10))
    r.usage = NS(input_other=100,input_cached=0,output=10)
    row = normalize_usage(r)
    assert row["input_tokens"] == 100
    assert row["cached_input_tokens"] is None
    assert row["usage_status"] == "partial"


@pytest.mark.parametrize("r", [None, NS(completion_text="ok"), response(),
    NS(usage=NS(input_other=0,input_cached=0,output=0))])
def test_missing_usage_not_fabricated_as_zero(r):
    row = normalize_usage(r)
    assert row["usage_status"] == "unavailable"
    assert row["total_tokens"] is None


def test_normalized_usage_fallback_and_explicit_raw_zero():
    row = normalize_usage(NS(usage=NS(input_other=40,input_cached=60,output=10)))
    assert row["usage_source"] == "astrbot_normalized"
    assert row["total_tokens"] == 110
    zero = normalize_usage(response(dict(prompt_tokens=0,completion_tokens=0,prompt_cache_hit_tokens=0)))
    assert zero["usage_status"] == "reported"
    assert zero["total_tokens"] == 0


@pytest.mark.parametrize("cached", [True,-1,"50",101])
def test_invalid_cache_counts_do_not_create_hits(cached):
    row = normalize_usage(response(dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=cached)))
    assert row["cached_input_tokens"] is None


def test_conflicting_deepseek_split_is_unknown():
    row = normalize_usage(response(dict(prompt_tokens=100,completion_tokens=10,
        prompt_cache_hit_tokens=60,prompt_cache_miss_tokens=70)))
    assert row["cached_input_tokens"] is None
    assert row["usage_status"] == "inconsistent"


@pytest.mark.asyncio
async def test_real_call_logs_counts_without_payload_or_keys(caplog):
    p = provider(response(dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=60)))
    proc = MemoryProcessor(llm_provider=p)
    assert await proc._call_llm_with_retry("PRIVATE CHAT", "PRIVATE PERSONA", operation="summary_private") == "ok"
    e = events(caplog)[0]
    assert e["cached_input_tokens"] == 60 and e["operation"] == "summary_private"
    serialized = json.dumps(e)
    for secret in ("PRIVATE CHAT","PRIVATE PERSONA","SECRET","private-provider-id"):
        assert secret not in serialized
    assert len(e["system_prompt_sha256"]) == 64
    assert p.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_retry_counts_failed_attempt_as_unknown(monkeypatch, caplog):
    monkeypatch.setattr(processor_module.asyncio,"sleep",AsyncMock())
    p = provider(None)
    p.text_chat.side_effect = [TimeoutError("PRIVATE ERROR"),response(dict(prompt_tokens=10,completion_tokens=2,prompt_cache_hit_tokens=6))]
    proc = MemoryProcessor(llm_provider=p)
    assert await proc._call_llm_with_retry("p","s",max_retries=2) == "ok"
    rows=events(caplog)
    assert [r["status"] for r in rows] == ["error","success"]
    assert rows[0]["total_tokens"] is None
    assert rows[0]["logical_call_id"] == rows[1]["logical_call_id"]
    assert rows[0]["request_id"] != rows[1]["request_id"]
    stats=proc.get_usage_stats()[0]
    assert stats["attempts"] == 2 and stats["usage_unavailable"] == 1
    assert stats["cache_hit_ratio"] == .6 and stats["cache_coverage"] == .5


@pytest.mark.asyncio
async def test_stream_final_usage_only_and_auto_fallback(caplog):
    p=provider(None)
    p.text_chat.side_effect=RuntimeError("non-stream not supported")
    async def stream(**kwargs):
        for _ in range(3):
            yield NS(is_chunk=True, completion_text="part",usage=NS(input_other=400,input_cached=600,output=10))
        yield response(dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=60))
    p.text_chat_stream=stream
    proc=MemoryProcessor(llm_provider=p)
    assert await proc._call_llm_with_retry("p","s",max_retries=1) == "ok"
    rows=events(caplog)
    assert len(rows)==2
    assert rows[0]["transport"]=="non_stream" and rows[0]["status"]=="error"
    assert rows[1]["transport"]=="stream" and rows[1]["input_tokens"]==100
    assert proc.get_usage_stats()[0]["input_tokens"]==100


@pytest.mark.asyncio
async def test_interrupted_stream_does_not_treat_chunk_usage_as_final(caplog):
    async def stream(**kwargs):
        yield NS(is_chunk=True,completion_text="partial",usage=NS(input_other=40,input_cached=60,output=10))
        raise TimeoutError("interrupted")
    p=provider(None); p.text_chat_stream=stream
    proc=MemoryProcessor(llm_provider=p,config={"llm_stream_mode":"always"})
    with pytest.raises(TimeoutError):
        await proc._call_llm_with_retry("p","s",max_retries=1)
    assert events(caplog)[0]["input_tokens"] is None


@pytest.mark.asyncio
async def test_cancellation_is_logged_without_retry(caplog):
    p=provider(None); p.text_chat.side_effect=asyncio.CancelledError()
    proc=MemoryProcessor(llm_provider=p)
    with pytest.raises(asyncio.CancelledError):
        await proc._call_llm_with_retry("p","s")
    assert p.text_chat.await_count==1
    assert events(caplog)[0]["status"]=="cancelled"


@pytest.mark.asyncio
async def test_logging_failure_does_not_fail_or_repeat_call(monkeypatch):
    from astrbot.api import logger
    proc=MemoryProcessor(llm_provider=provider(response(dict(prompt_tokens=10,completion_tokens=2))))
    monkeypatch.setattr(logger,"info",lambda *a,**k: (_ for _ in ()).throw(OSError("log full")))
    assert await proc._call_llm_with_retry("p","s") == "ok"
    assert proc._llm_provider.text_chat.await_count==1


@pytest.mark.asyncio
async def test_ratio_is_token_weighted_and_missing_cache_excluded():
    p=provider(None)
    p.text_chat.side_effect=[response(dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=90)),
        response(dict(prompt_tokens=900,completion_tokens=10,prompt_cache_hit_tokens=90)),
        response(dict(prompt_tokens=1000,completion_tokens=10))]
    proc=MemoryProcessor(llm_provider=p)
    for _ in range(3): await proc._call_llm_with_retry("p","s")
    row=proc.get_usage_stats()[0]
    assert row["cache_hit_ratio"]==.18  # not (90% + 10%)/2
    assert row["input_tokens"]==2000 and row["cache_observed_input_tokens"]==1000
    row["attempts"]=0
    assert proc.get_usage_stats()[0]["attempts"]==3


@pytest.mark.asyncio
@pytest.mark.parametrize("group,operation",[(False,"summary_private"),(True,"summary_group")])
async def test_summary_path_preserves_output_rules_and_labels(group,operation,caplog):
    p=provider(response(dict(prompt_tokens=100,completion_tokens=10,prompt_cache_hit_tokens=50),
        '{"summary":"测试","topics":["会议"],"key_facts":[],"importance":0.7}'))
    proc=MemoryProcessor(llm_provider=p)
    messages=[Message(id=1,session_id="s",role="user",content="明天开会",sender_id="u",sender_name="张三")]
    content,metadata,importance=await proc.process_conversation(messages,is_group_chat=group)
    assert importance==.7 and metadata["claims"][0]["evidence"][0]["quote"]=="明天开会"
    assert events(caplog)[0]["operation"]==operation
    sent=p.text_chat.call_args.kwargs
    assert sent["system_prompt"].startswith(CLAIM_INSTRUCTION)
    assert sent["system_prompt"].count(CLAIM_INSTRUCTION)==1
    assert "明天开会" in sent["prompt"] and "{conversation}" not in sent["prompt"]


@pytest.mark.asyncio
async def test_merge_format_precedes_count_and_content(caplog):
    p=provider(response(None,'{"summary":"合并","key_facts":["甲","乙"],"topics":["测试"],"importance":0.8}'))
    proc=MemoryProcessor(llm_provider=p)
    result=await proc.merge_memories([{"content":"秘密内容甲"},{"content":"秘密内容乙"}])
    prompt=p.text_chat.call_args.kwargs["prompt"]
    assert prompt.index('"importance": 0.5') < prompt.index("共 2 条") < prompt.index("秘密内容甲")
    assert result=={"summary":"合并","key_facts":["甲","乙"],"topics":["测试"],"importance":.8}
    assert events(caplog)[0]["operation"]=="merge_memories"


@pytest.mark.asyncio
async def test_persona_rules_precede_persona_and_custom_templates_stay_live(isolate):
    context=NS(persona_manager=NS(get_persona=AsyncMock(return_value=NS(system_prompt="PERSONA-A"))))
    proc=MemoryProcessor(llm_provider=provider(None),context=context)
    text=await proc._build_system_prompt_with_persona("a")
    assert text.index("时间转换") < text.index("PERSONA-A")
    context.persona_manager.get_persona.return_value=NS(system_prompt="PERSONA-B")
    other=await proc._build_system_prompt_with_persona("b")
    assert text.split("PERSONA-A")[0]==other.split("PERSONA-B")[0]
    isolate.update_prompt("memory_system_prompt_with_persona","CUSTOM {persona_prompt} THEN {base_prompt}")
    assert (await proc._build_system_prompt_with_persona("b")).startswith("CUSTOM PERSONA-B THEN ")
    isolate.update_prompt("private_chat_prompt","KEEP {conversation} BEFORE {current_date}")
    assert proc._get_chat_prompt(False)=="KEEP {conversation} BEFORE {current_date}"


def test_both_persona_fallbacks_keep_rules_before_dynamic_value():
    a=MemoryProcessor._build_enhanced_prompt_fallback("BASE","PERSONA", "DATE")
    b=prompt_manager.PROMPT_REGISTRY["memory_system_prompt_with_persona"]["default"]
    assert a.index("时间转换") < a.index("PERSONA")
    assert b.index("时间转换") < b.index("{persona_prompt}")


@pytest.mark.asyncio
async def test_offline_reader_deduplicates_rotated_logs_and_reports_bad_lines(tmp_path,caplog):
    proc=MemoryProcessor(llm_provider=provider(response(dict(prompt_tokens=10,completion_tokens=2,prompt_cache_hit_tokens=6))))
    await proc._call_llm_with_retry("p","s")
    line="prefix [LivingMemoryUsage] "+json.dumps(events(caplog)[0])+"\n"
    path=tmp_path/"test.log"; path.write_text(line+line+"[LivingMemoryUsage] invalid\n",encoding="utf-8")
    script=Path(__file__).resolve().parents[1]/"scripts/summarize_llm_usage.py"
    spec=importlib.util.spec_from_file_location("usage_script_test",script)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    result=mod.summarize([path])
    assert result["observed_attempts"]==1 and result["duplicate_events"]==1 and result["malformed_events"]==1
    assert result["groups"][0]["cache_hit_ratio"]==.6
