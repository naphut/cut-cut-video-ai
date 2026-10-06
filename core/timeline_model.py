import os
import copy
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from utils.logger import logger


@dataclass
class Transition:
    """
    Phase 3 Transition Entity:
    Represents a temporal relationship and visual/audio blend between two adjacent clips.
    """
    id: str
    clip_a_id: str
    clip_b_id: str
    cut_time: float
    duration: float = 1.0
    alignment: str = "center"       # "center", "start_on_cut", "end_on_cut"
    type: str = "cross_dissolve"    # "cross_dissolve", "fade_black", "wipe_left", etc.
    easing: str = "linear"          # "linear", "ease_in", "ease_out", "ease_in_out"
    audio_mode: str = "equal_power" # "equal_power", "linear", "none"
    parameters: Dict[str, Any] = field(default_factory=dict)



class Command:
    """Base Command pattern class for deterministic undo/redo operations."""
    def execute(self, model: 'TimelineModel') -> bool:
        raise NotImplementedError

    def undo(self, model: 'TimelineModel') -> bool:
        raise NotImplementedError


class SplitClipCommand(Command):
    """Splits a target video clip at the playhead position into two clips."""
    def __init__(self, split_sec: float):
        self.split_sec = round(split_sec, 2)
        self.prev_clips = []
        self.prev_playhead = split_sec

    def execute(self, model: 'TimelineModel') -> bool:
        self.prev_clips = copy.deepcopy(model.video_clips)
        self.prev_playhead = model.playhead_pos_sec

        target_idx = None
        for idx, clip in enumerate(model.video_clips):
            c_st = clip.get("start", 0.0)
            c_dur = clip.get("duration", 0.0)
            c_end = c_st + c_dur
            if c_st + 0.05 < self.split_sec < c_end - 0.05:
                target_idx = idx
                break

        if target_idx is None:
            return False

        orig_clip = model.video_clips[target_idx]
        c_st = orig_clip.get("start", 0.0)
        c_dur = orig_clip.get("duration", 0.0)
        c_name = orig_clip.get("name", "Clip")
        c_path = orig_clip.get("path", "")

        dur1 = round(self.split_sec - c_st, 2)
        dur2 = round(max(0.05, c_dur - dur1), 2)

        clip1 = {"name": c_name, "start": c_st, "duration": dur1, "path": c_path}
        clip2 = {"name": c_name, "start": round(self.split_sec, 2), "duration": dur2, "path": c_path}

        new_clips = model.video_clips[:target_idx] + [clip1, clip2] + model.video_clips[target_idx + 1:]
        model.video_clips = new_clips
        model.notify_changed()
        return True

    def undo(self, model: 'TimelineModel') -> bool:
        model.video_clips = copy.deepcopy(self.prev_clips)
        model.playhead_pos_sec = self.prev_playhead
        model.notify_changed()
        return True


class TrimLeftCommand(Command):
    """Trims everything from 0.0 to trim_sec and ripples all clips & subtitles left."""
    def __init__(self, trim_sec: float):
        self.trim_sec = round(trim_sec, 2)
        self.prev_clips = []
        self.prev_segments = []
        self.prev_playhead = 0.0

    def execute(self, model: 'TimelineModel') -> bool:
        if self.trim_sec <= 0.05 or self.trim_sec >= model.total_duration_sec - 0.05:
            return False

        self.prev_clips = copy.deepcopy(model.video_clips)
        self.prev_segments = copy.deepcopy(model.segments)
        self.prev_playhead = model.playhead_pos_sec

        # 1. Ripple clips
        new_clips = []
        for c in model.video_clips:
            c_st = c.get("start", 0.0)
            c_dur = c.get("duration", 0.0)
            c_end = c_st + c_dur
            if c_end <= self.trim_sec:
                continue
            c_copy = dict(c)
            if c_st < self.trim_sec:
                c_copy["start"] = 0.0
                c_copy["duration"] = round(c_end - self.trim_sec, 2)
            else:
                c_copy["start"] = round(c_st - self.trim_sec, 2)
            new_clips.append(c_copy)

        # 2. Ripple subtitles
        new_segs = []
        for s in model.segments:
            st = s.get("start", 0.0)
            et = s.get("end", 0.0)
            if et <= self.trim_sec:
                continue
            s_copy = dict(s)
            s_copy["start"] = round(max(0.0, st - self.trim_sec), 2)
            s_copy["end"] = round(max(0.1, et - self.trim_sec), 2)
            new_segs.append(s_copy)

        model.video_clips = new_clips
        model.segments = new_segs
        model.playhead_pos_sec = 0.0
        model.total_duration_sec = max(0.1, model.total_duration_sec - self.trim_sec)
        model.notify_changed()
        return True

    def undo(self, model: 'TimelineModel') -> bool:
        model.video_clips = copy.deepcopy(self.prev_clips)
        model.segments = copy.deepcopy(self.prev_segments)
        model.playhead_pos_sec = self.prev_playhead
        model.total_duration_sec = sum(c.get("duration", 0.0) for c in model.video_clips)
        model.notify_changed()
        return True


class TrimRightCommand(Command):
    """Trims everything from trim_sec to total_duration and updates duration."""
    def __init__(self, trim_sec: float):
        self.trim_sec = round(trim_sec, 2)
        self.prev_clips = []
        self.prev_segments = []
        self.prev_playhead = 0.0

    def execute(self, model: 'TimelineModel') -> bool:
        if self.trim_sec <= 0.05 or self.trim_sec >= model.total_duration_sec - 0.05:
            return False

        self.prev_clips = copy.deepcopy(model.video_clips)
        self.prev_segments = copy.deepcopy(model.segments)
        self.prev_playhead = model.playhead_pos_sec

        new_clips = []
        for c in model.video_clips:
            c_st = c.get("start", 0.0)
            c_dur = c.get("duration", 0.0)
            if c_st >= self.trim_sec:
                continue
            c_copy = dict(c)
            if (c_st + c_dur) > self.trim_sec:
                c_copy["duration"] = round(self.trim_sec - c_st, 2)
            new_clips.append(c_copy)

        new_segs = []
        for s in model.segments:
            st = s.get("start", 0.0)
            et = s.get("end", 0.0)
            if st >= self.trim_sec:
                continue
            s_copy = dict(s)
            if et > self.trim_sec:
                s_copy["end"] = round(self.trim_sec, 2)
            new_segs.append(s_copy)

        model.video_clips = new_clips
        model.segments = new_segs
        model.playhead_pos_sec = min(model.playhead_pos_sec, self.trim_sec)
        model.total_duration_sec = self.trim_sec
        model.notify_changed()
        return True

    def undo(self, model: 'TimelineModel') -> bool:
        model.video_clips = copy.deepcopy(self.prev_clips)
        model.segments = copy.deepcopy(self.prev_segments)
        model.playhead_pos_sec = self.prev_playhead
        model.total_duration_sec = sum(c.get("duration", 0.0) for c in model.video_clips)
        model.notify_changed()
        return True


class DeleteClipCommand(Command):
    """Deletes a selected clip and ripples subsequent clips & subtitles left."""
    def __init__(self, clip_idx: int):
        self.clip_idx = clip_idx
        self.prev_clips = []
        self.prev_segments = []
        self.prev_playhead = 0.0

    def execute(self, model: 'TimelineModel') -> bool:
        if self.clip_idx < 0 or self.clip_idx >= len(model.video_clips):
            return False
        if len(model.video_clips) <= 1:
            return False

        self.prev_clips = copy.deepcopy(model.video_clips)
        self.prev_segments = copy.deepcopy(model.segments)
        self.prev_playhead = model.playhead_pos_sec

        del_clip = model.video_clips[self.clip_idx]
        del_st = del_clip.get("start", 0.0)
        del_dur = del_clip.get("duration", 0.0)
        del_end = del_st + del_dur

        new_clips = []
        for idx, c in enumerate(model.video_clips):
            if idx == self.clip_idx:
                continue
            c_st = c.get("start", 0.0)
            c_copy = dict(c)
            if c_st >= del_end:
                c_copy["start"] = round(max(0.0, c_st - del_dur), 2)
            new_clips.append(c_copy)

        new_segs = []
        for s in model.segments:
            st = s.get("start", 0.0)
            et = s.get("end", 0.0)
            if st >= del_st and et <= del_end:
                continue
            s_copy = dict(s)
            if st >= del_end:
                s_copy["start"] = round(max(0.0, st - del_dur), 2)
                s_copy["end"] = round(max(0.1, et - del_dur), 2)
            elif et > del_st and et <= del_end:
                s_copy["end"] = round(del_st, 2)
            new_segs.append(s_copy)

        model.video_clips = new_clips
        model.segments = new_segs
        model.playhead_pos_sec = del_st
        model.total_duration_sec = sum(c.get("duration", 0.0) for c in new_clips)
        model.notify_changed()
        return True

    def undo(self, model: 'TimelineModel') -> bool:
        model.video_clips = copy.deepcopy(self.prev_clips)
        model.segments = copy.deepcopy(self.prev_segments)
        model.playhead_pos_sec = self.prev_playhead
        model.total_duration_sec = sum(c.get("duration", 0.0) for c in model.video_clips)
        model.notify_changed()
        return True


class TimelineModel:
    """
    Central Data Model for Non-Linear Editing:
    - Maintains state for video clips, subtitle segments, in/out marks, and playhead
    - Decoupled from Qt widgets / painting logic
    - Operates through reversible Commands with a 20-level history stack
    """
    def __init__(self):
        self.video_clips: List[Dict[str, Any]] = []
        self.transitions: List[Transition] = []
        self.segments: List[Dict[str, Any]] = []
        self.total_duration_sec: float = 60.0
        self.playhead_pos_sec: float = 0.0
        self.in_point_sec: Optional[float] = None
        self.out_point_sec: Optional[float] = None
        self.selected_clip_idx: Optional[int] = None
        self.video_path: Optional[str] = None

        self._undo_stack: List[Command] = []
        self._redo_stack: List[Command] = []
        self._listeners: List[Callable[[], None]] = []

    def add_listener(self, callback: Callable[[], None]):
        if callback not in self._listeners:
            self._listeners.append(callback)

    def notify_changed(self):
        for cb in self._listeners:
            try:
                cb()
            except Exception as e:
                logger.debug(f"TimelineModel listener error: {e}")

    def execute_command(self, cmd: Command) -> bool:
        if cmd.execute(self):
            self._undo_stack.append(cmd)
            if len(self._undo_stack) > 20:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
            return True
        return False

    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        cmd = self._undo_stack.pop()
        if cmd.undo(self):
            self._redo_stack.append(cmd)
            return True
        return False

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        cmd = self._redo_stack.pop()
        if cmd.execute(self):
            self._undo_stack.append(cmd)
            return True
        return False
