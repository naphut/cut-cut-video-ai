"""
Enterprise-Grade Gemini JSON Parser & Repair Engine.

Handles all real-world non-strict LLM JSON responses:
1. Pure JSON arrays
2. Markdown code blocks (```json ... ```)
3. Text before and after JSON (explanations, commentary)
4. Extra data (multiple JSON blocks, trailing junk) using JSONDecoder.raw_decode
5. Safe JSON syntax repairs:
   - Trailing commas before ] or }
   - Unquoted object keys ({startTime: ...})
   - Single-quoted keys and string values
   - Truncated closing brackets
6. Segment structure validation (numeric start/end, non-empty text, bounds sanity)
7. Raw response debug persistence (temp/gemini_debug/part_XXX_attempt_YY.txt)
8. Zero API key leakage
"""

import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from utils.file_utils import get_temp_path
from utils.logger import logger


def timestamp_to_seconds(ts: Any) -> float:
    """Convert '00:01:23,450' or '01:23,450' or numeric to float seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip().replace(",", ".")
    parts = s.split(":")
    try:
        if len(parts) == 3:
            p0, p1, p2 = float(parts[0]), float(parts[1]), float(parts[2])
            # If 3rd part is >= 60, it's milliseconds: e.g. "01:00:500" -> 1 min, 0 sec, 500 ms
            if p2 >= 60.0:
                return p0 * 60.0 + p1 + (p2 / 1000.0)
            # If p0 > 0 and (p0 * 3600 > 7200 or p0 <= 10) and p1 < 60:
            # Check if this is MM:SS:CS/MS (e.g., "01:29:00" = 1 min 29 sec)
            calc_hrs = p0 * 3600.0 + p1 * 60.0 + p2
            calc_mins = p0 * 60.0 + p1 + (p2 / 100.0 if p2 > 0 else 0.0)
            # In a chunk environment or standard subtitle timing, if calc_hrs > 3600 and p0 <= 59:
            # and p1 < 60, it's virtually always MM:SS:CS
            if p0 > 0 and p0 < 60 and calc_hrs > 3600:
                # If p0 is single digit or < 60 and p1 is seconds
                return calc_mins
            return calc_hrs
        elif len(parts) == 2:
            return float(parts[0]) * 60.0 + float(parts[1])
        else:
            return float(s)
    except Exception:
        return 0.0


def seconds_to_srt_time(seconds: float) -> str:
    """Convert seconds float to standard SRT timestamp format: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0.0
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis = 999
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def save_gemini_debug_response(
    raw_text: str,
    part_num: Optional[int] = None,
    attempt_num: Optional[int] = None,
    model: Optional[str] = None,
    error: Optional[Exception] = None
) -> str:
    """
    Save the exact raw Gemini response for debugging before modification.
    Never exposes API keys.
    """
    debug_dir = Path("temp/gemini_debug")
    debug_dir.mkdir(parents=True, exist_ok=True)

    part_str = f"part_{part_num:03d}" if part_num is not None else "part_unknown"
    att_str = f"attempt_{attempt_num:02d}" if attempt_num is not None else "attempt_01"
    filename = f"{part_str}_{att_str}.txt"
    filepath = debug_dir / filename

    header_lines = [
        f"=== GEMINI RAW RESPONSE DEBUG DUMP ===",
        f"Timestamp: {datetime.now().isoformat()}",
        f"Part: {part_num if part_num is not None else 'N/A'}",
        f"Attempt: {attempt_num if attempt_num is not None else 'N/A'}",
        f"Model: {model or 'unknown'}",
        f"Response Length: {len(raw_text)} chars",
        f"Error: {error or 'None'}",
        f"=======================================\n"
    ]

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(header_lines))
            f.write(raw_text)
        logger.info(f"💾 [Gemini Debug] Saved raw response dump to: {filepath}")
    except Exception as e:
        logger.warning(f"⚠️ [Gemini Debug] Could not save debug file: {e}")

    return str(filepath)


def validate_and_clean_segments(
    raw_items: List[Any],
    chunk_duration: float = 999999.0,
    part_num: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Validate that parsed JSON is a valid list of dialogue segments.
    Ensures:
    - Numeric start and end
    - end >= start (auto-healed if end <= start)
    - Non-empty spoken text
    - Preserves Khmer Unicode exactly
    - Discards corrupted segments without crashing the whole list
    """
    if not isinstance(raw_items, list):
        return []

    valid_segments = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue

        raw_st = item.get("startTime", item.get("start", 0.0))
        raw_et = item.get("endTime", item.get("end", None))

        st = timestamp_to_seconds(raw_st)
        if raw_et is not None:
            et = timestamp_to_seconds(raw_et)
        else:
            et = st + 2.0

        # Auto-heal invalid end time
        if et <= st:
            et = round(st + 1.5, 2)
        elif et - st > 30.0:
            # Overly long dialogue line sanity clamp
            et = round(st + 10.0, 2)

        src = str(item.get("sourceText", item.get("original_text", item.get("text", "")))).strip()
        trans = str(item.get("translatedText", item.get("translated_text", item.get("translation", item.get("khmer_text", src))))).strip()
        spk = str(item.get("speaker", item.get("character", "Speaker 1"))).strip()
        gender = str(item.get("gender", "male")).strip().lower()
        if gender not in ("male", "female", "child", "elder"):
            gender = "male"

        item_id = item.get("id")
        if item_id is None:
            item_id = f"seg_{idx+1:04d}"

        # Require at least some textual content
        if not src and not trans:
            continue

        valid_segments.append({
            "id": str(item_id),
            "start": round(st, 2),
            "end": round(et, 2),
            "sourceText": src or trans,
            "translatedText": trans or src,
            "translated_text": trans or src,
            "translation": trans or src,
            "original_text": src or trans,
            "speaker": spk or "Speaker 1",
            "gender": gender
        })

    return valid_segments


def parse_gemini_json_response(
    raw_text: str,
    part_num: Optional[int] = None,
    attempt_num: Optional[int] = None,
    model: Optional[str] = None,
    total_parts: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Parse Gemini JSON response with multi-stage recovery and raw_decode.
    Handles:
    - Normal JSON arrays
    - Markdown code fences
    - Preceding / trailing explanations
    - Extra data (multiple blocks) using JSONDecoder.raw_decode
    - Trailing commas
    - Unquoted property names
    - Single quotes
    - Truncated arrays
    """
    if not raw_text or not raw_text.strip():
        return []

    clean = raw_text.strip()

    # Step 1: Strip outer markdown code blocks if present
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean).strip()

    # Locate starting bracket
    start_bracket = clean.find('[')
    if start_bracket == -1:
        # Check if model returned a single object {...} instead of array
        start_brace = clean.find('{')
        if start_brace != -1:
            start_bracket = start_brace
        else:
            logger.warning("[Gemini JSON] No JSON array or object bracket found in response.")
            save_gemini_debug_response(raw_text, part_num, attempt_num, model, error=ValueError("No bracket found"))
            return []

    decoder = json.JSONDecoder()
    last_decode_err: Optional[json.JSONDecodeError] = None

    # ==================== STAGE 1: DIRECT RAW_DECODE ====================
    # Handles: Normal JSON, Surrounding text, and Extra Data!
    try:
        obj, end_idx = decoder.raw_decode(clean, idx=start_bracket)
        if isinstance(obj, dict):
            obj = [obj]
        if isinstance(obj, list):
            valid = validate_and_clean_segments(obj, part_num=part_num)
            if valid:
                return valid
    except json.JSONDecodeError as e:
        last_decode_err = e

    # ==================== STAGE 2: TRAILING COMMAS REPAIR ====================
    # Fixes `, ]` and `, }`
    repaired = clean[start_bracket:]
    repaired_trailing = re.sub(r',\s*([\]\}])', r'\1', repaired)
    try:
        obj, _ = decoder.raw_decode(repaired_trailing)
        if isinstance(obj, dict): obj = [obj]
        if isinstance(obj, list):
            valid = validate_and_clean_segments(obj, part_num=part_num)
            if valid:
                return valid
    except json.JSONDecodeError as e:
        last_decode_err = e

    # ==================== STAGE 3: UNQUOTED OBJECT KEYS REPAIR ====================
    # Fixes {startTime: -> {"startTime": or , startTime: -> , "startTime":
    repaired_unquoted = re.sub(r'([{\[,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', repaired_trailing)
    try:
        obj, _ = decoder.raw_decode(repaired_unquoted)
        if isinstance(obj, dict): obj = [obj]
        if isinstance(obj, list):
            valid = validate_and_clean_segments(obj, part_num=part_num)
            if valid:
                return valid
    except json.JSONDecodeError as e:
        last_decode_err = e

    # ==================== STAGE 4: SINGLE QUOTED KEYS / VALUES ====================
    # Fixes 'startTime': -> "startTime":
    repaired_single = re.sub(r"'([a-zA-Z0-9_]+)'\s*:", r'"\1":', repaired_unquoted)
    # Fixes simple single-quoted string values: : 'value' -> : "value"
    repaired_single = re.sub(r':\s*\'([^\'\\]*(?:\\.[^\'\\]*)*)\'', r': "\1"', repaired_single)
    try:
        obj, _ = decoder.raw_decode(repaired_single)
        if isinstance(obj, dict): obj = [obj]
        if isinstance(obj, list):
            valid = validate_and_clean_segments(obj, part_num=part_num)
            if valid:
                return valid
    except json.JSONDecodeError as e:
        last_decode_err = e

    # ==================== STAGE 5: AUTO-CLOSE TRUNCATED BRACKETS ====================
    open_brackets = repaired_single.count('[') - repaired_single.count(']')
    open_braces = repaired_single.count('{') - repaired_single.count('}')
    if open_brackets > 0 or open_braces > 0:
        patch = repaired_single
        # Trim dangling trailing comma or incomplete key
        patch = re.sub(r',\s*$', '', patch.strip())
        patch += ('}' * max(0, open_braces)) + (']' * max(0, open_brackets))
        try:
            obj, _ = decoder.raw_decode(patch)
            if isinstance(obj, dict): obj = [obj]
            if isinstance(obj, list):
                valid = validate_and_clean_segments(obj, part_num=part_num)
                if valid:
                    return valid
        except Exception:
            pass

    # ==================== STAGE 6: REGEX-BASED OBJECT EXTRACTION FALLBACK ====================
    # Extract individual valid {...} objects if outer array is corrupted
    extracted = []
    pattern = re.compile(
        r'\{[^{}]*?(?:"startTime"|"start"|"sourceText"|"original_text"|"translatedText"|"khmer_text"|startTime|start)[^{}]*?\}',
        re.DOTALL
    )
    for m in pattern.finditer(repaired_single):
        chunk_str = m.group(0)
        try:
            o = json.loads(chunk_str)
            if isinstance(o, dict):
                extracted.append(o)
        except Exception:
            try:
                fixed_o = re.sub(r',\s*\}', '}', chunk_str)
                o = json.loads(fixed_o)
                if isinstance(o, dict):
                    extracted.append(o)
            except Exception:
                pass

    if extracted:
        valid = validate_and_clean_segments(extracted, part_num=part_num)
        if valid:
            logger.info(f"🛠️ [Gemini JSON] Recovered {len(valid)} segments via individual object extractor.")
            return valid

    # ==================== FINAL: LOG & PERSIST RAW FAILURE ====================
    save_gemini_debug_response(raw_text, part_num, attempt_num, model, error=last_decode_err)

    part_display = f"{part_num}/{total_parts}" if (part_num is not None and total_parts is not None) else f"{part_num or 'N/A'}"
    err_line = last_decode_err.lineno if last_decode_err else "N/A"
    err_col = last_decode_err.colno if last_decode_err else "N/A"
    err_msg = last_decode_err.msg if last_decode_err else "Malformed JSON structure"

    logger.warning(
        f"[Gemini JSON] Parse failed\n"
        f"  Part: {part_display}\n"
        f"  Attempt: {attempt_num or 1}\n"
        f"  Model: {model or 'unknown'}\n"
        f"  Response length: {len(raw_text)}\n"
        f"  Error: {err_msg}\n"
        f"  Line: {err_line}\n"
        f"  Column: {err_col}"
    )

    return []
