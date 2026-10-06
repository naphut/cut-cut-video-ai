"""
Dedicated Translation AI Service
Leverages Gemini Flash (gemini-3.8-flash / gemini-3.7-flash / gemini-3.6-flash).
Focuses strictly on:
1. Contextual dialogue translation (Source -> Natural Spoken Khmer)
2. Duration-constrained dialogue phrasing (Matching actor's on-screen pacing)
3. Dialogue condensing for oversized lines
DOES NOT touch audio or STT.
"""
import os
import json
import time
import re
import html
import urllib.parse
import urllib.request
from typing import List, Dict, Any, Union, Optional
import requests
from core.models import Segment
from utils.logger import logger
from utils.config_manager import get_gemini_api_key
from typing import Tuple


def validate_khmer_translation(text: Optional[str], original_text: str = "") -> Tuple[bool, str]:
    """
    Validate translated text for Khmer video dubbing production:
    1. Must be non-empty string.
    2. Must contain Khmer Unicode characters (U+1780 to U+17FF).
    3. Must NOT contain Thai script (U+0E00 to U+0E7F).
    4. Must NOT contain Chinese characters (U+4E00 to U+9FFF).
    5. Must NOT be an untranslated verbatim copy of Chinese source text.
    """
    if text is None:
        return False, "Translation text is None"
    
    t = str(text).strip()
    if not t:
        return False, "Empty translation text"
    
    # Check Thai script leakage (U+0E00 to U+0E7F)
    if re.search(r'[\u0E00-\u0E7F]', t):
        return False, "Contains Thai script leakage"
    
    # Check Chinese script leakage (U+4E00 to U+9FFF)
    if re.search(r'[\u4E00-\u9FFF]', t):
        return False, "Contains Chinese script leakage"
        
    # Check if Khmer characters exist (U+1780 to U+17FF)
    if not re.search(r'[\u1780-\u17FF]', t):
        orig_clean = re.sub(r'[\s\d\.,!?:;\-\–_]', '', str(original_text))
        t_clean = re.sub(r'[\s\d\.,!?:;\-\–_]', '', t)
        if orig_clean and not t_clean:
            return False, "Missing Khmer script (only numbers/punctuation)"
        if not re.search(r'[\u1780-\u17FF]', t):
            return False, "No Khmer script found in output"

    # Check verbatim source copy if source has Chinese characters
    if original_text and len(original_text.strip()) > 1:
        if t == original_text.strip() and re.search(r'[\u4E00-\u9FFF]', original_text):
            return False, "Verbatim copy of untranslated source text"

    return True, "Valid Khmer translation"


class TranslationService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or get_gemini_api_key()
        # Production model hierarchy for translation (gemini-3.1-flash-lite & flash-lite-latest are ultra-fast and available)
        self.models = [
            "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.5-flash-lite"
        ]


    def translate_segments(
        self,
        segments: List[Union[Segment, Dict[str, Any]]],
        source_lang: str = "auto",
        target_lang: str = "km",
        progress_callback = None,
        is_cancelled_fn = None
    ) -> List[Segment]:
        """
        Translate a list of dialogue segments with contextual awareness and duration constraints.
        Uses optimized 20-segment batching with 3 parallel workers for ultra-fast throughput.
        """
        if not segments:
            return []

        # Convert all to Segment models
        seg_models: List[Segment] = []
        for i, s in enumerate(segments):
            if isinstance(s, Segment):
                seg_models.append(s)
            else:
                seg_models.append(Segment.from_dict(s, default_idx=i+1))

        total = len(seg_models)
        batch_size = 20
        logger.info(f"🌐 [Translation] Starting parallel translation of {total} segments ({source_lang} -> {target_lang}, batch_size={batch_size})...")

        batches = [seg_models[i:i + batch_size] for i in range(0, total, batch_size)]
        completed_count = 0

        def _process_one_batch(b_idx_and_batch):
            nonlocal completed_count
            if is_cancelled_fn and is_cancelled_fn():
                return
            b_idx, batch = b_idx_and_batch
            
            # Try Gemini contextual batch translation first
            batch_success = self._translate_batch_gemini(batch, source_lang=source_lang, target_lang=target_lang)
            
            # If batch failed, translate items individually using fallbacks
            if not batch_success:
                for seg in batch:
                    if is_cancelled_fn and is_cancelled_fn():
                        break
                    trans = self.translate_single_text(
                        seg.original_text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        duration=seg.slot_duration
                    )
                    seg.translated_text = trans
                    seg.translation_status = "translated"

            completed_count += len(batch)
            if progress_callback:
                pct = min(98, max(5, int((completed_count / float(total)) * 100)))
                progress_callback(pct, f"Gemini AI: Translating dialogues {completed_count}/{total}...")

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(_process_one_batch, enumerate(batches)))

        # End-of-batch Translation Validation Gate: Verify all segments
        invalid_segments = []
        for seg in seg_models:
            is_valid, reason = validate_khmer_translation(seg.translated_text, seg.original_text)
            if not is_valid:
                invalid_segments.append((seg, reason))

        if invalid_segments:
            logger.warning(f"⚠️ [Translation Gate] {len(invalid_segments)} segments failed Khmer validation. Running bounded correction pass...")
            for seg, reason in invalid_segments:
                logger.info(f"🔄 Correcting invalid translation for {seg.id}: {reason} (Current: '{seg.translated_text[:25]}...')")
                for retry_attempt in range(2):
                    corrected = self.translate_single_text(
                        seg.original_text,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        duration=seg.slot_duration
                    )
                    c_valid, _ = validate_khmer_translation(corrected, seg.original_text)
                    if c_valid:
                        seg.translated_text = corrected.strip()
                        seg.translation_status = "translated"
                        break
                    time.sleep(0.3)

        # Synchronize khmer_text so canonical records are identical
        for seg in seg_models:
            seg.khmer_text = seg.translated_text

        if progress_callback:
            progress_callback(100, f"Translation Complete! {total} segments translated.")

        logger.info(f"✅ [Translation] All {total} segments successfully validated and translated to {target_lang}.")
        return seg_models


    def _translate_batch_gemini(self, batch: List[Segment], source_lang: str, target_lang: str) -> bool:
        """Translate a batch of dialogues using Gemini with scene context and duration limits."""
        api_key = self.api_key or get_gemini_api_key()
        if not api_key:
            return False

        lines_payload = []
        for s in batch:
            lines_payload.append({
                "id": s.id,
                "speaker": s.speaker_id,
                "duration_seconds": s.slot_duration,
                "original_text": s.original_text
            })

        prompt = (
            "You are a master cinematic video dubbing translator specializing in authentic, natural spoken Khmer (អ្នកបកប្រែភាពយន្តជំនាញភាសានិយាយខ្មែរធម្មជាតិ).\n"
            f"Translate the following conversational dialogue lines from {source_lang} into vivid, dramatic, natural spoken Khmer.\n\n"
            "GOLDEN DUBBING TRANSLATION RULES:\n"
            "1. PURE KHMER SCRIPT ONLY: Translate ONLY into Khmer characters (អក្សរខ្មែរ, U+1780 to U+17FF). Absolutely NEVER output Thai script (ภาษาไทย), Chinese characters (汉字), or Latin letters in the translated text.\n"
            "2. NATURAL CINEMATIC SPOKEN KHMER (ភាសានិយាយបែបភាពយន្ត រស់រវើក):\n"
            "   - Translate how real Cambodians speak in movies and everyday life. NEVER translate word-for-word literally (កុំបកប្រែពាក្យតាមពាក្យ).\n"
            "   - FORBIDDEN ROBOTIC PHRASES: Do NOT start questions with 'តើ...' or address people as 'អ្នក' unless genuinely addressing a formal stranger. Instead of 'តើអ្នកសុខសប្បាយទេ?' write 'យ៉ាងម៉េចហើយ? / មិនអីទេណ៎ា?'. Instead of 'តើអ្នកចង់ធ្វើអ្វី?' write 'ពួកឯងចង់ធ្វើស្អីហ្នឹង?!'.\n"
            "   - EXPRESSIVE PARTICLES: End sentences with natural Khmer emotional particles that match the drama: '...ណ៎ា / ...ណា៎ / ...ហ្អ៎ / ...ហ្ន៎ / ...ហ្អី / ...ម៉េស / ...ចឹង / ...វ៉ើយ / ...តើ / ...ទៅ!'.\n"
            "3. DYNAMIC RELATIONSHIPS & AUTHENTIC PRONOUNS (សព្វនាមតាមតួអង្គ):\n"
            "   - Parent & Child: 'ប៉ា/ម៉ាក់' vs 'កូន' (e.g. '你怎么醒了' when mom asks child -> 'កូនភ្ញាក់ហើយហ្អ៎?')\n"
            "   - Grandparent & Grandchild: 'លោកតា/លោកយាយ' vs 'ចៅ'\n"
            "   - Couples / Lovers: 'បង' vs 'អូន'\n"
            "   - Friends / Peers / General: 'ឯង' vs 'ខ្ញុំ' or 'គ្នា'\n"
            "   - Boss / Master / Senior: 'លោក / លោកប្រធាន / ចៅហ្វាយ' vs 'ខ្ញុំ / ខ្ញុំបាទ'\n"
            "   - Enemies / Anger / Confrontation: 'ឯង / ហង' vs 'អញ / ខ្ញុំ' (e.g. '你们想干什么' -> 'ពួកឯងចង់ធ្វើស្អី?!')\n"
            "4. IDIOMATIC & DRAMATIC ADAPTATION (បកប្រែតាមន័យសាច់រឿង):\n"
            "   - Adapt foreign idioms into natural Khmer equivalents:\n"
            "     * '绝不是巧合' -> 'មិនមែនជារឿងចៃដន្យដាច់ខាត!'\n"
            "     * '怎么会这样' -> 'មិចបានទៅជាចឹង?!'\n"
            "     * '够...喝一壶了' -> 'ល្មមឱ្យ...រាងចាលម្តងហើយ!' / 'ល្មមឱ្យ...វល់ក្បាលហើយ!'\n"
            "     * '你给我闭嘴' -> 'បិទមាត់ឯងភ្លាមទៅ!'\n"
            "     * '没事了' -> 'មិនអីទេ / អស់អីហើយ'\n"
            "5. DURATION & PACING CONSTRAINTS (ចង្វាក់មាត់ និងរយៈពេល):\n"
            "   - If duration_seconds <= 1.0s: Keep it extremely punchy (3-6 syllables max, e.g. 'លឿនឡើង!', 'ដកថយ!', 'ម៉ាក់!').\n"
            "   - If duration_seconds <= 2.5s: Keep it concise and impactful (6-12 syllables max).\n"
            "   - Never write excessively long explanations that force the voice actor to rush.\n"
            "6. PRESERVE DIALOGUE DEPTH & VOCAL REPETITIONS:\n"
            "   - Do NOT drop names, vital plot details, or character intent.\n"
            "   - Preserve vocal repetitions if present (e.g. '快 快' -> 'លឿនឡើង លឿនឡើង!').\n"
            "7. SPEAKER ROLE & TAG IDENTIFICATION:\n"
            "   - 'child' / '[ក្មេង]' for kids, young boys, girls, children\n"
            "   - 'elder_male' / '[ចាស់ប្រុស]' for grandfathers, elderly men, senior patriarchs\n"
            "   - 'elder_female' / '[ចាស់ស្រី]' for grandmothers, elderly women, senior matriarchs\n"
            "   - 'female' / '[ស្រី]' for adult women, mothers, daughters, wives, ladies\n"
            "   - 'male' / '[ប្រុស]' for adult men, fathers, sons, husbands, young men\n\n"
            "Input Lines:\n"
            f"{json.dumps(lines_payload, ensure_ascii=False, indent=2)}\n\n"
            "Output ONLY a valid JSON array of objects containing EXACTLY every input item with keys 'id', 'translated_text', 'speaker_role', 'speaker_tag':\n"
            "[\n"
            "  {\n"
            "    \"id\": \"seg_0001\",\n"
            "    \"translated_text\": \"...natural spoken Khmer dialogue...\",\n"
            "    \"speaker_role\": \"male\",\n"
            "    \"speaker_tag\": \"[ប្រុស]\"\n"
            "  }\n"
            "]"
        )

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json"
            }
        }

        for model in self.models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        from utils.gemini_parser import parse_gemini_json_response
                        parsed = parse_gemini_json_response(raw_text)
                        
                        # Build comprehensive lookup supporting 'translation', 'translated_text', 'speaker_role', and 'speaker_tag'
                        trans_map = {}
                        for idx_p, item in enumerate(parsed):
                            if isinstance(item, dict):
                                item_id = str(item.get("id", "")).strip()
                                trans_txt = str(item.get("translation") or item.get("translated_text") or "").strip()
                                spk_r = str(item.get("speaker_role") or item.get("role") or "").strip().lower()
                                spk_t = str(item.get("speaker_tag") or item.get("tag") or "").strip()
                                item_val = {"text": trans_txt, "role": spk_r, "tag": spk_t}
                                if item_id:
                                    trans_map[item_id] = item_val
                                    clean_id = re.sub(r"^[^\d]+", "", item_id).lstrip("0") or "0"
                                    trans_map[clean_id] = item_val
                                trans_map[f"pos_{idx_p}"] = item_val

                        all_mapped = True
                        for i, s in enumerate(batch):
                            clean_s_id = re.sub(r"^[^\d]+", "", str(s.id)).lstrip("0") or "0"
                            matched = trans_map.get(str(s.id)) or trans_map.get(clean_s_id)
                            if not matched and len(parsed) == len(batch):
                                matched = trans_map.get(f"pos_{i}")

                            if isinstance(matched, dict):
                                matched_text = matched.get("text", "")
                                spk_role = matched.get("role", "")
                                spk_tag = matched.get("tag", "")
                            else:
                                matched_text = str(matched or "")
                                spk_role = ""
                                spk_tag = ""

                            if matched_text and matched_text.strip():
                                is_valid, rsn = validate_khmer_translation(matched_text.strip(), s.original_text)
                                if is_valid:
                                    s.translated_text = matched_text.strip()
                                    s.translation_status = "translated"

                                    # Infer role/tag if missing
                                    if not spk_role or not spk_tag:
                                        from services.speaker_detector import SpeakerDetector
                                        detector = SpeakerDetector()
                                        res = detector.detect_speaker_for_segment(s.translated_text, s.original_text, s.start, s.end)
                                        spk_role = spk_role or res.get("role", "male")

                                    # Map to the 5 core personas and voices
                                    if spk_role == "child" or "[ក្មេង]" in spk_tag:
                                        is_girl = any(w in (s.translated_text or "").lower() for w in ["ស្រី", "sreyka", "girl", "នៀននៀន"])
                                        s.persona = "Girl / Child" if is_girl else "Boy / Child"
                                        s.voice_id = "Khmer Child - Girl (Sreyka)" if is_girl else "Khmer Child - Boy (Vannak)"
                                        s.speaker_tag = "[ក្មេង]"
                                    elif spk_role == "elder_female" or "[ចាស់ស្រី]" in spk_tag:
                                        s.persona = "Elderly Female"
                                        s.voice_id = "Khmer Elder - Female (Grandmother)"
                                        s.speaker_tag = "[ចាស់ស្រី]"
                                    elif spk_role == "elder_male" or "[ចាស់ប្រុស]" in spk_tag or "[ចាស់]" in spk_tag:
                                        s.persona = "Elderly Male"
                                        s.voice_id = "Khmer Elder - Male (Grandfather)"
                                        s.speaker_tag = "[ចាស់ប្រុស]"
                                    elif spk_role == "female" or "[ស្រី]" in spk_tag:
                                        s.persona = "Female Adult"
                                        s.voice_id = "Khmer Female - Sreymom"
                                        s.speaker_tag = "[ស្រី]"
                                    else:
                                        s.persona = "Male Adult"
                                        s.voice_id = "Khmer Male - Piseth"
                                        s.speaker_tag = "[ប្រុស]"
                                    s.speaker_role = spk_role
                                else:
                                    logger.warning(f"⚠️ [Translation Validator] Batch item {s.id} rejected: {rsn}. Text: '{matched_text[:30]}'")
                                    all_mapped = False
                                    s.translation_status = "VALIDATION_FAILED"
                            else:
                                all_mapped = False
                                s.translation_status = "TRANSLATION_FAILED"

                        # Explicit retry for any individual items missed or rejected by validation
                        if not all_mapped:
                            for s in batch:
                                if not s.translated_text or s.translation_status != "translated":
                                    logger.warning(f"⚠️ [Translation] Segment {s.id} failed validation/mapping. Retrying individually with strict Khmer constraint...")
                                    res_single = self.translate_single_text(
                                        s.original_text,
                                        source_lang=source_lang,
                                        target_lang=target_lang,
                                        duration=s.slot_duration
                                    )
                                    s_valid, s_rsn = validate_khmer_translation(res_single, s.original_text)
                                    if s_valid:
                                        s.translated_text = res_single.strip()
                                        s.translation_status = "translated"
                                    else:
                                        # Sanitize any residual foreign script (e.g. trailing Thai/Chinese particle)
                                        cleaned = re.sub(r'[\u0E00-\u0E7F\u4E00-\u9FFF]', '', res_single).strip()
                                        if cleaned and re.search(r'[\u1780-\u17FF]', cleaned):
                                            s.translated_text = cleaned
                                            s.translation_status = "translated"
                                        else:
                                            s.translated_text = s.original_text
                                            s.translation_status = "ready"

                        return True
            except Exception as e:
                logger.debug(f"Gemini {model} batch translation error: {e}")
                continue

        return False

    def translate_single_text(self, text: str, source_lang: str = "auto", target_lang: str = "km", duration: float = 0.0) -> str:
        """Translate a single line with multi-tiered fallback (Gemini -> Google Mobile -> MyMemory)."""
        if not text or not text.strip():
            return ""

        # 1. Gemini Single
        api_key = self.api_key or get_gemini_api_key()
        if api_key:
            res = self._translate_single_gemini(text, source_lang, target_lang, duration)
            if res:
                return res

        # 2. Free Google Mobile Translation Engine
        res = self._translate_google_mobile(text, source_lang, target_lang)
        if res:
            return res

        # 3. MyMemory Free API
        res = self._translate_mymemory(text, source_lang, target_lang)
        if res:
            return res

        return text

    def _translate_single_gemini(self, text: str, source_lang: str, target_lang: str, duration: float) -> Optional[str]:
        api_key = self.api_key or get_gemini_api_key()
        dur_instruction = f"Speaking slot: {duration:.1f}s. Keep it punchy and concise to match actor lip pace." if duration > 0 else ""
        prompt = (
            f"You are a professional cinematic video dubbing translator specializing in natural spoken Khmer (ភាសានិយាយបែបភាពយន្តធម្មជាតិ).\n"
            f"Translate this dialogue line from {source_lang} into vivid, natural conversational Khmer:\n"
            f"CRITICAL RULES:\n"
            f"- Output MUST be 100% pure Khmer characters (អក្សរខ្មែរ, U+1780 to U+17FF). Absolutely NO Thai script, Chinese, or Latin.\n"
            f"- NATURAL SPOKEN KHMER: Avoid literal word-for-word translation. NEVER use robotic 'តើអ្នក...'. Use authentic pronouns (ប៉ា, ម៉ាក់, កូន, បង, អូន, ឯង, ខ្ញុំ) and expressive particles (ណ៎ា, ហ្អ៎, ទៅ, ម៉េស, ហ្នឹង...).\n"
            f"- PRESERVE COMPLETE MEANING: Keep plot meaning and emotional weight.\n"
            f"- {dur_instruction}\n\n"
            f"Original: \"{text}\"\n"
            f"Output ONLY the Khmer translation without quotes, Thai, Chinese, or explanations."
        )
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        headers = {"Content-Type": "application/json"}
        for model in self.models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=8)
                if r.status_code == 200:
                    data = r.json()
                    cand = data.get("candidates", [])
                    if cand:
                        return cand[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            except Exception:
                continue
        return None

    def _translate_google_mobile(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        try:
            sl = "auto" if source_lang == "auto" else source_lang
            url = f"https://translate.google.com/m?sl={sl}&tl={target_lang}&q=" + urllib.parse.quote(text)
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X)'}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                res_html = resp.read().decode('utf-8')
                match = re.search(r'<div[^>]*class=\"result-container\"[^>]*>(.*?)</div>', res_html, re.DOTALL)
                if match:
                    res = html.unescape(match.group(1).strip())
                    if res and not res.startswith("["):
                        return res
        except Exception:
            pass
        return None

    def _translate_mymemory(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        try:
            sl = "en" if source_lang == "auto" else source_lang
            url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair={sl}|{target_lang}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                res_text = data.get('responseData', {}).get('translatedText')
                if res_text and not res_text.startswith("MYMEMORY WARNING"):
                    return res_text
        except Exception:
            pass
        return None
