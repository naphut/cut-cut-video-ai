"""
Enterprise-Grade Gemini Multimodal Audio AI Service.
Implements the 5-Step Video Dubbing Pipeline:
1. Extract & Normalize Audio (24kHz Mono, 96kbps, Vocal Enhance Filter)
2. Smart Audio Chunking (120s chunks with 5s overlap)
3. Gemini Flash Multimodal 3-in-1 (Time, Spoken Khmer Translation, Speaker/Gender)
4. Merge & Deduplicate Overlapping Segments
5. Standard SRT Generation & Subtitle Segment Parsing
"""

import base64
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

import requests
from utils.config_manager import get_gemini_api_key, get_gemini_api_keys
from utils.file_utils import get_temp_path
from utils.logger import logger
from utils.gemini_parser import parse_gemini_json_response, timestamp_to_seconds, seconds_to_srt_time


class GeminiAudioService:
    def __init__(self, api_key: Optional[str] = None):
        if api_key:
            self.api_keys = [k.strip() for k in re.split(r"[\n,;\r]+", api_key) if k.strip()]
        else:
            self.api_keys = get_gemini_api_keys()

        self.api_key = self.api_keys[0] if self.api_keys else ""
        self._key_index = 0
        self.models = [
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.5-flash-lite"
        ]
        self._active_model: Optional[str] = None

    def get_current_key(self) -> str:
        if not self.api_keys:
            return self.api_key or ""
        return self.api_keys[self._key_index % len(self.api_keys)]

    def rotate_key(self) -> str:
        """Rotate to next API key in pool (for load balancing between multiple accounts)."""
        if not self.api_keys or len(self.api_keys) <= 1:
            return self.get_current_key()
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        logger.info(f"🔄 [Key Pool] Rotated to Account Key #{self._key_index + 1}/{len(self.api_keys)}")
        return self.get_current_key()

    def _get_supported_models(self, api_key: str = "") -> List[str]:
        """Return strictly curated ultra-fast multimodal speech models."""
        curated_priority = [
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.5-flash-lite"
        ]
        if self._active_model and self._active_model in curated_priority:
            curated_priority.remove(self._active_model)
            curated_priority.insert(0, self._active_model)
        return list(curated_priority)

    # ==================== STEP 1: EXTRACT & NORMALIZE AUDIO ====================
    def extract_and_normalize_audio(self, video_path: str, output_mp3_path: Optional[str] = None) -> Tuple[str, float]:
        """
        Extract vocal audio from video with studio parameters:
        - Sample Rate: 24000Hz (24kHz Mono)
        - Bitrate: 128 kbps
        - Vocal Filter: High vocal clarity, whisper capture, background noise reduction
        Returns (mp3_path, duration_seconds)
        """
        if not video_path or not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        if not output_mp3_path:
            clean_name = Path(video_path).stem
            output_mp3_path = get_temp_path(f"{clean_name}_vocal_24k.mp3")

        logger.info(f"🎙️ [Step 1] Extracting & Normalizing audio (24kHz Mono, 128k, Vocal Boost)...")
        # FFmpeg command with high fidelity vocal boost & speech preservation
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vn",
            "-ar", "24000",
            "-ac", "1",
            "-b:a", "128k",
            "-af", "highpass=f=60,lowpass=f=9000,volume=1.3,dynaudnorm=f=120:g=15",
            output_mp3_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not os.path.exists(output_mp3_path) or os.path.getsize(output_mp3_path) == 0:
            fallback_cmd = [
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-ar", "24000", "-ac", "1", "-b:a", "128k",
                output_mp3_path
            ]
            subprocess.run(fallback_cmd, capture_output=True, check=False)

        duration = self.get_audio_duration(output_mp3_path)
        logger.info(f"✅ [Step 1] Audio extracted: {output_mp3_path} ({duration:.2f}s, {os.path.getsize(output_mp3_path)/1024:.1f} KB)")
        return output_mp3_path, duration

    def get_audio_duration(self, audio_path: str) -> float:
        """Measure exact audio duration in seconds using ffprobe."""
        try:
            cmd = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception as e:
            logger.warning(f"ffprobe duration error: {e}")
        return 0.0

    # ==================== STEP 2: SMART AUDIO CHUNKING ====================
    def chunk_audio(
        self,
        audio_path: str,
        total_duration: float,
        chunk_duration: float = 150.0,
        overlap: float = 5.0
    ) -> List[Dict]:
        """
        Split audio into chunks:
        - If <= 180s (3 minutes): 1 single fast chunk (processed in ~4s).
        - If > 180s: 150s chunks with 5s overlap processed in parallel.
        Returns list of dicts: [{"path": chunk_path, "offset": start_offset, "duration": dur, "index": i}]
        """
        chunks = []
        if total_duration <= 180.0:
            logger.info(f"⚡ [Step 2] Audio duration {total_duration:.1f}s <= 180s: Processing as 1 single fast chunk.")
            chunks.append({
                "path": audio_path,
                "offset": 0.0,
                "duration": total_duration,
                "index": 0
            })
            return chunks

        step = chunk_duration - overlap  # e.g. 145.0 seconds
        num_chunks = int(math.ceil(total_duration / step))
        logger.info(f"✂️ [Step 2] Chunking audio into {num_chunks} parts (150s each, {overlap}s overlap)...")

        for i in range(num_chunks):
            start_sec = i * step
            if start_sec >= total_duration:
                break
            dur = min(chunk_duration, total_duration - start_sec)
            chunk_file = get_temp_path(f"gemini_chunk_{i:03d}_{int(start_sec)}s.mp3")

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-t", str(dur),
                "-i", audio_path,
                "-c", "copy",
                chunk_file
            ]
            subprocess.run(cmd, capture_output=True, check=False)
            active_path = chunk_file if (os.path.exists(chunk_file) and os.path.getsize(chunk_file) > 100) else audio_path
            chunks.append({
                "path": active_path,
                "offset": start_sec,
                "duration": dur,
                "index": i
            })

        return chunks

    # ==================== STEP 3: GEMINI FLASH 3-IN-1 PROCESSING ====================
    def process_chunk_with_gemini(
        self,
        chunk_path: str,
        api_key: Optional[str] = None,
        part_index: int = 1,
        total_parts: int = 1
    ) -> List[Dict]:
        """
        Send chunk to Google Gemini Flash with multi-tier error separation:
        - HTTP/Network/Quota errors -> Rotate API Key / Model
        - JSON Syntax / Parsing errors -> Repair & Retry SAME chunk with strict prompt (DO NOT rotate key)
        """
        with open(chunk_path, "rb") as f:
            audio_bytes = f.read()
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        prompt = (
            "You are an elite multilingual video dubbing director and Khmer localization master.\n"
            "Listen carefully to this entire audio track and perform 3 tasks simultaneously:\n"
            "1. Accurately detect 100% of all spoken dialogue, sentences, whispers, and background voice lines from start to finish without skipping or missing anything.\n"
            "2. Identify exact startTime and endTime in standard format (e.g. '00:00:01,200'). Ensure timestamps align closely with the spoken words.\n"
            "3. Identify the speaker and gender ('male', 'female', 'child', 'elder').\n"
            "4. Translate every dialogue into natural, conversational, spoken Khmer (ភាសានិយាយបែបធម្មជាតិ ពិរោះ រលូន សមស្របនឹងការបញ្ចូលសម្លេង Dubbing) fitting the dialogue pacing.\n\n"
            "CRITICAL: Output strictly a valid JSON array of objects with the exact schema:\n"
            "[\n"
            "  {\n"
            "    \"startTime\": \"00:00:01,200\",\n"
            "    \"endTime\": \"00:00:04,500\",\n"
            "    \"sourceText\": \"Original spoken sentence\",\n"
            "    \"translatedText\": \"អត្ថបទនិយាយជាភាសាខ្មែរ\",\n"
            "    \"speaker\": \"Speaker 1\",\n"
            "    \"gender\": \"male\"\n"
            "  }\n"
            "]\n"
            "Do NOT include markdown formatting or backticks. Return ONLY the raw JSON array."
        )

        strict_suffix = (
            "\n\nCRITICAL FORMAT ENFORCEMENT:\n"
            "Return ONLY one valid JSON array.\n"
            "Do not use Markdown.\n"
            "Do not use ```json.\n"
            "Do not include explanations.\n"
            "Do not include comments.\n"
            "Use double quotes for all JSON keys and string values.\n"
            "Return exactly one JSON array.\n"
            "Do not output any text before or after the JSON."
        )

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/mp3",
                            "data": audio_b64
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        }

        # Build candidate key pool
        pool = list(self.api_keys) if self.api_keys else []
        if api_key:
            if api_key in pool:
                pool.remove(api_key)
            pool.insert(0, api_key)
        else:
            pool.sort(key=lambda k: 0 if k.startswith("AIzaSy") else 1)

        if not pool:
            default_k = get_gemini_api_key()
            if default_k:
                pool.append(default_k)

        if not pool:
            raise ValueError("No Gemini API Key available. Please configure your API key.")

        models_to_try = self._get_supported_models("")
        last_error = ""

        part_label = f"Part {part_index}/{total_parts}" if total_parts > 1 else "Audio"

        for model in models_to_try:
            model_unavailable = False
            for key_idx, current_api_key in enumerate(pool):
                if model_unavailable:
                    break
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={current_api_key}"
                # Safe logging without exposing API key
                k_label = f"Key #{key_idx + 1}"

                # JSON Format Retry Loop on SAME Key & Model (Up to 3 attempts)
                max_json_retries = 3
                for attempt in range(max_json_retries):
                    active_prompt = prompt if attempt == 0 else (prompt + strict_suffix)
                    payload["contents"][0]["parts"][1]["text"] = active_prompt

                    try:
                        logger.info(f"🌐 [Step 3] Calling Gemini AI ({model}) for {part_label} with {k_label} (Attempt {attempt + 1}/{max_json_retries})...")
                        resp = requests.post(url, headers=headers, json=payload, timeout=75)

                        if resp.status_code == 200:
                            data = resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                text_res = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                                parsed = parse_gemini_json_response(
                                    text_res,
                                    part_num=part_index,
                                    attempt_num=attempt + 1,
                                    model=model,
                                    total_parts=total_parts
                                )
                                if parsed:
                                    self._active_model = model
                                    if current_api_key in self.api_keys:
                                        self._key_index = self.api_keys.index(current_api_key)
                                    logger.info(f"✅ [Step 3] Gemini ({model}) returned {len(parsed)} segments for {part_label} using {k_label}.")
                                    return parsed
                                else:
                                    # JSON was malformed! Do NOT rotate API key! Retry SAME chunk with strict prompt.
                                    logger.warning(f"⚠️ [Step 3] Malformed JSON received for {part_label} from {model} with {k_label}. Retrying SAME chunk with strict prompt...")
                                    continue
                            else:
                                logger.warning(f"⚠️ [Step 3] Empty candidates received for {part_label} from {model} with {k_label}.")
                                continue

                        # ==================== HTTP / API ERROR CLASSIFICATION ====================
                        elif resp.status_code in (503, 404):
                            logger.warning(f"⚠️ {model} returned HTTP {resp.status_code}. Waiting 2s before retry or fallback...")
                            time.sleep(2)
                            last_error = f"{model} HTTP {resp.status_code}"
                            model_unavailable = True
                            break
                        elif resp.status_code == 429:
                            logger.warning(f"⚠️ {k_label} rate limit reached (HTTP 429). Rotating key...")
                            time.sleep(2)
                            last_error = f"{k_label} HTTP 429 Rate Limit"
                            break
                        elif resp.status_code in (401, 403):
                            err_msg = ""
                            try:
                                err_msg = resp.json().get("error", {}).get("message", "")
                            except Exception:
                                pass
                            logger.warning(f"⚠️ {k_label} returned HTTP {resp.status_code}: {err_msg[:60]}. Rotating key...")
                            last_error = f"{k_label} HTTP {resp.status_code}"
                            break
                        else:
                            logger.warning(f"⚠️ {k_label} {model} returned HTTP {resp.status_code}. Rotating key...")
                            last_error = f"{k_label} {model} HTTP {resp.status_code}"
                            break

                    except requests.exceptions.Timeout:
                        logger.warning(f"⚠️ {k_label} ({model}) request timed out after 75s. Rotating key...")
                        last_error = f"{k_label} {model} Timeout"
                        break
                    except Exception as e:
                        logger.warning(f"⚠️ {k_label} ({model}) network exception: {e}. Rotating key...")
                        last_error = f"{model}: {e}"
                        break

        raise RuntimeError(f"Gemini AI Speech processing failed: {last_error}")

    def _parse_json_response(self, raw_text: str) -> List[Dict]:
        """Backward-compatible wrapper routing to robust parser."""
        return parse_gemini_json_response(raw_text)

    # ==================== STEP 4: MERGE & DEDUPLICATE ====================
    def merge_and_deduplicate(
        self,
        chunk_results: List[Any],
        overlap: float = 5.0,
        total_movie_duration: Optional[float] = None
    ) -> List[Dict]:
        """
        Merge results from chunks:
        1. Add chunk startOffset to segment timestamps.
        2. Strictly clamp timestamps within chunk duration and movie boundaries (zero overrun past EOF).
        3. Globally sort all segments by start time.
        4. Deduplicate overlapping segments from the 5-second overlap window using multi-segment lookback.
        5. Smooth subtitle collisions (end <= next_start).
        6. Format Khmer speaker tag ([ប្រុស], [ស្រី], [ក្មេង], [ចាស់]).
        """
        logger.info(f"🔄 [Step 4] Merging and deduplicating chunks...")
        all_raw = []
        for entry in chunk_results:
            if not entry:
                continue
            if len(entry) >= 3:
                offset, ch_dur, segs = entry[0], entry[1], entry[2]
            else:
                offset, segs = entry[0], entry[1]
                ch_dur = 150.0

            if not segs:
                continue

            for s in segs:
                rel_st = float(s.get("start", 0.0))
                rel_et = float(s.get("end", rel_st + 1.0))

                # Discard hallucinations that exceed chunk duration
                if ch_dur and rel_st >= ch_dur - 0.15:
                    continue

                if ch_dur and rel_et > ch_dur:
                    rel_et = ch_dur

                if rel_et <= rel_st:
                    rel_et = rel_st + 0.50

                abs_st = round(rel_st + offset, 2)
                abs_et = round(rel_et + offset, 2)

                # Discard segments past total video duration
                if total_movie_duration and abs_st >= total_movie_duration:
                    continue
                if total_movie_duration and abs_et > total_movie_duration:
                    abs_et = round(total_movie_duration, 2)

                all_raw.append({
                    "start": abs_st,
                    "end": abs_et,
                    "sourceText": s.get("sourceText", "").strip(),
                    "translatedText": s.get("translatedText", "").strip(),
                    "speaker": s.get("speaker", "Speaker 1"),
                    "gender": s.get("gender", "male").lower()
                })

        # 1. Sort all segments globally by start time
        all_raw.sort(key=lambda x: (x["start"], x["end"]))

        # 2. Deduplicate overlapping segments at boundaries
        gender_to_tag = {
            "male": "[ប្រុស]",
            "female": "[ស្រី]",
            "child": "[ក្មេង]",
            "elder": "[ចាស់ប្រុស]",
            "elder_male": "[ចាស់ប្រុស]",
            "elder_female": "[ចាស់ស្រី]"
        }

        all_segments = []
        duplicates_removed = 0

        for item in all_raw:
            abs_start = item["start"]
            abs_end = item["end"]
            khmer_text = item["translatedText"]
            orig_text = item["sourceText"]
            gender = (item.get("gender") or "male").lower().strip()
            speaker_tag = gender_to_tag.get(gender, "[ប្រុស]")

            clean_curr_khmer = re.sub(r"^\s*\[(ប្រុស|ស្រី|ក្មេង|ក្មេងប្រុស|ក្មេងស្រី|ចាស់|ចាស់ប្រុស|ចាស់ស្រី)\]\s*", "", khmer_text).strip()
            clean_curr_orig = orig_text.strip()

            is_duplicate = False
            # Check against up to 3 recent segments for boundary overlap duplication
            for prev_seg in reversed(all_segments[-3:]):
                clean_prev_khmer = re.sub(r"^\s*\[(ប្រុស|ស្រី|ក្មេង|ក្មេងប្រុស|ក្មេងស្រី|ចាស់|ចាស់ប្រុស|ចាស់ស្រី)\]\s*", "", prev_seg["khmer_text"]).strip()
                clean_prev_orig = prev_seg.get("original_text", "").strip()

                # Exact match in either Khmer or source Chinese
                if (clean_curr_khmer and clean_prev_khmer and clean_curr_khmer == clean_prev_khmer) or \
                   (clean_curr_orig and clean_prev_orig and clean_curr_orig == clean_prev_orig):
                    is_duplicate = True
                    break

                # Boundary overlap proximity (< 3.0s start delta)
                if abs(abs_start - prev_seg["start"]) < 3.0:
                    # Substring containment
                    if clean_curr_orig and clean_prev_orig and len(clean_curr_orig) >= 4 and len(clean_prev_orig) >= 4:
                        if clean_curr_orig in clean_prev_orig or clean_prev_orig in clean_curr_orig:
                            is_duplicate = True
                            break
                    if clean_curr_khmer and clean_prev_khmer and len(clean_curr_khmer) >= 10 and len(clean_prev_khmer) >= 10:
                        if clean_curr_khmer in clean_prev_khmer or clean_prev_khmer in clean_curr_khmer:
                            is_duplicate = True
                            break

            if is_duplicate:
                duplicates_removed += 1
                continue

            # Determine Persona & Voice for the 5 categories
            if gender == "child" or "[ក្មេង]" in speaker_tag:
                persona_val = "Boy / Child"
                voice_val = "Khmer Child - Boy (Vannak)"
            elif gender == "elder_female" or "[ចាស់ស្រី]" in speaker_tag:
                persona_val = "Elderly Female"
                voice_val = "Khmer Elder - Female (Grandmother)"
            elif gender in ("elder", "elder_male") or "[ចាស់ប្រុស]" in speaker_tag or "[ចាស់]" in speaker_tag:
                persona_val = "Elderly Male"
                voice_val = "Khmer Elder - Male (Grandfather)"
            elif gender == "female" or "[ស្រី]" in speaker_tag:
                persona_val = "Female Adult"
                voice_val = "Khmer Female - Sreymom"
            else:
                persona_val = "Male Adult"
                voice_val = "Khmer Male - Piseth"

            tagged_khmer = f"{speaker_tag} {clean_curr_khmer}"
            seg_id = f"sub_{len(all_segments) + 1:04d}"
            all_segments.append({
                "id": seg_id,
                "start": abs_start,
                "end": abs_end,
                "character": item.get("speaker", "Speaker 1"),
                "gender": gender,
                "speaker_tag": speaker_tag,
                "persona": persona_val,
                "voice": voice_val,
                "voice_id": voice_val,
                "original_text": orig_text,
                "khmer_text": tagged_khmer,
                "text": orig_text,
                "startTime": seconds_to_srt_time(abs_start),
                "endTime": seconds_to_srt_time(abs_end)
            })

        # 3. Anti-collision smoothing: eliminate stacking overlaps
        for i in range(len(all_segments) - 1):
            curr = all_segments[i]
            nxt = all_segments[i + 1]
            if curr["end"] > nxt["start"] - 0.05:
                curr["end"] = max(curr["start"] + 0.30, round(nxt["start"] - 0.05, 2))
                curr["endTime"] = seconds_to_srt_time(curr["end"])

        logger.info(f"📊 [STT] chunks={len(chunk_results)} segments_raw={len(all_raw)} segments_final={len(all_segments)} duplicates_removed={duplicates_removed}")
        logger.info(f"✅ [Step 4] Merge complete: Total {len(all_segments)} clean Khmer segments (0 overlaps, bounds enforced).")
        return all_segments

    # ==================== STEP 5: SEGMENTS TO SRT ====================
    def segments_to_srt(self, segments: List[Dict], srt_output_path: Optional[str] = None) -> str:
        """
        Convert segments into standard SubRip (.srt) format:
        1
        00:00:01,200 --> 00:00:04,500
        [ប្រុស] ជំរាបសួរអ្នកទាំងអស់គ្នា...
        """
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            st = seconds_to_srt_time(seg["start"])
            et = seconds_to_srt_time(seg["end"])
            txt = seg["khmer_text"]
            srt_lines.append(f"{i}\n{st} --> {et}\n{txt}\n")

        srt_content = "\n".join(srt_lines)

        if srt_output_path:
            with open(srt_output_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
            logger.info(f"📄 [Step 5] SRT written to: {srt_output_path}")

        return srt_content

    # ==================== MASTER ORCHESTRATION PIPELINE ====================
    def process_video_pipeline(
        self,
        video_path: str,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        srt_output_path: Optional[str] = None
    ) -> Tuple[List[Dict], str]:
        """
        Execute full 5-step pipeline end-to-end:
        Extract 24kHz -> Chunk 120s -> Gemini Flash -> Merge/Deduplicate -> Generate SRT.
        """
        api_key = self.api_key or get_gemini_api_key()
        if not api_key:
            raise ValueError("Google Gemini API Key is required! Please configure your Gemini API Key.")

        # 1. Extract & Normalize Audio
        if progress_callback:
            progress_callback(10, "🎙️ [1/5] Extracting audio (24kHz Mono, 96kbps, Vocal Clean)...")
        mp3_path, duration = self.extract_and_normalize_audio(video_path)

        # 2. Smart Chunking
        if progress_callback:
            progress_callback(25, f"✂️ [2/5] Smart audio chunking ({duration:.1f}s)...")
        chunks = self.chunk_audio(mp3_path, duration, chunk_duration=150.0, overlap=5.0)

        # 3. Gemini Flash Processing per chunk (Parallel if multi-chunk & multi-key)
        chunk_results = []
        if len(chunks) == 1:
            active_k = self.get_current_key()
            if progress_callback:
                progress_callback(45, "⚡ [3/5] Gemini AI analyzing audio (Ultra-fast Flash-Lite)...")
            res = self.process_chunk_with_gemini(
                chunks[0]["path"],
                api_key=active_k,
                part_index=1,
                total_parts=1
            )
            chunk_results.append((chunks[0]["offset"], chunks[0]["duration"], res))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            num_workers = min(len(chunks), max(2, len(self.api_keys)))
            logger.info(f"⚡ Processing {len(chunks)} chunks concurrently with {num_workers} parallel workers...")

            def _worker_task(item):
                idx, ch = item
                assigned_key = self.api_keys[idx % len(self.api_keys)] if self.api_keys else self.api_key
                part_res = self.process_chunk_with_gemini(
                    ch["path"],
                    api_key=assigned_key,
                    part_index=idx + 1,
                    total_parts=len(chunks)
                )
                return idx, ch["offset"], ch.get("duration", 150.0), part_res

            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                futures = {executor.submit(_worker_task, (i, ch)): i for i, ch in enumerate(chunks)}
                completed_count = 0
                temp_results = [None] * len(chunks)
                for fut in as_completed(futures):
                    idx, offset, ch_dur, part_res = fut.result()
                    temp_results[idx] = (offset, ch_dur, part_res)
                    completed_count += 1
                    pct = int(30 + (completed_count / len(chunks)) * 50)
                    if progress_callback:
                        progress_callback(pct, f"🧠 [3/5] Gemini AI completed Part {completed_count}/{len(chunks)}...")
                chunk_results = [r for r in temp_results if r is not None]

        # 4. Merge & Deduplicate
        if progress_callback:
            progress_callback(85, "🔄 [4/5] Merging and deduplicating Khmer segments...")
        merged_segments = self.merge_and_deduplicate(chunk_results, overlap=5.0, total_movie_duration=duration)

        # 5. Build SRT
        if progress_callback:
            progress_callback(95, "📄 [5/5] Generating standard SubRip (.srt)...")
        if not srt_output_path:
            clean_name = Path(video_path).stem
            srt_output_path = get_temp_path(f"{clean_name}_khmer.srt")

        srt_content = self.segments_to_srt(merged_segments, srt_output_path=srt_output_path)

        if progress_callback:
            progress_callback(100, f"✅ Gemini Pipeline Complete! {len(merged_segments)} segments.")

        return merged_segments, srt_content
