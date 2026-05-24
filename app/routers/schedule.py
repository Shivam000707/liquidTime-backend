from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta

from app.models.schedule import (
    VoiceCommandRequest, VoiceCommandResponse,
    GenerateRequest, GenerateResponse, Block,
)
from app.services.nim import call_nim, generate_schedule

router = APIRouter(tags=["schedule"])


def fmt_display_time(dt: datetime) -> str:
    """Format datetime as '9:00 AM' — no leading zero, cross-platform."""
    s = dt.strftime("%I:%M %p")
    return s.lstrip("0") or "12:00 AM"


def assign_block_ids(raw_blocks: list[dict], existing_ids: set[str]) -> list[dict]:
    """
    Ensure every block has a unique, real id. Blocks whose id is 'NEW', missing,
    or a duplicate get a fresh id 'b{N}'. Mutates and returns the list.
    """
    seen: set[str] = set()
    used = set(existing_ids)

    def next_id() -> str:
        n = 1
        while f"b{n}" in used:
            n += 1
        used.add(f"b{n}")
        return f"b{n}"

    for b in raw_blocks:
        bid = b.get("id")
        if not bid or bid == "NEW" or bid in seen:
            bid = next_id()
            b["id"] = bid
        seen.add(bid)
        used.add(bid)
    return raw_blocks


def normalize_block(b: dict, target_date: str) -> dict:
    """
    Force the block onto target_date and recompute display strings from ISO,
    so model sloppiness on date/display fields is harmless.
    """
    for key in ("startISO", "endISO"):
        iso = b.get(key, "")
        if "T" in iso:
            b[key] = target_date + iso[iso.index("T"):]
    try:
        b["start"] = fmt_display_time(datetime.fromisoformat(b["startISO"]))
        b["end"]   = fmt_display_time(datetime.fromisoformat(b["endISO"]))
    except (ValueError, KeyError):
        pass
    return b


def resolve_conflicts(blocks: list[Block]) -> list[Block]:
    """
    Walk the schedule sorted by startISO. If a floating block overlaps the
    previous block's end, push it forward by its durationMin.
    Fixed blocks are never moved by this pass.
    """
    sorted_blocks = sorted(blocks, key=lambda b: b.startISO)
    result: list[Block] = []

    for block in sorted_blocks:
        if not result:
            result.append(block)
            continue

        prev = result[-1]
        prev_end   = datetime.fromisoformat(prev.endISO)
        curr_start = datetime.fromisoformat(block.startISO)

        if curr_start >= prev_end:
            result.append(block)
        elif block.type == "floating":
            new_start = prev_end
            new_end   = new_start + timedelta(minutes=block.durationMin)
            hint = (block.hint or "").rstrip()
            if hint and not hint.endswith("·"):
                hint += " · "
            hint += "auto-resolved overlap"
            result.append(block.model_copy(update={
                "startISO": new_start.isoformat(),
                "endISO":   new_end.isoformat(),
                "start":    fmt_display_time(new_start),
                "end":      fmt_display_time(new_end),
                "changed":  True,
                "hint":     hint,
            }))
        else:
            # Fixed block collision — accept as-is (NIM should not produce this)
            result.append(block)

    return result


@router.post(
    "/schedule/voice-command",
    response_model=VoiceCommandResponse,
    summary="Process a voice command and reflow the schedule",
)
async def voice_command(req: VoiceCommandRequest):
    try:
        datetime.fromisoformat(req.current_time_context)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="current_time_context must be a valid ISO datetime string like '2026-05-20T11:30:00'",
        )

    schedule_dicts = [b.model_dump(exclude_none=False) for b in req.current_schedule]

    try:
        new_schedule_raw, message = await call_nim(
            transcript=req.transcript,
            current_schedule=schedule_dicts,
            current_time_context=req.current_time_context,
        )
    except Exception as e:
        # Return the original schedule unchanged — client shows a friendly error banner.
        return VoiceCommandResponse(
            status="error",
            new_schedule=req.current_schedule,
            message=f"Couldn't process that command — try rephrasing. ({e})",
        )

    # Resolve 'NEW' sentinels and any duplicate ids the model emitted.
    existing_ids = {b.id for b in req.current_schedule}
    new_schedule_raw = assign_block_ids(new_schedule_raw, existing_ids)

    # Keep every block on today's date and recompute display strings.
    input_date = req.current_time_context[:10]
    new_schedule_raw = [normalize_block(b, input_date) for b in new_schedule_raw]

    try:
        new_blocks = [Block(**b) for b in new_schedule_raw]
    except Exception as e:
        return VoiceCommandResponse(
            status="error",
            new_schedule=req.current_schedule,
            message=f"AI returned an unreadable schedule — your schedule is unchanged. ({e})",
        )

    new_blocks = resolve_conflicts(new_blocks)

    return VoiceCommandResponse(
        status="ok",
        new_schedule=new_blocks,
        message=message,
    )


@router.post(
    "/schedule/generate",
    response_model=GenerateResponse,
    summary="Generate a fresh schedule from a natural-language description",
)
async def generate(req: GenerateRequest):
    try:
        datetime.fromisoformat(req.target_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="target_date must be a valid date string like '2026-05-20'",
        )

    try:
        raw_blocks, message = await generate_schedule(
            description=req.description,
            target_date=req.target_date,
            user_name=req.user_name,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI generation failed: {e}")

    raw_blocks = assign_block_ids(raw_blocks, set())
    raw_blocks = [normalize_block(b, req.target_date) for b in raw_blocks]

    try:
        blocks = [Block(**b) for b in raw_blocks]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI returned invalid schedule schema: {e}")

    blocks = resolve_conflicts(blocks)

    return GenerateResponse(status="ok", schedule=blocks, message=message)
