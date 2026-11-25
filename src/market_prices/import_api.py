"""Upload preview, explicit confirmation, durable task state and replayable SSE."""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from market_prices.curation import SOURCE_URL
from market_prices.import_records import MAX_UPLOAD_BYTES
from market_prices.import_service import confirm_preview, create_preview
from market_prices.job_store import JobStore, public_job
from market_prices.warehouse import DataError

router = APIRouter(prefix="/api/v1", tags=["数据管理与流处理"])
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class ConfirmImport(BaseModel):
    preview_id: Identifier


@router.post("/imports/preview")
async def preview_upload(
    file: Annotated[UploadFile, File()],
    source_url: Annotated[str, Form(max_length=2048)] = SOURCE_URL,
    sheet_name: Annotated[str | None, Form(max_length=80)] = None,
):
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        return await asyncio.to_thread(
            create_preview, content, file.filename or "upload.csv", source_url, sheet_name
        )
    finally:
        await file.close()


@router.post("/imports", status_code=202)
def confirm_import(body: ConfirmImport):
    return public_job(confirm_preview(body.preview_id))


@router.get("/imports/service")
def import_service_status():
    store = JobStore()
    with store.connection(True) as db:
        path = store.root / "warehouse/current.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        last = db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
    return {
        "worker": store.worker_status(),
        "current_run_id": current.get("run_id"),
        "last_event_id": last,
    }


@router.get("/jobs")
def list_jobs(limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return {"items": [public_job(job) for job in JobStore().jobs(limit)]}


@router.get("/jobs/{job_id}")
def get_job(job_id: Identifier):
    return public_job(JobStore().job(job_id))


@router.get("/stream/events")
async def stream_events(
    request: Request,
    after: Annotated[int, Query(ge=0, le=9_000_000_000_000_000)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
):
    if last_event_id is not None:
        if not last_event_id.isdigit() or len(last_event_id) > 16:
            raise DataError(422, "invalid_event_id", "Last-Event-ID必须是非负事件序号")
        after = max(after, int(last_event_id))
    store = JobStore()

    async def generate():
        cursor = after
        idle = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await asyncio.to_thread(store.events, cursor)
            for event in events:
                cursor = event["id"]
                data = json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"))
                yield f"id: {cursor}\nevent: {event['event']}\ndata: {data}\n\n"
            idle = 0 if events else idle + 1
            if idle >= 30:
                yield ": heartbeat\n\n"
                idle = 0
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
