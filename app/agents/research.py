"""Research Agent with tool-scoped retry and auditable failures."""

from collections.abc import Awaitable, Callable

from app.mcp.research_client import (
    ResearchMCPClient,
    ResearchServiceError,
    run_async,
)
from app.models.company import CompanyProfile
from app.models.research import ResearchQueryResult, ResearchResult
from app.models.trace import TraceStatus
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TimedTrace, TraceWriter

TemporaryResearchError = (TimeoutError, ResearchServiceError)


async def _call_with_retry(
    *,
    task_id: str,
    tool_name: str,
    query_type: str,
    query: str,
    call: Callable[[], Awaitable[ResearchQueryResult]],
    max_retry: int,
    trace: TraceWriter,
    fault: ResearchFaultInjector,
) -> ResearchQueryResult:
    for attempt in range(max_retry + 1):
        timer = TimedTrace()
        try:
            if fault.should_fail(task_id, tool_name):
                raise TimeoutError(f"injected timeout for {tool_name}")
            result = await call()
            end_time, latency_ms = timer.finish()
            trace.write(
                task_id=task_id,
                node="research",
                event_type="TOOL_CALL",
                status=TraceStatus.SUCCESS,
                start_time=timer.start_time,
                end_time=end_time,
                latency_ms=latency_ms,
                input_summary=f"tool={tool_name};attempt={attempt + 1}",
                output_summary=(
                    f"found={result.found};candidates={len(result.candidate_evidence)};"
                    f"facts={len(result.facts)};rejected={result.rejected_result_count};"
                    f"fetched={result.fetched_content_count};"
                    f"fetch_failed={result.failed_content_count};"
                    f"verified={result.completed_verification_count};"
                    f"verify_failed={result.failed_verification_count}"
                ),
            )
            return result
        except TemporaryResearchError as exc:
            end_time, latency_ms = timer.finish()
            has_retry = attempt < max_retry
            trace.write(
                task_id=task_id,
                node="research",
                event_type="RETRY" if has_retry else "TOOL_CALL",
                status=TraceStatus.RETRY if has_retry else TraceStatus.FAILED,
                start_time=timer.start_time,
                end_time=end_time,
                latency_ms=latency_ms,
                input_summary=f"tool={tool_name};attempt={attempt + 1}",
                output_summary="temporary failure",
                error=f"{type(exc).__name__}: {exc}",
            )
            if not has_retry:
                return ResearchQueryResult(
                    query_type=query_type,
                    query=query,
                    found=False,
                    facts=[],
                    source="unavailable",
                    status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                )
    raise AssertionError("retry loop exited unexpectedly")


async def research_company_and_industry_async(
    task_id: str,
    company: CompanyProfile,
    anomaly_flags: list[str],
    client: ResearchMCPClient,
    max_retry: int,
    trace: TraceWriter,
    fault: ResearchFaultInjector,
) -> ResearchResult:
    company_result = await _call_with_retry(
        task_id=task_id,
        tool_name="search_company",
        query_type="company",
        query=company.company_name,
        call=lambda: client.search_company(company.company_name),
        max_retry=max_retry,
        trace=trace,
        fault=fault,
    )
    industry_result = await _call_with_retry(
        task_id=task_id,
        tool_name="search_industry",
        query_type="industry",
        query=company.industry,
        call=lambda: client.search_industry(company.industry),
        max_retry=max_retry,
        trace=trace,
        fault=fault,
    )
    failed_tools = [
        tool_name
        for tool_name, result in (
            ("search_company", company_result),
            ("search_industry", industry_result),
        )
        if result.status == "FAILED"
    ]
    found_count = int(company_result.found) + int(industry_result.found)
    if failed_tools:
        status = "INCOMPLETE"
    elif found_count == 2:
        status = "COMPLETE"
    elif found_count == 1:
        status = "PARTIAL"
    else:
        status = "EMPTY"
    results = (company_result, industry_result)
    all_facts = [fact for result in results for fact in result.facts]
    all_candidates = [item for result in results for item in result.candidate_evidence]
    all_evidence = [item for result in results for item in result.evidence]
    fetch_statuses = {result.content_fetch_status for result in results}
    fetched_content_count = sum(result.fetched_content_count for result in results)
    failed_content_count = sum(result.failed_content_count for result in results)
    if fetched_content_count and failed_content_count:
        content_fetch_status = "PARTIAL"
    elif failed_content_count:
        content_fetch_status = "FAILED"
    elif fetched_content_count:
        content_fetch_status = "COMPLETE"
    elif "DISABLED" in fetch_statuses:
        content_fetch_status = "DISABLED"
    else:
        content_fetch_status = "NOT_NEEDED"
    content_fetch_incomplete = content_fetch_status in {
        "DISABLED",
        "PARTIAL",
        "FAILED",
    }
    if content_fetch_incomplete:
        status = "INCOMPLETE"
    verification_execution_statuses = {
        result.verification_execution_status for result in results
    }
    completed_verification_count = sum(
        result.completed_verification_count for result in results
    )
    failed_verification_count = sum(
        result.failed_verification_count for result in results
    )
    if completed_verification_count and failed_verification_count:
        verification_execution_status = "PARTIAL"
    elif failed_verification_count:
        verification_execution_status = "FAILED"
    elif completed_verification_count:
        verification_execution_status = "COMPLETE"
    elif "DISABLED" in verification_execution_statuses:
        verification_execution_status = "DISABLED"
    else:
        verification_execution_status = "NOT_NEEDED"
    verification_incomplete = verification_execution_status in {
        "DISABLED",
        "PARTIAL",
        "FAILED",
    }
    if verification_incomplete:
        status = "INCOMPLETE"
    verification_status = "NOT_FOUND"
    fact_statuses = {fact.verification_status for fact in all_facts}
    if "CONFLICTING" in fact_statuses:
        verification_status = "CONFLICTING"
    elif "CORROBORATED" in fact_statuses:
        verification_status = "CORROBORATED"
    elif "SUPPORTED" in fact_statuses:
        verification_status = "SUPPORTED"
    elif all_candidates:
        verification_status = "UNVERIFIED"
    return ResearchResult(
        company_name=company.company_name,
        industry=company.industry,
        anomaly_flags=anomaly_flags,
        company_result=company_result,
        industry_result=industry_result,
        status=status,
        execution_status="INCOMPLETE" if failed_tools else "COMPLETE",
        verification_status=verification_status,
        candidate_count=len(all_candidates),
        verified_fact_count=len(all_facts),
        rejected_result_count=sum(
            item.evidence_stage == "REJECTED" for item in all_evidence
        ),
        content_fetch_status=content_fetch_status,
        fetched_content_count=fetched_content_count,
        failed_content_count=failed_content_count,
        content_fetch_incomplete=content_fetch_incomplete,
        verification_execution_status=verification_execution_status,
        completed_verification_count=completed_verification_count,
        failed_verification_count=failed_verification_count,
        verification_incomplete=verification_incomplete,
        external_research_incomplete=(
            bool(failed_tools) or content_fetch_incomplete or verification_incomplete
        ),
        failed_tools=failed_tools,
    )


def research_company_and_industry(
    task_id: str,
    company: CompanyProfile,
    anomaly_flags: list[str],
    max_retry: int,
    trace: TraceWriter,
    fault: ResearchFaultInjector,
    client: ResearchMCPClient | None = None,
) -> ResearchResult:
    client = client or ResearchMCPClient()
    return run_async(
        research_company_and_industry_async(
            task_id,
            company,
            anomaly_flags,
            client,
            max_retry,
            trace,
            fault,
        )
    )
