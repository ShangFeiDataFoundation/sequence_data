"""增强型 Agent 编排器。

Agent 只负责编排确定性解析工具、调用云端语义提供方、记录执行轨迹并生成
待确认项。字段集合、统计结果和最终结构仍由本地工具产出。
"""

from __future__ import annotations

import logging

from ...config import Settings, get_settings
from ...schemas import (
    AgentEvent,
    AgentReviewItem,
    AgentReviewSeverity,
    AgentRunStatus,
    BatchStatus,
    ResourceStatus,
)
from ...storage import BatchStore, now
from ..pipeline import finalize_batch, parse_resource
from ..semantic import SemanticProvider, build_provider
from .review import build_review_items

logger = logging.getLogger(__name__)


def run_enhanced_agent(
    batch_id: str,
    store: BatchStore,
    settings: Settings | None = None,
) -> None:
    """执行一轮增强型 Agent 解析。"""
    settings = settings or get_settings()
    provider: SemanticProvider | None = None
    run = store.load_agent_run(batch_id)
    require_api = bool(run.get("require_api") or settings.agent_require_api)

    store.update_agent_run(
        batch_id,
        status=AgentRunStatus.RUNNING.value,
        current_step="select_provider",
        started_at=now().isoformat(),
        finished_at=None,
        error=None,
    )
    _emit(
        store,
        batch_id,
        event_type="AGENT_STARTED",
        step="select_provider",
        message="增强型 Agent 已启动。",
        data={"require_api": require_api},
    )

    try:
        provider = build_provider(settings)
        if require_api and provider.name != "openai_compatible":
            raise RuntimeError(
                "增强型 Agent 要求使用云端 API，但 SEMANTIC_API_BASE、"
                "SEMANTIC_API_KEY、SEMANTIC_MODEL 未完整配置或初始化失败。"
            )

        store.update_agent_run(
            batch_id,
            provider=provider.name,
            model=settings.semantic_model,
        )
        _emit(
            store,
            batch_id,
            event_type="PROVIDER_SELECTED",
            step="select_provider",
            message=f"语义提供方已选择：{provider.name}。",
            data={
                "provider": provider.name,
                "model": settings.semantic_model,
                "api_required": require_api,
            },
        )

        record = store.load(batch_id)
        resource_descriptions: dict[str, str] = record.get("resource_descriptions") or {}
        field_descriptions: dict[str, dict[str, str]] = (
            record.get("field_descriptions") or {}
        )
        resources = list(record["resources"])
        total = max(len(resources), 1)
        review_items: list[AgentReviewItem] = []

        for index, resource in enumerate(resources):
            code = resource["resource_code"]
            progress = index / total
            store.update_resource(
                batch_id,
                code,
                status=ResourceStatus.PARSING.value,
                progress=0.1,
                error=None,
            )
            store.update_agent_run(
                batch_id,
                current_step=f"resource:{code}",
                progress=progress,
            )
            _emit(
                store,
                batch_id,
                event_type="RESOURCE_STARTED",
                step=f"resource:{code}",
                resource_code=code,
                message=f"开始解析资源 {code}。",
                data={"index": index + 1, "total": len(resources)},
            )

            try:
                descriptor = parse_resource(
                    batch_id=batch_id,
                    store=store,
                    resource=resource,
                    dialect=record["sql_dialect"],
                    resource_description=resource_descriptions.get(code)
                    or resource_descriptions.get(resource["sql_file"]),
                    field_descriptions=_field_descriptions_for(resource, field_descriptions),
                    provider=provider,
                    settings=settings,
                )
            except Exception as exc:
                logger.exception("增强型 Agent 资源解析失败: %s", code)
                store.update_resource(
                    batch_id,
                    code,
                    status=ResourceStatus.FAILED.value,
                    progress=1.0,
                    error=str(exc),
                )
                review_item = AgentReviewItem(
                    review_id=f"rev_failed_{code}",
                    severity=AgentReviewSeverity.ERROR,
                    code="RESOURCE_FAILED",
                    message=f"资源 {code} 解析失败，需要检查输入文件或修复解析错误。",
                    resource_code=code,
                    evidence={"error": str(exc)},
                )
                review_items.append(review_item)
                store.update_agent_run(
                    batch_id,
                    progress=(index + 1) / total,
                    review_items=[item.model_dump(mode="json") for item in review_items],
                )
                _emit(
                    store,
                    batch_id,
                    event_type="RESOURCE_FAILED",
                    step=f"resource:{code}",
                    resource_code=code,
                    message=f"资源 {code} 解析失败。",
                    data={"error": str(exc)},
                )
                continue

            store.save_result(
                batch_id,
                code,
                descriptor.model_dump(mode="json", by_alias=True),
            )
            store.update_resource(
                batch_id,
                code,
                status=ResourceStatus.COMPLETED.value,
                progress=1.0,
                field_count=len(descriptor.fields),
                error=None,
            )

            new_reviews = build_review_items(
                descriptor,
                limit=settings.agent_max_review_items,
                offset=len(review_items),
            )
            review_items.extend(new_reviews)
            store.update_agent_run(
                batch_id,
                progress=(index + 1) / total,
                review_items=[item.model_dump(mode="json") for item in review_items],
            )
            _emit(
                store,
                batch_id,
                event_type="RESOURCE_COMPLETED",
                step=f"resource:{code}",
                resource_code=code,
                message=f"资源 {code} 解析完成。",
                data={
                    "field_count": len(descriptor.fields),
                    "review_item_count": len(new_reviews),
                    "provider": descriptor.semantic_provider,
                },
            )

        finalize_batch(batch_id, store, provider.name)
        final_record = store.load(batch_id)
        final_status = _agent_status(final_record["status"], review_items)
        store.update_agent_run(
            batch_id,
            status=final_status.value,
            current_step="finished",
            progress=1.0,
            finished_at=now().isoformat(),
            review_items=[item.model_dump(mode="json") for item in review_items],
        )
        _emit(
            store,
            batch_id,
            event_type="AGENT_FINISHED",
            step="finished",
            message=f"增强型 Agent 已结束，状态：{final_status.value}。",
            data={
                "status": final_status.value,
                "review_item_count": len(review_items),
            },
        )
    except Exception as exc:
        logger.exception("增强型 Agent 运行失败: %s", batch_id)
        store.update(
            batch_id,
            status=BatchStatus.FAILED.value,
            finished_at=now().isoformat(),
            error=str(exc),
        )
        store.update_agent_run(
            batch_id,
            status=AgentRunStatus.FAILED.value,
            current_step="failed",
            finished_at=now().isoformat(),
            error=str(exc),
        )
        _emit(
            store,
            batch_id,
            event_type="AGENT_FAILED",
            step="failed",
            message="增强型 Agent 运行失败。",
            data={"error": str(exc)},
        )
    finally:
        if provider is not None:
            provider.close()


def _field_descriptions_for(
    resource: dict,
    field_descriptions: dict[str, dict[str, str]],
) -> dict[str, str]:
    return (
        field_descriptions.get(resource["resource_code"])
        or field_descriptions.get(resource["sql_file"])
        or {}
    )


def _agent_status(batch_status: str, review_items: list[AgentReviewItem]) -> AgentRunStatus:
    if batch_status == BatchStatus.FAILED.value:
        return AgentRunStatus.FAILED
    if review_items:
        return AgentRunStatus.REVIEW_REQUIRED
    return AgentRunStatus.COMPLETED


def _emit(
    store: BatchStore,
    batch_id: str,
    *,
    event_type: str,
    step: str,
    message: str,
    resource_code: str | None = None,
    data: dict | None = None,
) -> None:
    event = AgentEvent(
        event_id="evt_pending",
        timestamp=now(),
        event_type=event_type,
        step=step,
        resource_code=resource_code,
        message=message,
        data=data or {},
    )
    payload = event.model_dump(mode="json")
    payload["event_id"] = f"evt_{len(store.load_agent_run(batch_id).get('events', [])) + 1:06d}"
    store.append_agent_event(batch_id, payload)
