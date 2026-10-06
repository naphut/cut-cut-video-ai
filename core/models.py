"""
Core Data Models for AI Video Dubbing Studio - System Specification v1.0
Defines production-grade Speaker Profiles, Segments, and Project state schemas.
"""
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List

# Standard 12 Personas (Clean & Professional)
PERSONA_CHOICES = [
    "Male Adult",
    "Female Adult",
    "Boy / Child",
    "Girl / Child",
    "Elderly Male",
    "Elderly Female",
    "Young Male",
    "Young Female",
    "Narrator",
    "AI / Robot",
    "Announcer",
    "Crowd / Group"
]

# Standard 9 Emotions (Clean & Professional)
EMOTION_CHOICES = [
    "Neutral",
    "Happy",
    "Sad",
    "Angry",
    "Fear",
    "Excited",
    "Romantic",
    "Crying",
    "Surprised"
]

# Standard 8 Speaking Styles
STYLE_CHOICES = [
    "Normal",
    "Fast",
    "Slow",
    "Soft",
    "Loud",
    "Whisper",
    "Serious",
    "Dramatic"
]

def normalize_persona(p: str) -> str:
    """Normalize persona string by removing legacy emojis if present."""
    if not p:
        return "Male Adult"
    s = str(p).strip()
    for choice in PERSONA_CHOICES:
        if choice.lower() == s.lower() or choice.lower() in s.lower():
            return choice
    return s

def normalize_emotion(e: str) -> str:
    """Normalize emotion string by removing legacy emojis if present."""
    if not e:
        return "Neutral"
    s = str(e).strip()
    for choice in EMOTION_CHOICES:
        if choice.lower() == s.lower() or choice.lower() in s.lower():
            return choice
    return s

# Default Persona to Voice Mapping (Supports both clean and legacy keys)
PERSONA_DEFAULT_VOICE = {
    "Male Adult": "Khmer Male - Piseth",
    "Female Adult": "Khmer Female - Sreymom",
    "Boy / Child": "Khmer Child - Boy (Vannak)",
    "Girl / Child": "Khmer Child - Girl (Sreyka)",
    "Elderly Male": "Khmer Elder - Male (Grandfather)",
    "Elderly Female": "Khmer Elder - Female (Grandmother)",
    "Young Male": "Khmer Male - Piseth",
    "Young Female": "Khmer Female - Sreymom",
    "Narrator": "Khmer Male - Piseth",
    "AI / Robot": "Khmer Male - Piseth",
    "Announcer": "Khmer Male - Piseth",
    "Crowd / Group": "Khmer Male - Piseth",
    # Legacy emoji mappings for backwards compatibility
    "👨 Male Adult": "Khmer Male - Piseth",
    "👩 Female Adult": "Khmer Female - Sreymom",
    "👦 Boy / Child": "Khmer Child - Boy (Vannak)",
    "👧 Girl / Child": "Khmer Child - Girl (Sreyka)",
    "👴 Elderly Male": "Khmer Elder - Male (Grandfather)",
    "👵 Elderly Female": "Khmer Elder - Female (Grandmother)",
    "👨🦱 Young Male": "Khmer Male - Piseth",
    "👩🦰 Young Female": "Khmer Female - Sreymom",
    "🎭 Narrator": "Khmer Male - Piseth",
    "🤖 AI / Robot": "Khmer Male - Piseth",
    "📢 Announcer": "Khmer Male - Piseth",
    "👥 Crowd / Group": "Khmer Male - Piseth"
}


@dataclass
class SpeakerProfile:
    """
    Independent Speaker Profile.
    Represents a recurring actor/character in the project across multiple lines.
    """
    id: str                                  # "speaker_01", "speaker_02", etc.
    display_name: str                        # "Speaker 1"
    persona: str = "👨 Male Adult"
    gender: str = "male"                     # male | female | neutral
    age_group: str = "adult"                 # child | young_adult | adult | elder
    voice_id: str = "Khmer Male - Piseth"
    default_emotion: str = "😐 Neutral"
    default_style: str = "Normal"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_id: str = "speaker_01") -> "SpeakerProfile":
        spk_id = data.get("id") or default_id
        d_name = data.get("display_name") or f"Speaker {spk_id.replace('speaker_', '')}"
        persona = data.get("persona") or "👨 Male Adult"
        gender = data.get("gender") or ("female" if "ស្រី" in persona or "Female" in persona else "male")
        age = data.get("age_group") or ("child" if "ក្មេង" in persona or "Child" in persona else "adult")
        voice = data.get("voice_id") or data.get("voice") or PERSONA_DEFAULT_VOICE.get(persona, "Khmer Male - Piseth")
        emotion = data.get("default_emotion") or data.get("emotion") or "😐 Neutral"
        style = data.get("default_style") or data.get("style") or "Normal"

        return cls(
            id=spk_id,
            display_name=d_name,
            persona=persona,
            gender=gender,
            age_group=age,
            voice_id=voice,
            default_emotion=emotion,
            default_style=style
        )


@dataclass
class Segment:
    """
    Rich Subtitle & Dubbing Segment representation.
    Tracks dialogue lifecycle across VAD, STT, Translation, TTS, and Dialogue Sync.
    """
    id: str
    start: float
    end: float
    speaker_id: str = "speaker_01"
    speaker_tag: Optional[str] = None
    speaker_role: Optional[str] = None
    persona: str = "Male Adult"
    emotion: str = "Neutral"
    speaking_style: str = "Normal"
    source_language: str = "auto"
    original_text: str = ""
    target_language: str = "km"
    translated_text: str = ""
    voice_id: str = "Khmer Male - Piseth"
    tts_audio: Optional[str] = None
    tts_duration: float = 0.0
    slot_duration: float = 0.0
    sync_rate: float = 1.0
    audio_hash: Optional[str] = None
    status: str = "ready"                    # ready | processing | completed | error
    translation_status: str = "pending"      # pending | translated | approved
    tts_status: str = "pending"              # pending | generated | synced
    error: Optional[str] = None
    raw_start: Optional[float] = None
    raw_end: Optional[float] = None
    words: Optional[List[Dict[str, Any]]] = None
    confidence: Optional[float] = None

    def __post_init__(self):
        if self.raw_start is None:
            self.raw_start = self.start
        if self.raw_end is None:
            self.raw_end = self.end
        if self.slot_duration <= 0.0:
            self.slot_duration = max(0.1, round(self.end - self.start, 2))
        if not self.translated_text and hasattr(self, 'khmer_text') and getattr(self, 'khmer_text', None):
            self.translated_text = getattr(self, 'khmer_text')
        if not self.audio_hash and self.translated_text:
            self.audio_hash = self.compute_cache_hash()

    def compute_cache_hash(self) -> str:
        """Compute unique content hash for smart incremental voice caching."""
        txt = (self.translated_text or self.original_text or "").strip()
        voice = self.voice_id or ""
        emo = self.emotion or "neutral"
        sty = self.speaking_style or "normal"
        dur = round(self.slot_duration, 1)
        raw = f"{txt}|{voice}|{emo}|{sty}|{dur}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        """Convert segment to dictionary format compatible with UI tables and JSON storage."""
        d = asdict(self)
        # Compatibility aliases for legacy UI widgets
        d["character"] = self.persona or self.speaker_id
        d["speaker"] = self.speaker_id
        d["text"] = self.original_text
        d["khmer_text"] = self.translated_text
        d["voice"] = self.voice_id
        d["style"] = self.speaking_style
        return d

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __setitem__(self, key: str, value: Any):
        if key == "text":
            self.original_text = value
        elif key == "khmer_text":
            self.translated_text = value
            self.audio_hash = self.compute_cache_hash()
        elif key in ["character", "persona"]:
            self.persona = value
        elif key == "speaker":
            self.speaker_id = value
        elif key == "emotion":
            self.emotion = value
            self.audio_hash = self.compute_cache_hash()
        elif key in ["style", "speaking_style"]:
            self.speaking_style = value
            self.audio_hash = self.compute_cache_hash()
        elif key == "voice":
            self.voice_id = value
            self.audio_hash = self.compute_cache_hash()
        elif hasattr(self, key):
            setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return key in self.to_dict()

    def keys(self):
        return self.to_dict().keys()

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_idx: int = 1) -> "Segment":
        """Construct Segment from dictionary with backwards-compatibility support."""
        seg_id = data.get("id") or f"seg_{default_idx:04d}"
        st = float(data.get("start", 0.0))
        et = float(data.get("end", st + 2.5))
        if et <= st:
            et = st + 2.0

        spk = data.get("speaker_id") or data.get("speaker") or f"speaker_{1 + (default_idx % 2):02d}"
        persona = data.get("persona") or data.get("character") or "👨 Male Adult"
        
        # Normalize persona if string is legacy
        if "ស្រី" in persona or "female" in str(persona).lower():
            if "ចាស់" in persona:
                persona = "👵 Elderly Female"
            elif "ក្មេង" in persona:
                persona = "👧 Girl / Child"
            else:
                persona = "👩 Female Adult"
        elif "ក្មេង" in persona or "child" in str(persona).lower():
            persona = "👦 Boy / Child"
        elif "ចាស់" in persona or "elder" in str(persona).lower():
            persona = "👴 Elderly Male"
        elif persona not in PERSONA_CHOICES:
            persona = "👨 Male Adult"

        emotion = data.get("emotion") or "😐 Neutral"
        if emotion not in EMOTION_CHOICES:
            # Match by keyword
            matched_emo = "😐 Neutral"
            for emo in EMOTION_CHOICES:
                if emo.split()[-1].lower() in str(emotion).lower():
                    matched_emo = emo
                    break
            emotion = matched_emo

        style = data.get("speaking_style") or data.get("style") or "Normal"
        if style not in STYLE_CHOICES:
            style = "Normal"

        src_lang = data.get("source_language") or data.get("lang") or "auto"
        orig_text = data.get("original_text") or data.get("text") or ""
        trans_text = data.get("translated_text") or data.get("khmer_text") or ""
        tgt_lang = data.get("target_language") or "km"
        voice = data.get("voice_id") or data.get("voice") or PERSONA_DEFAULT_VOICE.get(persona, "Khmer Male - Piseth")

        return cls(
            id=str(seg_id),
            start=round(st, 2),
            end=round(et, 2),
            speaker_id=str(spk),
            persona=persona,
            emotion=emotion,
            speaking_style=style,
            source_language=src_lang,
            original_text=orig_text,
            target_language=tgt_lang,
            translated_text=trans_text,
            voice_id=voice,
            tts_audio=data.get("tts_audio"),
            tts_duration=float(data.get("tts_duration", 0.0)),
            slot_duration=round(et - st, 2),
            sync_rate=float(data.get("sync_rate", 1.0)),
            audio_hash=data.get("audio_hash"),
            status=data.get("status", "ready"),
            translation_status="translated" if trans_text else "pending",
            tts_status="generated" if data.get("tts_audio") else "pending",
            error=data.get("error"),
            raw_start=float(data.get("raw_start")) if data.get("raw_start") is not None else st,
            raw_end=float(data.get("raw_end")) if data.get("raw_end") is not None else et,
            words=data.get("words"),
            confidence=float(data.get("confidence")) if data.get("confidence") is not None else None
        )


@dataclass
class ProjectData:
    """
    Production-Grade Project State (.vproj / project.json v2.0).
    The Single Source of Truth for Vide AI Studio.
    """
    format: str = "VideAI_Project"
    version: str = "2.0"
    project_name: str = "Untitled Project"
    video_path: Optional[str] = None
    audio_path: Optional[str] = None
    output_dir: str = ""
    speakers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    segments: List[Dict[str, Any]] = field(default_factory=list)
    master_wav: Optional[str] = None
    bgm_volume: int = 30
    effects: Dict[str, Any] = field(default_factory=dict)
    export_settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectData":
        speakers = data.get("speakers", {})
        raw_segs = data.get("segments", [])
        parsed_segs = []
        for i, s in enumerate(raw_segs):
            if isinstance(s, Segment):
                parsed_segs.append(s.to_dict())
            elif isinstance(s, dict):
                parsed_segs.append(Segment.from_dict(s, default_idx=i+1).to_dict())

        return cls(
            format=data.get("format", "VideAI_Project"),
            version=data.get("version", "2.0"),
            project_name=data.get("project_name", "Untitled Project"),
            video_path=data.get("video_path"),
            audio_path=data.get("audio_path"),
            output_dir=data.get("output_dir", ""),
            speakers=speakers,
            segments=parsed_segs,
            master_wav=data.get("master_wav"),
            bgm_volume=int(data.get("bgm_volume", 30)),
            effects=data.get("effects", {}),
            export_settings=data.get("export_settings", {})
        )
