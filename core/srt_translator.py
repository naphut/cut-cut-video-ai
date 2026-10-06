"""
Professional SRT Translation System for Khmer Video Dubbing
Follows best practices for AI translation with context preservation,
dialogue detection, speaker mapping, and spoken Khmer (ភាសានិយាយ) dubbing optimization.
"""

import re
import json
import os
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from utils.logger import logger
from utils.config_manager import get_gemini_api_key

@dataclass
class SubtitleSegment:
    """Represents a single subtitle segment with timing and context"""
    index: int
    start_time: str        # "00:00:01,000"
    end_time: str          # "00:00:03,500"
    start_seconds: float
    end_seconds: float
    text: str
    translated_text: str = ""
    speaker: str = ""
    context: str = ""

@dataclass
class SRTBatch:
    """Batch of subtitle segments for contextual translation"""
    segments: List[SubtitleSegment]
    batch_id: int
    batch_size: int = 20   # Process 20-50 segments per batch

class SRTTranslator:
    """
    Advanced SRT Translator with context preservation and dubbing optimization
    """
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-1.5-flash"):
        self.api_key = api_key or get_gemini_api_key()
        self.model = model
        self.batch_size = 20
        self.source_language = "auto"
        self.target_language = "km"  # Khmer
        
        self.speaker_mapping: Dict[str, str] = {}
        self.character_context: Dict[str, str] = {}
        
    def parse_srt(self, srt_content: str) -> List[SubtitleSegment]:
        """Parse raw SRT content string into structured SubtitleSegment list."""
        segments = []
        blocks = srt_content.strip().replace("\r\n", "\n").split("\n\n")
        
        for block in blocks:
            lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
            if len(lines) < 3:
                continue
                
            try:
                index = int(lines[0].strip())
                time_line = lines[1].strip()
                
                start_str, end_str = self._parse_timestamp_line(time_line)
                start_sec = self._time_to_seconds(start_str)
                end_sec = self._time_to_seconds(end_str)
                
                raw_text = " ".join(lines[2:]).strip()
                m_bracket = re.match(r'^\s*(?:\[|\()([^\]\)]+)(?:\]|\))\s*[:：\-–—]?\s*', raw_text)
                m_colon = re.match(r'^\s*(ក្មេង(?:ប្រុស|ស្រី)?|កូន|child(?:ren)?|kid|boy|girl|ចាស់(?:ប្រុស|ស្រី)?|មនុស្សចាស់|elder(?:ly)?(?:\s*(?:male|female|man|woman))?|លោកតា|លោកយាយ|យាយ|តា|ស្រី|female|woman|lady|ប្រុស|male|man|guy|speaker\s*\d+)\s*[:：]\s*', raw_text, flags=re.IGNORECASE)
                m = m_bracket or m_colon
                spk = ""
                clean_text = raw_text
                if m:
                    clean_text = raw_text[m.end():].strip()
                    tw = m.group(1).lower()
                    if any(w in tw for w in ['ក្មេង', 'កូន', 'child', 'kid', 'boy', 'girl']):
                        spk = "🧒 ក្មេង"
                    elif any(w in tw for w in ['ចាស់ស្រី', 'លោកយាយ', 'យាយ']) or 'elderly female' in tw:
                        spk = "👵 ចាស់ស្រី"
                    elif any(w in tw for w in ['ចាស់ប្រុស', 'លោកតា', 'តា']) or 'elderly male' in tw:
                        spk = "👴 ចាស់ប្រុស"
                    elif any(w in tw for w in ['ចាស់', 'elder', 'មនុស្សចាស់']):
                        spk = "👵👴 មនុស្សចាស់"
                    elif any(w in tw for w in ['ស្រី', 'female', 'woman', 'lady']):
                        spk = "👩 ស្រី"
                    elif any(w in tw for w in ['ប្រុស', 'male', 'man']):
                        spk = "👨 ប្រុស"

                from services.khmer_frontend import strip_speaker_tags
                clean_text = strip_speaker_tags(clean_text)

                segments.append(SubtitleSegment(
                    index=index,
                    start_time=start_str,
                    end_time=end_str,
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    text=clean_text or raw_text,
                    speaker=spk
                ))
            except Exception as e:
                logger.warning(f"Error parsing SRT segment block: {e}")
                continue
                
        return segments
    
    def parse_segment_dicts(self, segments_list: List[dict]) -> List[SubtitleSegment]:
        """Convert pipeline segment dicts [{'start', 'end', 'text'}] to SubtitleSegment objects."""
        sub_segments = []
        for i, seg in enumerate(segments_list):
            start_sec = float(seg.get("start", 0.0))
            end_sec = float(seg.get("end", 0.0))
            start_str = self._seconds_to_time(start_sec)
            end_str = self._seconds_to_time(end_sec)
            text = seg.get("original_text", seg.get("text", ""))
            khmer_text = seg.get("khmer_text", "")
            
            sub_segments.append(SubtitleSegment(
                index=i + 1,
                start_time=start_str,
                end_time=end_str,
                start_seconds=start_sec,
                end_seconds=end_sec,
                text=text,
                translated_text=khmer_text
            ))
        return sub_segments

    def _parse_timestamp_line(self, time_line: str) -> Tuple[str, str]:
        """Parse timestamp line like '00:00:01,000 --> 00:00:03,500'"""
        parts = time_line.split(' --> ')
        return parts[0].strip(), parts[1].strip()
    
    def _time_to_seconds(self, time_str: str) -> float:
        """Convert SRT timestamp string to seconds float."""
        time_str = time_str.replace(',', '.')
        parts = time_str.split(':')
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s
    
    def _seconds_to_time(self, seconds: float) -> str:
        """Convert seconds back to SRT timestamp format (00:00:00,000)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds % 1) * 1000))
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    
    def create_translation_prompt(
        self, 
        segments: List[SubtitleSegment],
        previous_context: str = "",
        character_context: str = ""
    ) -> str:
        """Create the system prompt for contextual Khmer subtitle translation."""
        segments_text = "\n\n".join([
            f"{seg.index}\n{seg.start_time} --> {seg.end_time}\n{seg.text}"
            for seg in segments
        ])
        
        prompt = f"""You are a professional Khmer subtitle translator with expertise in video dubbing and localization.

Your task is to translate the following SRT subtitle file from {self.source_language} into natural, clear, concise, and accurate Khmer (ភាសាខ្មែរ) specifically crafted for AI video dubbing.

**CRITICAL VOICE DUBBING & TIME-CONSTRAINT RULES:**
1. Preserve the original SRT structure exactly.
2. NEVER change, remove, reorder, or merge subtitle numbers or timestamps.
3. Translate ONLY the dialogue text.
4. AI CONDENSING RULE: Spoken Khmer text tends to have more syllables than English/foreign languages. You MUST produce CONCISE, PITHY spoken Khmer so that the voice actor can comfortably speak the translation within the segment's exact timestamp duration.
5. Do NOT translate word-by-word. Omit filler words and redundant phrases while strictly preserving the core meaning, tone, and emotion.
6. Use natural spoken Khmer (ភាសានិយាយ) that everyday Cambodians naturally speak.
7. Avoid overly formal or literary vocabulary unless the speaker in the scene is formal.
8. If a dialogue segment is short (e.g., 1-2 seconds), keep the Khmer translation brief and punchy.
9. Output ONLY valid SRT format without any extra notes, explanations, or quotes.

**FOR DRAMA / STORY CONTENT:**
{character_context}

**PREVIOUS CONTEXT:**
{previous_context}

**INPUT SRT:**

{segments_text}

**OUTPUT:**
Return ONLY the translated SRT content with the exact same structure, numbers, and timestamps."""
        
        return prompt
    
    def create_batches(self, segments: List[SubtitleSegment]) -> List[SRTBatch]:
        """Create batches of segments for contextual translation."""
        batches = []
        batch_size = self.batch_size
        
        for i in range(0, len(segments), batch_size):
            batch_segments = segments[i:i+batch_size]
            batch = SRTBatch(
                segments=batch_segments,
                batch_id=i // batch_size,
                batch_size=len(batch_segments)
            )
            batches.append(batch)
            
        return batches
    
    def get_previous_context(self, batches: List[SRTBatch], current_batch_idx: int) -> str:
        """Get context from previous batches for continuity."""
        if current_batch_idx == 0:
            return ""
            
        prev_batch = batches[current_batch_idx - 1]
        context_segments = prev_batch.segments[-3:]
        
        context_text = [f"{seg.index}. {seg.text}" for seg in context_segments]
        return "Previous context:\n" + "\n".join(context_text)
    
    def build_character_context(self, segments: List[SubtitleSegment]) -> str:
        """Build character context for drama/conversation content."""
        characters = set()
        for seg in segments:
            text = seg.text
            if ':' in text:
                parts = text.split(':')
                if len(parts) > 1 and len(parts[0].strip()) < 25:
                    characters.add(parts[0].strip())
        
        if not characters:
            return ""
            
        context = "**Character Dialogues:**\n"
        for char in characters:
            context += f"- {char} is a speaker in this scene\n"
        return context

    def translate_segments(
        self, 
        segments: List[SubtitleSegment],
        source_lang: str = "en",
        target_lang: str = "km"
    ) -> List[SubtitleSegment]:
        """Translate SubtitleSegment objects using Gemini or fallback service."""
        self.source_language = source_lang
        self.target_language = target_lang
        
        if not segments:
            return []

        # If Gemini API key is available, use Gemini SRT Batch Translation
        if self.api_key:
            gemini_translator = GeminiSRTTranslator(api_key=self.api_key, model=self.model)
            return gemini_translator.translate_batch_segments(segments, source_lang=source_lang, target_lang=target_lang)

        # Fallback service using MyMemory / Lingva translation service per segment
        from services.translation_service import TranslationService
        ts = TranslationService()
        
        for seg in segments:
            seg.translated_text = ts.translate_text(seg.text, source_lang=source_lang, target_lang=target_lang)
            
        return segments

    def format_srt(self, segments: List[SubtitleSegment]) -> str:
        """Format SubtitleSegment list back to standard SRT content string."""
        srt_lines = []
        for seg in segments:
            srt_lines.append(str(seg.index))
            srt_lines.append(f"{seg.start_time} --> {seg.end_time}")
            text = seg.translated_text if seg.translated_text else seg.text
            srt_lines.append(text)
            srt_lines.append("")
            
        return "\n".join(srt_lines)
    
    def save_srt(self, segments: List[SubtitleSegment], filepath: str):
        """Save translated segments to SRT file."""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self.format_srt(segments))


class GeminiSRTTranslator(SRTTranslator):
    """
    SRT Translator powered by Google Gemini API
    """
    def __init__(self, api_key: str, model: str = "gemini-1.5-flash"):
        super().__init__(api_key, model)
        self.model_instance = None
        self._init_gemini()

    def _init_gemini(self):
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            self.model_instance = genai.GenerativeModel(self.model)
            logger.info(f"Initialized Gemini SRT Translator with model {self.model}")
        except Exception as e:
            logger.warning(f"Could not initialize google.generativeai: {e}")

    def translate_batch_segments(
        self, 
        segments: List[SubtitleSegment], 
        source_lang: str = "en", 
        target_lang: str = "km"
    ) -> List[SubtitleSegment]:
        """Batch translate segments using Gemini API."""
        if not self.model_instance:
            return super().translate_segments(segments, source_lang, target_lang)

        batches = self.create_batches(segments)
        character_context = self.build_character_context(segments)
        
        all_translated_map = {}

        for batch_idx, batch in enumerate(batches):
            prev_context = self.get_previous_context(batches, batch_idx)
            prompt = self.create_translation_prompt(batch.segments, prev_context, character_context)
            
            try:
                response = self.model_instance.generate_content(
                    prompt,
                    generation_config={"temperature": 0.3, "top_p": 0.9, "max_output_tokens": 4096}
                )
                res_srt = response.text.strip()
                translated_segs = self.parse_srt(res_srt)
                
                for orig_seg, trans_seg in zip(batch.segments, translated_segs):
                    orig_seg.translated_text = trans_seg.text
                    all_translated_map[orig_seg.index] = orig_seg
            except Exception as e:
                logger.error(f"Gemini batch translation error: {e}")

        # Ensure all segments have valid fallback if any missed
        from services.translation_service import TranslationService
        ts = TranslationService()
        for seg in segments:
            if not seg.translated_text:
                seg.translated_text = ts.translate_text(seg.text, source_lang, target_lang)

        return segments


class ContextualSRTTranslator:
    """
    Advanced contextual SRT translator with speaker detection
    """
    def __init__(self):
        self.speaker_patterns = [
            r'^([A-Z][a-z]+):',
            r'^([A-Z][a-z]+)\s+said',
            r'^([A-Z][a-z]+)\s+asked',
            r'^([A-Z][a-z]+)\s+replied',
        ]
        
    def detect_speakers(self, segments: List[SubtitleSegment]) -> Dict[int, str]:
        """Detect speakers in dialogue lines."""
        speaker_map = {}
        for seg in segments:
            text = seg.text.strip()
            for pattern in self.speaker_patterns:
                match = re.match(pattern, text)
                if match:
                    speaker_map[seg.index] = match.group(1)
                    break
        return speaker_map
