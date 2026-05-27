import os
import json
import logging
import httpx

logger = logging.getLogger("liquidtime.nim")

NIM_BASE_URL   = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL      = os.getenv("NIM_MODEL", "openai/gpt-oss-120b")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

# Shared JSON Schema for a single schedule block.
BLOCK_ITEM_SCHEMA = {
    "type": "object",
    "required": ["id", "title", "start", "end", "startISO", "endISO", "durationMin", "category"],
    "properties": {
        "id":          {"type": "string", "description": "Existing block id e.g. 'b1'. For a brand-new block you are adding, use the literal string \"NEW\" — never invent your own id."},
        "title":       {"type": "string"},
        "start":       {"type": "string", "description": "Display time like '9:00 AM'"},
        "end":         {"type": "string", "description": "Display time like '10:30 AM'"},
        "startISO":    {"type": "string", "description": "ISO local datetime like '2026-05-20T09:00:00'"},
        "endISO":      {"type": "string", "description": "ISO local datetime like '2026-05-20T10:30:00'"},
        "durationMin": {"type": "integer"},
        "category":    {"type": "string", "enum": ["class", "gym", "food", "work"]},
        "hint":        {"type": "string", "description": "Brief note about the block — why it moved, what it is, or any context"},
        "changed":     {"type": "boolean", "description": "true if this block's time was mutated"},
    },
}

# ── Voice command (full CRUD) ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are LiquidTime's scheduling engine. You receive a user's current daily schedule (as JSON) and a voice command, and you output the full updated schedule via the update_schedule function.

The user may ask you to MOVE, ADD, DELETE, or RENAME blocks. All blocks are freely reschedulable.

RULES:
1. All blocks can be freely rescheduled to honor the user's intent.
2. Blocks must NOT overlap. If blocks conflict, push the later block forward in time.
3. Preserve durationMin exactly — never shorten or lengthen a block unless the user explicitly requests it.
4. ADD: when the user asks to add a block, include a new block in the output with id set to the literal string "NEW". Never invent an id. Infer category and durationMin from the user's words. If unspecified, default to category 'work', durationMin 60.
5. DELETE: when the user asks to remove a block, simply omit it from the returned array.
6. RENAME: change only the title field of the targeted block.
7. Otherwise return ALL blocks from the input, even unchanged ones.
8. Set changed: true only on blocks whose startISO or endISO changed (or newly added blocks).
9. Update the hint field on moved/added blocks to briefly explain the change (e.g. 'moved up', 'added by voice').
10. Keep all ISO timestamps on the same calendar date as the input. Do not schedule past midnight.
11. Display time fields (start, end) must exactly match startISO/endISO in 12-hour format with AM/PM and no leading zero (e.g. '9:00 AM', '12:30 PM').
12. Respond ONLY via the update_schedule function call — no plain text outside the function.

CONFLICT RESOLUTION:
- When a change causes a conflict, push blocks to the earliest available gap that satisfies the user's intent.
- A gap is valid if (gap_end - gap_start) >= block.durationMin.
- Meal prep blocks (category: food) should stay after gym blocks when possible.
- Blocks that have already started or finished (before current_time) should not be moved unless explicitly requested."""

SCHEDULE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_schedule",
        "description": (
            "Emit the complete, updated schedule for the day after processing the user's voice command. "
            "Return every block that should exist after the change (omit deleted blocks, include added ones with id \"NEW\")."
        ),
        "parameters": {
            "type": "object",
            "required": ["schedule", "message"],
            "properties": {
                "schedule": {
                    "type": "array",
                    "description": "Complete ordered list of schedule blocks for today after the change.",
                    "items": BLOCK_ITEM_SCHEMA,
                },
                "message": {
                    "type": "string",
                    "description": "Short human-readable summary of what changed, e.g. 'Shifted lab +1h, moved powerlifting earlier.'",
                },
            },
        },
    },
}

# ── Generation from scratch ───────────────────────────────────────────────
GENERATE_SYSTEM_PROMPT = """You are LiquidTime's schedule designer. The user describes the day they want, and you produce a complete, ordered, non-overlapping daily schedule via the create_schedule function.

RULES:
1. Lay out a realistic day within waking hours (roughly 7:00 AM to 11:00 PM).
2. Blocks must NOT overlap. Order them chronologically.
3. Choose sensible durationMin for each block based on the user's description.
4. category must be one of: 'class', 'gym', 'food', 'work'. Use 'class' for lectures/labs/study, 'gym' for workouts, 'food' for meals, 'work' for jobs/projects/personal work.
5. Set id to the literal string "NEW" for EVERY block — never invent ids.
6. All startISO/endISO must be on the given target date. Do not schedule past midnight.
7. Display time fields (start, end) must exactly match startISO/endISO in 12-hour format with AM/PM and no leading zero (e.g. '9:00 AM', '12:30 PM').
8. Add a short hint on each block describing the activity or context.
9. Respond ONLY via the create_schedule function call — no plain text outside the function."""

GENERATE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_schedule",
        "description": "Emit a complete daily schedule built from the user's natural-language description.",
        "parameters": {
            "type": "object",
            "required": ["schedule", "message"],
            "properties": {
                "schedule": {
                    "type": "array",
                    "description": "Complete ordered list of schedule blocks for the target date.",
                    "items": BLOCK_ITEM_SCHEMA,
                },
                "message": {
                    "type": "string",
                    "description": "Short friendly summary of the schedule you built.",
                },
            },
        },
    },
}


async def _post_nim(payload: dict) -> dict:
    """POST a chat-completions request to NVIDIA NIM and return the parsed JSON body."""
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.post(
            f"{NIM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {NVIDIA_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code != 200:
            logger.error("NIM HTTP %s — %s", resp.status_code, resp.text[:600])
        resp.raise_for_status()

    data = resp.json()
    if "choices" not in data or not data["choices"]:
        logger.error("NIM unexpected response body: %s", json.dumps(data)[:800])
        raise ValueError(f"NIM response missing 'choices': {data}")
    return data


def _extract_call(data: dict, fn_name: str) -> tuple[list[dict], str]:
    """Pull (schedule, message) from a NIM response — tool_calls first, JSON content fallback."""
    message = data["choices"][0]["message"]

    if message.get("tool_calls"):
        raw_args = message["tool_calls"][0]["function"]["arguments"]
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            logger.error("tool_call arguments not valid JSON (%s): %s", fn_name, raw_args[:400])
            raise ValueError(f"NIM tool_call JSON decode error: {exc}") from exc
        if "schedule" not in args:
            logger.error("tool_call missing 'schedule' key (%s): %s", fn_name, raw_args[:400])
            raise ValueError(f"NIM tool_call has no 'schedule' field ({fn_name})")
        return args["schedule"], args.get("message", "Schedule updated.")

    content = message.get("content", "")
    finish_reason = data["choices"][0].get("finish_reason", "?")
    logger.warning("── AI REPLY (no tool_call; finish=%s) ─────────────────────", finish_reason)
    logger.warning("Raw content: %s", content[:600])
    try:
        start = content.index("{")
        end   = content.rindex("}") + 1
        args  = json.loads(content[start:end])
        if "schedule" in args:
            return args["schedule"], args.get("message", "Schedule updated.")
    except (ValueError, KeyError, json.JSONDecodeError):
        pass

    raise ValueError(
        f"NIM gave no tool_call and no parseable JSON ({fn_name}). "
        f"finish_reason={finish_reason!r}. content={content[:300]!r}"
    )


async def call_nim(
    transcript: str,
    current_schedule: list[dict],
    current_time_context: str,
) -> tuple[list[dict], str]:
    """
    Call NVIDIA NIM with function calling to process a voice command.
    Returns (updated_blocks, summary_message).
    """
    logger.info("── VOICE COMMAND ──────────────────────────────")
    logger.info("Transcript : %s", transcript)
    logger.info("Time ctx   : %s", current_time_context)
    logger.info("Model      : %s", NIM_MODEL)

    user_message = (
        f"Current time: {current_time_context}\n\n"
        f"Current schedule (JSON):\n{json.dumps(current_schedule, indent=2)}\n\n"
        f'Voice command: "{transcript}"\n\n'
        "Apply the voice command and call update_schedule with the full updated schedule."
    )

    payload = {
        "model": NIM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "tools": [SCHEDULE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "update_schedule"}},
        "temperature": 0.1,
        "max_tokens": 8192,
    }

    data = await _post_nim(payload)
    schedule, message = _extract_call(data, "update_schedule")
    logger.info("── AI REPLY ───────────────────────────────────")
    logger.info("Message    : %s", message)
    logger.info("Blocks     : %d returned", len(schedule))
    return schedule, message


async def generate_schedule(
    description: str,
    target_date: str,
    user_name: str | None,
) -> tuple[list[dict], str]:
    """
    Call NVIDIA NIM to build a fresh schedule from a natural-language description.
    Returns (blocks, summary_message).
    """
    logger.info("── GENERATE SCHEDULE ──────────────────────────")
    logger.info("Description: %s", description)
    logger.info("Target date: %s", target_date)
    logger.info("Model      : %s", NIM_MODEL)

    who = f" for {user_name}" if user_name else ""
    user_message = (
        f"Target date: {target_date}\n\n"
        f"Build a daily schedule{who} based on this description:\n"
        f'"{description}"\n\n'
        "Call create_schedule with the complete schedule."
    )

    payload = {
        "model": NIM_MODEL,
        "messages": [
            {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "tools": [GENERATE_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "create_schedule"}},
        "temperature": 0.3,
        "max_tokens": 8192,
    }

    data = await _post_nim(payload)
    schedule, message = _extract_call(data, "create_schedule")
    logger.info("── AI REPLY ───────────────────────────────────")
    logger.info("Message    : %s", message)
    logger.info("Blocks     : %d generated", len(schedule))
    return schedule, message
