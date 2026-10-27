import os
import re
import sys
import json
import time
import fcntl
import shutil
import inspect
import hashlib
import importlib
import tempfile
import threading
import subprocess

from lfx.custom.custom_component.component import Component
from lfx.io import (
 DataInput,
 FileInput,
 DropdownInput,
 BoolInput,
 IntInput,
 StrInput,
 MultilineInput,
 Output,
)
from lfx.schema.data import Data
from lfx.schema.message import Message


# ================= dependency management (self-healing) =================

_PKG_LOCK = threading.Lock()
_FW = {"module": None, "source": "", "version": "", "error": "", "warming": False}
_FFMPEG = {"path": "", "source": "", "error": ""}


def _persist_dir(name):
 base = os.environ.get("LANGFLOW_CONFIG_DIR") or ""
 if base and os.path.isdir(base) and os.access(base, os.W_OK):
  root = base
 else:
  root = tempfile.gettempdir()
 path = os.path.join(root, name)
 os.makedirs(path, exist_ok=True)
 return path


def _add_to_path(path):
 if path not in sys.path:
  sys.path.append(path)


def _pip_install_pkg(target, package_name):
 commands = [[
  sys.executable, "-m", "pip", "install", "--target", target,
  "--no-warn-script-location", "--disable-pip-version-check", package_name,
 ]]
 uv = shutil.which("uv")
 if uv:
  commands.append([uv, "pip", "install", "--python", sys.executable, "--target", target, package_name])
 errors = []
 for cmd in commands:
  try:
   result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
   if result.returncode == 0:
    return None
   errors.append(cmd[0] + ": " + (result.stderr or result.stdout or "")[-300:])
  except Exception as e:
   errors.append(cmd[0] + ": " + str(e)[:200])
 return " | ".join(errors) or "unknown install error"


def _patch_av():
 try:
  import av
 except Exception:
  return
 if getattr(av.open, "_ceq_patched", False):
  return
 original_open = av.open

 def _compat_open(*args, **kwargs):
  try:
   return original_open(*args, **kwargs)
  except TypeError as e:
   if "metadata_errors" in kwargs and "metadata_errors" in str(e):
    kwargs.pop("metadata_errors", None)
    return original_open(*args, **kwargs)
   raise

 _compat_open._ceq_patched = True
 av.open = _compat_open


def _load_faster_whisper():
 if _FW["module"] is not None:
  return _FW["module"]
 with _PKG_LOCK:
  if _FW["module"] is not None:
   return _FW["module"]
  target = _persist_dir("_pylibs")
  _add_to_path(target)
  importlib.invalidate_caches()
  module = None
  try:
   import faster_whisper
   module = faster_whisper
  except ImportError:
   lock_path = os.path.join(target, ".install.lock")
   with open(lock_path, "a+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
     importlib.invalidate_caches()
     try:
      import faster_whisper
      module = faster_whisper
     except ImportError:
      error = _pip_install_pkg(target, "faster-whisper")
      importlib.invalidate_caches()
      try:
       import faster_whisper
       module = faster_whisper
      except ImportError as e:
       _FW["error"] = error or str(e)
       raise RuntimeError(
        "faster-whisper is not installed and auto-install failed: "
        + (error or str(e))[:400]
        + ". Ask the Matcha team to add faster-whisper to the base image."
       )
    finally:
     fcntl.flock(fh, fcntl.LOCK_UN)
  location = str(getattr(module, "__file__", "") or "")
  _FW["source"] = "self-installed" if location.startswith(target) else "base image"
  _FW["version"] = str(getattr(module, "__version__", "") or "unknown")
  _FW["error"] = ""
  _FW["module"] = module
  return module


def _imageio_ffmpeg_path():
 try:
  import imageio_ffmpeg
  path = imageio_ffmpeg.get_ffmpeg_exe()
 except Exception:
  return None
 if not path or not os.path.isfile(path):
  return None
 try:
  os.chmod(path, os.stat(path).st_mode | 0o111)
 except Exception:
  pass
 if os.access(path, os.X_OK):
  return path
 return None


def _load_ffmpeg():
 if _FFMPEG["path"]:
  return _FFMPEG["path"]
 with _PKG_LOCK:
  if _FFMPEG["path"]:
   return _FFMPEG["path"]
  system_ffmpeg = shutil.which("ffmpeg")
  if system_ffmpeg:
   _FFMPEG["path"] = system_ffmpeg
   _FFMPEG["source"] = "system"
   _FFMPEG["error"] = ""
   return system_ffmpeg
  target = _persist_dir("_pylibs")
  _add_to_path(target)
  importlib.invalidate_caches()
  path = _imageio_ffmpeg_path()
  if not path:
   lock_path = os.path.join(target, ".install.lock")
   with open(lock_path, "a+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
     importlib.invalidate_caches()
     path = _imageio_ffmpeg_path()
     if not path:
      error = _pip_install_pkg(target, "imageio-ffmpeg")
      importlib.invalidate_caches()
      path = _imageio_ffmpeg_path()
      if not path:
       _FFMPEG["error"] = (error or "imageio-ffmpeg installed but binary not found")[:400]
       return None
    finally:
     fcntl.flock(fh, fcntl.LOCK_UN)
  _FFMPEG["path"] = path
  _FFMPEG["source"] = "self-installed (imageio-ffmpeg)"
  _FFMPEG["error"] = ""
  return path


def _warm_up():
 try:
  _load_faster_whisper()
 except Exception as e:
  _FW["error"] = str(e)[:400]
 try:
  _load_ffmpeg()
 except Exception as e:
  _FFMPEG["error"] = str(e)[:400]
 finally:
  _FW["warming"] = False


def _get_decoder():
 try:
  _load_faster_whisper()
  _patch_av()
 except Exception:
  return None
 try:
  from faster_whisper.audio import decode_audio
  return decode_audio
 except Exception:
  pass
 try:
  from faster_whisper import decode_audio
  return decode_audio
 except Exception:
  return None


# ================= transcription engine =================

_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_TRACK_SEMAPHORE = threading.Semaphore(2)
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "qa_transcript_cache")
_STALE_SECONDS = 900
_SAMPLE_RATE = 16000

HALLUCINATION_RE = re.compile(
 r"(thanks? (?:you )?for watching|please subscribe|like and subscribe|"
 r"subtitles? by|captions? by|amara\.org|transcribed by|\u266a)",
 re.IGNORECASE,
)


def _to_float(value, default):
 try:
  return float(value)
 except (TypeError, ValueError):
  return default


def _to_int(value, default):
 try:
  return int(float(value))
 except (TypeError, ValueError):
  return default


def _supported_kwargs(func, kwargs):
 try:
  params = inspect.signature(func).parameters
  return {k: v for k, v in kwargs.items() if k in params}
 except (TypeError, ValueError):
  return kwargs


def _get_model(size, device, compute_type, cpu_threads, num_workers):
 fw = _load_faster_whisper()
 _patch_av()
 key = (size, device, compute_type, cpu_threads, num_workers)
 with _MODEL_LOCK:
  if key not in _MODEL_CACHE:
   kwargs = {
    "device": device,
    "compute_type": compute_type,
    "cpu_threads": cpu_threads,
    "num_workers": num_workers,
    "download_root": _persist_dir("_whisper_models"),
   }
   kwargs = _supported_kwargs(fw.WhisperModel.__init__, kwargs)
   _MODEL_CACHE[key] = fw.WhisperModel(size, **kwargs)
  return _MODEL_CACHE[key]


def _write_json(path, obj):
 tmp = path + ".tmp"
 with open(tmp, "w", encoding="utf-8") as f:
  json.dump(obj, f)
 os.replace(tmp, path)


def _read_json(path):
 try:
  with open(path, "r", encoding="utf-8") as f:
   return json.load(f)
 except Exception:
  return None


def _preprocess_audio(path):
 ffmpeg_bin = _load_ffmpeg()
 if not ffmpeg_bin:
  return None, "ffmpeg not available: " + (_FFMPEG.get("error") or "unknown")
 out_path = path + ".norm.wav"
 cmd = [
  ffmpeg_bin, "-y", "-loglevel", "error", "-i", path,
  "-ar", "16000", "-ac", "1",
  "-af", "highpass=f=100,lowpass=f=3800,dynaudnorm",
  out_path,
 ]
 try:
  subprocess.run(cmd, check=True, capture_output=True, timeout=300)
  return out_path, None
 except Exception as e:
  return None, str(e)[:200]


def _build_kwargs(opts, refine=False):
 kw = {
  "beam_size": max(opts["beam_size"], 5) if refine else opts["beam_size"],
  "best_of": opts["best_of"],
  "patience": opts["patience"],
  "language": opts["language"],
  "initial_prompt": opts["initial_prompt"],
  "hotwords": opts["hotwords"],
  "condition_on_previous_text": False,
  "temperature": 0.0,
  "compression_ratio_threshold": 2.4,
  "log_prob_threshold": -1.0,
  "no_speech_threshold": 0.6,
  "repetition_penalty": opts["repetition_penalty"],
  "no_repeat_ngram_size": opts["no_repeat_ngram_size"],
  "word_timestamps": True,
 }
 if refine:
  kw["vad_filter"] = False
 else:
  kw["vad_filter"] = True
  kw["vad_parameters"] = {
   "threshold": opts["vad_threshold"],
   "min_silence_duration_ms": opts["vad_min_silence_ms"],
   "speech_pad_ms": opts["vad_pad_ms"],
  }
 return kw


def _segment_to_dict(seg, offset=0.0):
 text = (seg.text or "").strip()
 words = []
 probs = []
 for w in (getattr(seg, "words", None) or []):
  token = (w.word or "").strip()
  if not token:
   continue
  p = float(getattr(w, "probability", 1.0) or 0.0)
  words.append([float(w.start) + offset, float(w.end) + offset, round(p, 3), token])
  probs.append(p)
 if not words:
  words.append([float(seg.start) + offset, float(seg.end) + offset, 1.0, text])
 avg_lp = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
 mean_p = (sum(probs) / len(probs)) if probs else 1.0
 return {
  "start": float(seg.start) + offset,
  "end": float(seg.end) + offset,
  "text": text,
  "words": words,
  "avg_logprob": round(avg_lp, 3),
  "no_speech_prob": round(float(getattr(seg, "no_speech_prob", 0.0) or 0.0), 3),
  "compression_ratio": round(float(getattr(seg, "compression_ratio", 0.0) or 0.0), 3),
  "mean_word_prob": round(mean_p, 3),
  "min_word_prob": round(min(probs), 3) if probs else 1.0,
  "low_conf": avg_lp < -0.8 or mean_p < 0.65,
  "refined": False,
 }


def _is_hallucination(d):
 if d["no_speech_prob"] > 0.6 and d["avg_logprob"] < -1.0:
  return True
 if d["compression_ratio"] > 2.6:
  return True
 if HALLUCINATION_RE.search(d["text"]) and d["mean_word_prob"] < 0.8:
  return True
 return False


def _merge_refined(orig, new_segs):
 words = []
 for x in new_segs:
  words.extend(x["words"])
 probs = [w[2] for w in words] or [1.0]
 avg_lp = sum(x["avg_logprob"] for x in new_segs) / len(new_segs)
 mean_p = sum(probs) / len(probs)
 merged = dict(orig)
 merged.update({
  "text": " ".join(x["text"] for x in new_segs).strip(),
  "words": words,
  "avg_logprob": round(avg_lp, 3),
  "mean_word_prob": round(mean_p, 3),
  "min_word_prob": round(min(probs), 3),
  "low_conf": avg_lp < -0.8 or mean_p < 0.65,
  "refined": True,
 })
 return merged


def _transcribe_file(model, path, opts, status_path):
 kwargs = _supported_kwargs(model.transcribe, _build_kwargs(opts))
 segments_gen, info = model.transcribe(path, **kwargs)
 duration = float(getattr(info, "duration", 0.0) or 0.0)
 scale = 80.0 if opts["refine_enabled"] else 100.0
 segments = []
 dropped = 0
 last_write = 0.0
 for seg in segments_gen:
  d = _segment_to_dict(seg)
  if d["text"]:
   if opts["hallucination_filter"] and _is_hallucination(d):
    dropped += 1
   else:
    segments.append(d)
  now = time.time()
  if duration and now - last_write > 5:
   _write_json(status_path, {
    "state": "running",
    "started": opts["started"],
    "progress": round(min(float(seg.end) / duration, 1.0) * scale, 1),
   })
   last_write = now
 info_dict = {
  "duration": duration,
  "language": getattr(info, "language", None),
  "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
 }
 return segments, info_dict, dropped


def _refine_segments(path, segments, opts, status_path):
 if not opts["refine_enabled"] or not segments:
  return segments, 0
 decode_audio = _get_decoder()
 if decode_audio is None:
  return segments, 0
 head = float(opts.get("refine_head_sec") or 0)
 tail = float(opts.get("refine_tail_sec") or 0)
 last_end = segments[-1]["end"]
 targets = set()
 for i, s in enumerate(segments):
  if s["low_conf"]:
   targets.add(i)
  elif head and s["start"] <= head:
   targets.add(i)
  elif tail and s["end"] >= last_end - tail:
   targets.add(i)
 targets = sorted(targets)[:opts["refine_max"]]
 if not targets:
  return segments, 0
 audio = decode_audio(path, sampling_rate=_SAMPLE_RATE)
 model = _get_model(opts["refine_model"], opts["device"], opts["compute_type"], opts["threads"], 2)
 kwargs = _supported_kwargs(model.transcribe, _build_kwargs(opts, refine=True))
 improved = 0
 for n, i in enumerate(targets):
  s = segments[i]
  a = max(0.0, s["start"] - 0.15)
  b = s["end"] + 0.15
  clip = audio[int(a * _SAMPLE_RATE): int(b * _SAMPLE_RATE)]
  if len(clip) < int(_SAMPLE_RATE * 0.3):
   continue
  try:
   gen, _ = model.transcribe(clip, **kwargs)
   new = [_segment_to_dict(x, offset=a) for x in gen]
   new = [x for x in new if x["text"]]
  except Exception:
   continue
  if not new:
   continue
  candidate = _merge_refined(s, new)
  better = candidate["mean_word_prob"] >= s["mean_word_prob"] + 0.02
  fixed = s["low_conf"] and not candidate["low_conf"]
  if better or fixed:
   segments[i] = candidate
   improved += 1
  _write_json(status_path, {
   "state": "refining",
   "started": opts["started"],
   "progress": round(80 + 20 * (n + 1) / len(targets), 1),
  })
 return segments, improved


def _track_job(key, path, opts, delete_after):
 cache_file = os.path.join(_CACHE_DIR, key + ".json")
 status_file = os.path.join(_CACHE_DIR, key + ".status")
 temp_paths = []
 try:
  with _TRACK_SEMAPHORE:
   _write_json(status_file, {"state": "running", "started": opts["started"], "progress": 0.0})
   work_path = path
   preprocess_error = None
   if opts["preprocess"]:
    new_path, preprocess_error = _preprocess_audio(path)
    if new_path:
     work_path = new_path
     temp_paths.append(new_path)
   model = _get_model(opts["model_size"], opts["device"], opts["compute_type"], opts["threads"], 2)
   segments, info, dropped = _transcribe_file(model, work_path, opts, status_file)
   segments, refined = _refine_segments(work_path, segments, opts, status_file)
   info["hallucinations_dropped"] = dropped
   info["segments_refined"] = refined
   info["preprocess_error"] = preprocess_error
   info["preprocessed"] = work_path != path
   info["engine_source"] = _FW.get("source") or "unknown"
   info["engine_version"] = _FW.get("version") or "unknown"
   _write_json(cache_file, {"segments": segments, "info": info})
   _write_json(status_file, {"state": "done", "started": opts["started"], "progress": 100.0})
 except Exception as e:
  try:
   _write_json(status_file, {"state": "error", "started": opts["started"], "error": str(e)})
  except Exception:
   pass
 finally:
  cleanup = list(temp_paths)
  if delete_after:
   cleanup.append(path)
  for p in cleanup:
   try:
    os.remove(p)
   except Exception:
    pass
  with _JOBS_LOCK:
   _JOBS.pop(key, None)


# ================= signal detection =================

HOLD_ANNOUNCE_RE = re.compile(
 r"((?:place|put|keep|keeping|placing|putting)\s+"
 r"(?:you|the call|your call|a call|this call|the line)?\s*"
 r"on\s+(?:a\s+)?(?:brief\s+|short\s+|quick\s+)?hold"
 r"|hold\s+the\s+line"
 r"|on\s+hold\s+for)",
 re.IGNORECASE,
)

HOLD_RETURN_RE = re.compile(
 r"(thank you for (?:holding|waiting|being on hold|your patience)|"
 r"thanks for (?:holding|waiting|your patience)|"
 r"sorry (?:for|about) the (?:wait|delay)|taking (?:a bit |a little )?longer|"
 r"are you (?:still )?there|still with me|i'?m back|\bwaiting\b)",
 re.IGNORECASE,
)

CREDENTIAL_TRIGGERS = re.compile(
 r"(passwords?|pass\s?codes?|passphrase|\bpin\b|"
 r"temporary password|your username is|username is|"
 r"log\s?in details|credentials|security (?:code|answer|question)|"
 r"one[- ]time (?:code|password)|\botp\b|verification code|activation code)",
 re.IGNORECASE,
)

AGENT_CUES = [
 "my name is", "thank you for calling", "thank you for contacting", "one moment",
 "i'll be glad", "i will be glad", "could you help me", "can you confirm",
 "please note", "is there anything else", "have a wonderful day",
 "have a great day", "thank you for being on hold", "bear with me",
 "let me check", "i can help", "happy to help", "may i know",
 "thank you for confirming", "could you please", "help me with",
 "place you on hold", "your username is", "your password is",
 "just give me", "allow me",
]

IVR_CUES = [
 "your call may be recorded", "for training purposes", "may be monitored",
 "quality control", "quality purposes", "next available",
 "please have your", "connect you to", "all our agents",
 "press one", "press two", "calls are recorded",
]

_NUM_WORDS = {
 "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
 "fifteen": 15, "twenty": 20, "thirty": 30, "couple": 2, "few": 3,
}

_SPELLED_DIGIT = r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|double|triple)\b"

_PHONETIC_RUN = re.compile(r"((?:\b[\w']+\s+as\s+(?:in|an)\s+[\w']+[\s,.\-]*){2,})", re.IGNORECASE)

_LOOP_RE = re.compile(r"\b((?:\w+\W+){1,5}?)(?:\1){2,}", re.IGNORECASE)

HOLD_LABEL = "[during hold - not agent-facing]"


def _is_noise(text, prob):
 core = re.sub(r"[^A-Za-z0-9]", "", str(text or ""))
 if len(core) <= 1:
  return True
 if len(core) <= 2 and float(prob or 0.0) < 0.6:
  return True
 return False


def _split_utterances(segments, max_gap=0.8, max_len=12.0):
 out = []
 for seg in segments:
  words = seg.get("words") or []
  if len(words) < 2 or any(len(w) < 4 for w in words):
   if not _is_noise(seg.get("text"), seg.get("mean_word_prob", 1.0)):
    out.append(seg)
   continue
  parts = []
  cur = [words[0]]
  for prev, w in zip(words, words[1:]):
   gap = w[0] - prev[1]
   sentence_end = str(prev[3]).endswith((".", "?", "!"))
   too_long = w[1] - cur[0][0] > max_len
   if gap > max_gap or (sentence_end and gap > 0.15) or too_long:
    parts.append(cur)
    cur = [w]
   else:
    cur.append(w)
  parts.append(cur)
  for p in parts:
   probs = [float(w[2]) for w in p] or [1.0]
   item = dict(seg)
   item["start"] = p[0][0]
   item["end"] = p[-1][1]
   item["words"] = p
   item["text"] = " ".join(str(w[3]) for w in p).strip()
   item["mean_word_prob"] = round(sum(probs) / len(probs), 3)
   item["low_conf"] = item["mean_word_prob"] < 0.65
   if _is_noise(item["text"], item["mean_word_prob"]):
    continue
   out.append(item)
 return out


def _collapse_loops(text):
 out, n = _LOOP_RE.subn(r"\1", text + " ")
 return out.strip(), n


def _apply_glossary(text, glossary_text):
 if not glossary_text:
  return text
 for line in glossary_text.splitlines():
  if "=>" not in line:
   continue
  wrong, right = line.split("=>", 1)
  wrong = wrong.strip()
  right = right.strip()
  if not wrong:
   continue
  text = re.sub(r"\b" + re.escape(wrong) + r"\b", right, text, flags=re.IGNORECASE)
 return text


def _mark_uncertain(seg, threshold):
 tokens = []
 for w in seg.get("words") or []:
  if len(w) < 4 or not w[3]:
   return seg["text"]
  token = w[3]
  if w[2] < threshold:
   token += "(?)"
  tokens.append(token)
 return " ".join(tokens) if tokens else seg["text"]


def redact_text(text):
 hits = []
 out = text
 if _PHONETIC_RUN.search(out) and len(re.findall(r"\bas\s+(?:in|an)\b", out, re.IGNORECASE)) >= 2:
  out = _PHONETIC_RUN.sub(" [REDACTED_SPELLED_CREDENTIAL] ", out)
  hits.append("phonetic_credential")
 if CREDENTIAL_TRIGGERS.search(out):
  out = CREDENTIAL_TRIGGERS.sub(lambda m: m.group(0) + " [REDACTED]", out)
  hits.append("credential_phrase")
 out, n = re.subn(r"\b\d{4,}\b", "[REDACTED_NUMBER]", out)
 if n:
  hits.append("numeric_id")
 out, n = re.subn(r"\b(?=[A-Za-z0-9]{5,})(?=[A-Za-z0-9]*\d)[A-Za-z0-9]+\b", "[REDACTED_ID]", out)
 if n:
  hits.append("alphanumeric_id")
 if len(re.findall(_SPELLED_DIGIT, out, re.IGNORECASE)) >= 4:
  out = re.sub(
   r"((?:" + _SPELLED_DIGIT + r"[\s,.\-]*){4,})",
   " [REDACTED_SPOKEN_DIGITS] ",
   out,
   flags=re.IGNORECASE,
  )
  hits.append("spoken_digits")
 out, n = re.subn(r"\b[\w.\-+]+\s?(?:@|\bat\b)\s?[\w.\-]+\.\w{2,}\b", "[REDACTED_EMAIL]", out)
 if n:
  hits.append("email")
 out = re.sub(r"\s{2,}", " ", out).strip()
 return out, hits


def _promised_seconds(text):
 m = re.search(
  r"(\d+|a|an|one|two|three|four|five|ten|fifteen|twenty|couple|few)"
  r"[\s\-]*(?:more\s+)?(?:of\s+)?(minute|minutes|min|second|seconds|sec)",
  text,
  re.IGNORECASE,
 )
 if not m:
  return None
 raw = m.group(1).lower()
 unit = m.group(2).lower()
 val = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
 if val is None:
  return None
 return val * 60 if unit.startswith("min") else val


def _ts(seconds):
 m, s = divmod(int(max(seconds, 0)), 60)
 return "%02d:%02d" % (m, s)


def _dur(seconds):
 m, s = divmod(int(max(seconds, 0)), 60)
 if m:
  return "%dm %02ds" % (m, s)
 return "%ds" % s


def _agent_score(segments):
 score = 0
 for s in segments:
  low = s["text"].lower()
  for cue in AGENT_CUES:
   if cue in low:
    score += 1
 return score


def _guess_speaker(text, prev):
 low = text.lower()
 if any(c in low for c in AGENT_CUES):
  return "AGENT"
 if len(text.split()) <= 6:
  return "CALLER" if prev in ("AGENT", "IVR") else (prev or "CALLER")
 if prev == "AGENT":
  return "CALLER"
 return "AGENT"


def _overlap_seconds(a, b):
 a = sorted(a)
 b = sorted(b)
 i = 0
 j = 0
 total = 0.0
 while i < len(a) and j < len(b):
  start = max(a[i][0], b[j][0])
  end = min(a[i][1], b[j][1])
  if end > start:
   total += end - start
  if a[i][1] < b[j][1]:
   i += 1
  else:
   j += 1
 return total


#============== helper function ==============

def _split_stereo(path):
 decode = _get_decoder()
 if decode is None:
  return None
 try:
  left, right = decode(path, sampling_rate=_SAMPLE_RATE, split_stereo=True)
 except Exception:
  return None
 import wave
 import numpy as np
 n = min(len(left), len(right))
 if n == 0 or np.allclose(left[:n], right[:n], atol=1e-4):
  return None
 out = []
 for i, ch in enumerate((left, right)):
  p = str(path) + ".ch" + str(i) + ".wav"
  pcm = (np.clip(ch, -1.0, 1.0) * 32767).astype(np.int16)
  with wave.open(p, "wb") as w:
   w.setnchannels(1)
   w.setsampwidth(2)
   w.setframerate(_SAMPLE_RATE)
   w.writeframes(pcm.tobytes())
  out.append(p)
 return out

# ================= component =================


class CustomComponent(Component):

 display_name = "Service Desk QA Transcriber"

 description = (

  "High-accuracy dual-channel call transcription with confidence scoring, second-pass "

  "refinement, Genesys hold events and an audit-ready payload."

 )

 inputs = [

  DataInput(name="recording_data", display_name="Recording Data", required=False,

   info="Connect the Audio Files output of the Genesys Recording Downloader."),

  FileInput(name="audio_file", display_name="Audio File (manual test)",

   file_types=["mp3", "wav", "webm", "m4a", "ogg", "flac"], required=False),

  MultilineInput(name="test_metadata", display_name="Test Call Metadata (JSON, manual tests only)", value="",

   advanced=True,

   info='Used only with Audio File. Example: {"conversation_id":"TEST-INC90412738","start_time":"2026-10-14T13:10:00Z",'

    '"end_time":"2026-10-14T13:11:45Z","agents":[{"name":"Faiz Sayyed","email":"faiz.sayyed@test-sandbox.local"}]}'),

  DropdownInput(name="run_mode", display_name="Run Mode",

   options=["background (UI-safe)", "wait until done (webhook/production)"],

   value="background (UI-safe)"),

  IntInput(name="ui_wait_sec", display_name="Max Wait in Background Mode (seconds)", value=90, advanced=True),

  IntInput(name="max_wait_min", display_name="Max Wait in Wait Mode (minutes)", value=30, advanced=True),

  DropdownInput(name="model_size", display_name="Model Size",

   options=["small.en", "medium.en", "distil-small.en", "distil-medium.en",

    "distil-large-v3", "large-v3", "large-v3-turbo"],

   value="large-v3-turbo"),

  DropdownInput(name="speaker_mapping", display_name="Speaker Mapping",

   options=["auto", "track 0 is agent", "track 1 is agent"], value="auto"),

  BoolInput(name="redact_pii", display_name="Redact Credentials / PII", value=False),

  BoolInput(name="use_genesys_holds", display_name="Use Genesys Hold Events", value=True),

  IntInput(name="hold_threshold", display_name="Silence Hold Threshold (seconds)", value=30,

   info="Only used when Genesys hold data is unavailable."),

  IntInput(name="hold_checkin_sec", display_name="Hold Update Interval (seconds)", value=120,

   info="CEQ: update the caller about every 2 minutes while on hold."),

  IntInput(name="dead_air_checkin_sec", display_name="Dead Air Update Interval (seconds)", value=30,

   info="CEQ: update the caller about every 30 seconds of dead air."),

  IntInput(name="beam_size", display_name="Beam Size", value=5, advanced=True),

  IntInput(name="best_of", display_name="Best Of", value=5, advanced=True),

  StrInput(name="patience", display_name="Beam Patience", value="1.0", advanced=True),

  BoolInput(name="refine_low_confidence", display_name="Second Pass Refinement", value=True),

  DropdownInput(name="refine_model", display_name="Second-Pass Model",

   options=["large-v3", "large-v3-turbo", "distil-large-v3"], value="large-v3", advanced=True),

  IntInput(name="refine_max", display_name="Max Segments to Refine per Track", value=40, advanced=True),

  IntInput(name="refine_head_sec", display_name="Always Refine First N Seconds", value=60, advanced=True),

  IntInput(name="refine_tail_sec", display_name="Always Refine Last N Seconds", value=30, advanced=True),

  BoolInput(name="hallucination_filter", display_name="Drop Hallucinated Segments", value=True, advanced=True),

  StrInput(name="vad_threshold", display_name="VAD Speech Threshold", value="0.4", advanced=True),

  IntInput(name="vad_pad_ms", display_name="VAD Speech Padding (ms)", value=400, advanced=True),

  IntInput(name="vad_min_silence_ms", display_name="VAD Min Silence (ms)", value=500, advanced=True),

  StrInput(name="repetition_penalty", display_name="Repetition Penalty", value="1.0", advanced=True),

  IntInput(name="no_repeat_ngram_size", display_name="No-Repeat N-gram Size", value=0, advanced=True),

  BoolInput(name="preprocess_audio", display_name="Preprocess Audio (ffmpeg)", value=False, advanced=True),

  BoolInput(name="use_dynamic_hotwords", display_name="Auto Hotwords (agent name)", value=True, advanced=True),

  MultilineInput(name="extra_hotwords", display_name="Extra Hotwords", advanced=True,

   value=("Altera, ServiceNow, Duo, VPN, Active Directory, ANSOS, Time and Attendance, MTA, ATA, "

    "Web Scheduler, Time Card, Employee ID, Unlock, Password Reset, Kronos, Workday, "

    "Citrix, Epic, Teams, Outlook, Marin General")),

  MultilineInput(name="correction_glossary", display_name="Correction Glossary", advanced=True,

   value=("marine general=>Marin General\nmarine health=>Marin Health\ndual app=>Duo app\n"

    "service now=>ServiceNow\nansof=>ANSOS\nanthos=>ANSOS\nsign card=>time card")),

  BoolInput(name="mark_uncertain_words", display_name="Mark Uncertain Words (?)", value=True, advanced=True),

  StrInput(name="uncertain_word_threshold", display_name="Uncertain Word Threshold", value="0.45", advanced=True),

  StrInput(name="review_confidence_threshold", display_name="Review Below Confidence", value="0.80", advanced=True),

  IntInput(name="cache_hours", display_name="Cache Retention (hours)", value=24, advanced=True),

  DropdownInput(name="device", display_name="Device", options=["auto", "cpu", "cuda"], value="auto", advanced=True),

  DropdownInput(name="compute_type", display_name="Compute Type",

   options=["int8", "int8_float16", "float16", "float32"], value="int8", advanced=True),

  IntInput(name="cpu_threads", display_name="CPU Threads per Track", value=8, advanced=True),

  StrInput(name="language", display_name="Language", value="en", advanced=True),

  IntInput(name="dead_air_threshold", display_name="Dead Air Minimum Gap (seconds)", value=10, advanced=True),

  BoolInput(name="label_speakers", display_name="Heuristic Labels (single-track only)", value=True, advanced=True),

  StrInput(name="turn_gap", display_name="Turn Merge Gap (seconds)", value="1.5", advanced=True),

  BoolInput(name="delete_audio_after", display_name="Delete Audio Files After Transcription", value=True),

  MultilineInput(name="initial_prompt", display_name="Domain Prompt", advanced=True,

   info="Vocabulary only. Never put example dialogue here.",

   value=("Altera service desk support call. Thank you for contacting the service desk. "

    "First and last name. Callback number and extension. Employee ID. "

    "ANSOS Time and Attendance. Web Scheduler. Password reset. Account unlock. ServiceNow incident.")),

  MultilineInput(name="audit_instructions", display_name="Audit Mission Instructions",

   value=("Audit the call below using your CEQ policy, together with the TICKET MATCH and "

    "EXPECTED PROCEDURE context. Return JSON only, in your defined schema.")),

  IntInput(name="max_transcript_chars", display_name="Max Transcript Characters", value=60000, advanced=True),

 ]

 outputs = [

  Output(display_name="Mission Payload", name="mission_payload", method="build_mission_payload", group_outputs=True),

  Output(display_name="QA Report", name="report", method="build_report", group_outputs=True),

  Output(display_name="Structured Data", name="structured", method="build_structured", group_outputs=True),

  Output(display_name="Engine Status", name="engine_status", method="build_engine_status", group_outputs=True),

 ]

 # ---------------- engine status ----------------

 def build_engine_status(self) -> Message:
  needs_warmup = _FW["module"] is None or not _FFMPEG["path"]
  if needs_warmup and not _FW["warming"]:
   _FW["warming"] = True
   threading.Thread(target=_warm_up, daemon=True).start()
  deadline = time.time() + 60
  while _FW["warming"] and time.time() < deadline:
   time.sleep(2)
  models_dir = _persist_dir("_whisper_models")
  lines = ["TRANSCRIPTION ENGINE STATUS"]
  if _FW["module"] is not None:
   lines.append("faster-whisper: READY (" + _FW["source"] + ", version " + _FW["version"] + ")")
  elif _FW["warming"]:
   lines.append("faster-whisper: INSTALLING in the background. Run again in 2-3 minutes.")
  else:
   lines.append("faster-whisper: NOT AVAILABLE - " + (_FW["error"] or "unknown error"))
  if _FFMPEG["path"]:
   lines.append("ffmpeg: READY (" + _FFMPEG["source"] + ") at " + _FFMPEG["path"])
  else:
   lines.append("ffmpeg: NOT AVAILABLE - " + (_FFMPEG["error"] or "unknown") + " (keep Preprocess Audio off)")
  lines.append("Package folder: " + _persist_dir("_pylibs"))
  lines.append("Model folder: " + models_dir)
  try:
   cached = [d for d in os.listdir(models_dir) if not d.startswith(".")]
  except Exception:
   cached = []
  lines.append("Cached model files: " + (", ".join(cached[:6]) or "none yet"))
  lines.append("Preprocess Audio: " + ("ON" if self.preprocess_audio else "off"))
  return Message(text="\n".join(lines))

 # ---------------- input handling ----------------

  
 # ---------------- input handling ----------------

 def _resolve_inputs(self):
  meta = {}
  tracks = []

  rd = self.recording_data

  if isinstance(rd, list) and rd:
   rd = rd[0]

  if hasattr(rd, "data"):
   rd = rd.data or {}

  if isinstance(rd, dict) and rd:
   if rd.get("error"):
    raise RuntimeError(
     "Downloader error: "
     + str(rd.get("error"))
     + " - "
     + str(rd.get("detail", ""))
    )

   meta = rd

   for t in rd.get("tracks", []) or []:
    if t.get("file_path") and os.path.exists(t["file_path"]):
     tracks.append({
      "track": str(t.get("track")),
      "path": t["file_path"]
     })

   if (
    not tracks
    and rd.get("file_path")
    and os.path.exists(rd["file_path"])
   ):
    tracks.append({
     "track": "0",
     "path": rd["file_path"]
    })

  if not tracks and self.audio_file:
   path = self.audio_file

   if isinstance(path, list) and path:
    path = path[0]

   channels = _split_stereo(str(path))

   if channels:
    tracks = [
     {"track": "0", "path": channels[0]},
     {"track": "1", "path": channels[1]},
    ]
   else:
    tracks = [{
     "track": "0",
     "path": str(path),
     "keep": True,
    }]

   raw = (self.test_metadata or "").strip()

   if raw and not meta:
    try:
     meta = json.loads(raw)
    except ValueError:
     meta = {}

  if not tracks:
   raise RuntimeError(
    "No audio found. Re-run the whole flow so the Downloader fetches the files again."
   )

  return tracks, meta

 @staticmethod
 def _file_hash(path):
  h = hashlib.md5()
  with open(path, "rb") as f:
   for chunk in iter(lambda: f.read(1048576), b""):
    h.update(chunk)
  return h.hexdigest()

 def _pick_device(self):
  device = self.device or "auto"
  compute_type = self.compute_type or "int8"
  if device == "auto":
   try:
    import ctranslate2
    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
   except Exception:
    device = "cpu"
  if device == "cuda" and compute_type == "int8":
   compute_type = "float16"
  if device == "cpu" and compute_type in ("float16", "int8_float16"):
   compute_type = "int8"
  return device, compute_type

 def _pick_agent_track(self, per_track):
  if len(per_track) < 2:
   return 0, "n/a (single track)"
  mapping = self.speaker_mapping or "auto"
  if mapping == "track 0 is agent":
   return 0, "manual"
  if mapping == "track 1 is agent":
   return 1, "manual"
  s0 = _agent_score(per_track[0]["segments"])
  s1 = _agent_score(per_track[1]["segments"])
  if s0 == s1:
   return 0, "low (tie - verify and set mapping manually)"
  idx = 0 if s0 > s1 else 1
  conf = "high" if abs(s0 - s1) >= 3 else "medium"
  return idx, conf

 def _build_hotwords(self, meta):
  parts = []
  if self.use_dynamic_hotwords:
   for a in meta.get("agents") or []:
    if a.get("name"):
     parts.append(a["name"])
  if self.extra_hotwords:
   parts += [h.strip() for h in self.extra_hotwords.replace("\n", ",").split(",") if h.strip()]
  parts = list(dict.fromkeys(parts))
  return ", ".join(parts) or None

 # ---------------- job management ----------------

 def _purge_cache(self):
  if not os.path.isdir(_CACHE_DIR):
   return
  max_age = max(1, _to_int(self.cache_hours, 24)) * 3600
  now = time.time()
  for name in os.listdir(_CACHE_DIR):
   path = os.path.join(_CACHE_DIR, name)
   try:
    if now - os.path.getmtime(path) > max_age:
     os.remove(path)
   except Exception:
    pass

 @staticmethod
 def _job_key(file_hash, opts):
  relevant = {k: v for k, v in opts.items() if k not in ("threads", "started")}
  key = file_hash + "|" + json.dumps(relevant, sort_keys=True, default=str)
  return hashlib.md5(key.encode("utf-8")).hexdigest()

 def _paths(self, key):
  return (os.path.join(_CACHE_DIR, key + ".json"), os.path.join(_CACHE_DIR, key + ".status"))

 def _ensure_jobs(self, tracks, opts_base):
  for t in tracks:
   t["job_started"] = False
   cache_file, status_file = self._paths(t["key"])
   if os.path.exists(cache_file):
    continue
   with _JOBS_LOCK:
    if t["key"] in _JOBS:
     continue
   status = _read_json(status_file)
   if status and status.get("state") == "error":
    continue
   if status and status.get("state") in ("running", "refining"):
    try:
     age = time.time() - os.path.getmtime(status_file)
    except Exception:
     age = _STALE_SECONDS + 1
    if age < _STALE_SECONDS:
     continue
   opts = dict(opts_base)
   opts["started"] = time.time()
   _write_json(status_file, {"state": "running", "started": opts["started"], "progress": 0.0})
   thread = threading.Thread(
    target=_track_job,
    args=(t["key"], t["path"], opts, bool(self.delete_audio_after) and not t.get("keep")),
    daemon=True,
   )
   with _JOBS_LOCK:
    _JOBS[t["key"]] = thread
   thread.start()
   t["job_started"] = True

 def _track_state(self, t):
  cache_file, status_file = self._paths(t["key"])
  if os.path.exists(cache_file):
   return "done", 100.0, None
  status = _read_json(status_file) or {}
  state = status.get("state", "queued")
  if state == "error":
   try:
    os.remove(status_file)
   except Exception:
    pass
   return "error", 0.0, status.get("error")
  return state, float(status.get("progress") or 0.0), None

 # ---------------- main ----------------

 def _run(self):
  if getattr(self, "_cached_result", None) is not None:
   return self._cached_result
  t0 = time.time()
  os.makedirs(_CACHE_DIR, exist_ok=True)
  self._purge_cache()
  tracks, meta = self._resolve_inputs()
  for t in tracks:
   t["hash"] = self._file_hash(t["path"])
  duplicate_tracks = False
  if len(tracks) == 2 and tracks[0]["hash"] == tracks[1]["hash"]:
   duplicate_tracks = True
   tracks = tracks[:1]
  device, compute_type = self._pick_device()
  threads = _to_int(self.cpu_threads, 0)
  if threads == 0 and device == "cpu":
   threads = max(1, (os.cpu_count() or 4) // 2)
  opts_base = {
   "model_size": self.model_size,
   "device": device,
   "compute_type": compute_type,
   "threads": threads,
   "beam_size": max(1, _to_int(self.beam_size, 5)),
   "best_of": max(1, _to_int(self.best_of, 5)),
   "patience": _to_float(self.patience, 1.0),
   "language": (self.language or "").strip() or None,
   "initial_prompt": (self.initial_prompt or "").strip() or None,
   "hotwords": self._build_hotwords(meta),
   "repetition_penalty": _to_float(self.repetition_penalty, 1.0),
   "no_repeat_ngram_size": max(0, _to_int(self.no_repeat_ngram_size, 0)),
   "vad_threshold": _to_float(self.vad_threshold, 0.4),
   "vad_pad_ms": _to_int(self.vad_pad_ms, 400),
   "vad_min_silence_ms": _to_int(self.vad_min_silence_ms, 500),
   "hallucination_filter": bool(self.hallucination_filter),
   "refine_enabled": bool(self.refine_low_confidence),
   "refine_model": self.refine_model or "large-v3",
   "refine_max": max(0, _to_int(self.refine_max, 40)),
   "refine_head_sec": max(0, _to_int(self.refine_head_sec, 60)),
   "refine_tail_sec": max(0, _to_int(self.refine_tail_sec, 30)),
   "preprocess": bool(self.preprocess_audio),
  }
  for t in tracks:
   t["key"] = self._job_key(t["hash"], opts_base)
  self._ensure_jobs(tracks, opts_base)
  if str(self.run_mode or "").startswith("wait"):
   limit = max(1, _to_int(self.max_wait_min, 30)) * 60
  else:
   limit = max(5, _to_int(self.ui_wait_sec, 90))
  deadline = t0 + limit
  while True:
   states = [self._track_state(t) for t in tracks]
   errors = [s[2] for s in states if s[0] == "error"]
   if errors:
    raise RuntimeError("Transcription failed: " + str(errors[0]) + " (re-run to retry)")
   if all(s[0] == "done" for s in states):
    break
   if time.time() >= deadline:
    progress = []
    for t, s in zip(tracks, states):
     progress.append({"track": t["track"], "state": s[0], "progress_pct": s[1]})
    overall = round(sum(s[1] for s in states) / len(states), 1)
    return {
     "status": "PROCESSING",
     "conversation_id": meta.get("conversation_id"),
     "overall_progress_pct": overall,
     "tracks": progress,
     "detail": "Transcription is running in the background. Re-run the flow in 1-2 minutes.",
    }
   time.sleep(2)
  for t in tracks:
   if self.delete_audio_after and not t.get("job_started") and not t.get("keep"):
    try:
     os.remove(t["path"])
    except Exception:
     pass
  results = []
  for t in tracks:
   cache_file, _ = self._paths(t["key"])
   data = _read_json(cache_file)
   if not data:
    raise RuntimeError("Cached transcript missing for track " + t["track"] + ". Re-run the flow.")
   results.append(data)
  result = self._analyse(tracks, results, meta, duplicate_tracks, device, threads, t0, opts_base)
  result["cache_keys"] = [t["key"] for t in tracks]
  result["audio_paths"] = [t["path"] for t in tracks]
  self._cached_result = result
  return result

 # ---------------- analysis ----------------

 def _analyse(self, tracks, results, meta, duplicate_tracks, device, threads, t0, opts):
  per_track = []
  audio_duration = 0.0
  language = None
  language_prob = 0.0
  hallucinations_dropped = 0
  segments_refined = 0
  preprocess_errors = []
  preprocessed_tracks = 0
  engine_source = "unknown"
  engine_version = "unknown"
  for t, res in zip(tracks, results):
   per_track.append({"track": t["track"], "segments": res.get("segments", [])})
   info = res.get("info") or {}
   audio_duration = max(audio_duration, float(info.get("duration") or 0.0))
   hallucinations_dropped += int(info.get("hallucinations_dropped") or 0)
   segments_refined += int(info.get("segments_refined") or 0)
   if info.get("preprocessed"):
    preprocessed_tracks += 1
   if info.get("preprocess_error"):
    preprocess_errors.append(str(info.get("preprocess_error")))
   if info.get("engine_source"):
    engine_source = info.get("engine_source")
    engine_version = info.get("engine_version") or "unknown"
   if language is None:
    language = info.get("language")
    language_prob = float(info.get("language_probability") or 0.0)

  dual = len(per_track) == 2
  agent_index, mapping_confidence = self._pick_agent_track(per_track)
  uncertain_t = _to_float(self.uncertain_word_threshold, 0.45)
  review_t = _to_float(self.review_confidence_threshold, 0.80)

  # ---- label segments ----
  loops_collapsed = 0
  all_segs = []
  for idx, pt in enumerate(per_track):
   for s in _split_utterances(pt["segments"]):
    if dual:
     speaker = "AGENT" if idx == agent_index else "CALLER"
    else:
     speaker = ""
    text, n = _collapse_loops(s["text"])
    loops_collapsed += n
    corrected = _apply_glossary(text, self.correction_glossary)
    display = corrected
    if self.mark_uncertain_words and n == 0 and corrected == s["text"]:
     display = _mark_uncertain(s, uncertain_t)
    if s["start"] < 60 and any(c in corrected.lower() for c in IVR_CUES):
     speaker = "IVR"
    item = dict(s)
    item["text"] = corrected
    item["display_text"] = display
    item["speaker"] = speaker
    all_segs.append(item)
  all_segs.sort(key=lambda x: x["start"])
  if not dual and self.label_speakers:
   prev = ""
   for s in all_segs:
    if s["speaker"] != "IVR":
     s["speaker"] = _guess_speaker(s["text"], prev)
    prev = s["speaker"]

  # ---- word intervals and confidence ----
  all_words = []
  agent_words = []
  caller_words = []
  word_probs = []
  agent_probs = []
  caller_probs = []
  for s in all_segs:
   for w in s["words"]:
    pair = (float(w[0]), float(w[1]))
    all_words.append(pair)
    prob = float(w[2]) if len(w) > 2 else 1.0
    word_probs.append(prob)
    if s["speaker"] == "AGENT":
     agent_words.append(pair)
     agent_probs.append(prob)
    elif s["speaker"] == "CALLER":
     caller_words.append(pair)
     caller_probs.append(prob)
  all_words.sort()

  def _mean(values):
   return round(sum(values) / len(values), 3) if values else None

  transcript_confidence = _mean(word_probs)
  uncertain_words = sum(1 for p in word_probs if p < uncertain_t)
  uncertain_pct = round(uncertain_words / len(word_probs) * 100, 1) if word_probs else 0.0
  review_reasons = []
  if transcript_confidence is not None and transcript_confidence < review_t:
   review_reasons.append("mean word confidence " + str(transcript_confidence))
  if uncertain_pct > 10:
   review_reasons.append(str(uncertain_pct) + "% uncertain words")
  if language_prob and language_prob < 0.8:
   review_reasons.append("low language confidence")

  # ---- silence gaps ----
  hold_t = _to_float(self.hold_threshold, 30.0)
  dead_t = _to_float(self.dead_air_threshold, 10.0)
  hold_checkin = _to_float(self.hold_checkin_sec, 120.0)
  dead_checkin = _to_float(self.dead_air_checkin_sec, 30.0)
  gaps = []
  cur_end = None
  for ws, we in all_words:
   if cur_end is not None and ws - cur_end >= dead_t:
    gaps.append((cur_end, ws, ws - cur_end))
   cur_end = we if cur_end is None else max(cur_end, we)

  # ---- holds ----
  use_genesys = bool(self.use_genesys_holds) and bool(meta.get("hold_data_available"))
  hold_events = []
  dead_air_events = []
  long_silences = []
  if use_genesys:
   for h in meta.get("genesys_holds") or []:
    hold_events.append({
     "start": float(h["start"]),
     "end": float(h["end"]),
     "duration": round(float(h["duration"]), 1),
     "source": "genesys",
    })
   for gs, ge, gd in gaps:
    covered = 0.0
    for h in hold_events:
     covered += max(0.0, min(ge, h["end"]) - max(gs, h["start"]))
    if covered >= gd * 0.5:
     continue
    ev = {"start": gs, "end": ge, "duration": round(gd, 1)}
    if gd >= dead_checkin:
     long_silences.append(ev)
    else:
     dead_air_events.append(ev)
  else:
   for gs, ge, gd in gaps:
    ev = {"start": gs, "end": ge, "duration": round(gd, 1)}
    if gd >= hold_t:
     ev["source"] = "silence"
     hold_events.append(ev)
    elif gd >= dead_checkin:
     long_silences.append(ev)
    else:
     dead_air_events.append(ev)
  hold_events.sort(key=lambda e: e["start"])
  genesys_holds = [h for h in hold_events if h.get("source") == "genesys"]

  def _during_hold(t):
   for h in genesys_holds:
    if h["start"] + 1 <= t <= h["end"] - 1:
     return True
   return False

  # ---- phrase signals ----
  hold_announcements = []
  hold_returns = []
  credential_events = []
  repeats = []
  low_conf_count = 0
  seen = {}
  for s in all_segs:
   raw_lower = s["text"].lower()
   if s["speaker"] == "AGENT":
    m = HOLD_ANNOUNCE_RE.search(s["text"])
    if m and not HOLD_RETURN_RE.search(s["text"]):
     promised = _promised_seconds(s["text"])
     if promised is not None and promised < 30:
      promised = None
     hold_announcements.append({"time": s["start"], "phrase": m.group(0).lower(), "promised_sec": promised})
    r = HOLD_RETURN_RE.search(s["text"])
    if r:
     hold_returns.append({"time": s["start"], "phrase": r.group(0).lower()})
    if CREDENTIAL_TRIGGERS.search(s["text"]):
     credential_events.append({"start": s["start"], "end": s["end"]})
   if s.get("low_conf"):
    low_conf_count += 1
   norm = s["speaker"] + ":" + re.sub(r"[^\w\s]", "", raw_lower).strip()
   if len(norm) > 16:
    if norm in seen:
     repeats.append({"time": s["start"], "first_seen": seen[norm]})
    else:
     seen[norm] = s["start"]

  # ---- hold evaluation ----
  discrepancies = []
  no_checkin = []
  agent_starts = sorted(s["start"] for s in all_segs if s["speaker"] == "AGENT")
  for h in hold_events:
   ann = None
   for a in hold_announcements:
    if -10 <= (h["start"] - a["time"]) <= 60:
     ann = a
   ack = None
   for r in hold_returns:
    if h["end"] - 5 <= r["time"] <= h["end"] + 20:
     ack = r
     break
   h["announced_at"] = ann["time"] if ann else None
   h["announced_phrase"] = ann["phrase"] if ann else None
   h["promised_sec"] = ann["promised_sec"] if ann else None
   h["acknowledged_phrase"] = ack["phrase"] if ack else None
   inside = [t for t in agent_starts if h["start"] + 1 < t < h["end"] - 1]
   points = [h["start"]] + inside + [h["end"]]
   longest = 0.0
   longest_from = h["start"]
   for a, b in zip(points, points[1:]):
    if b - a > longest:
     longest = b - a
     longest_from = a
   h["agent_updates_during_hold"] = len(inside)
   h["longest_without_update_sec"] = round(longest, 1)
   kind = "Hold" if use_genesys else "Silence"
   if ann is None:
    discrepancies.append(kind + " of " + _dur(h["duration"]) + " at " + _ts(h["start"]) + " - no hold announcement detected")
   elif ann["promised_sec"] and h["duration"] - ann["promised_sec"] > 20:
    over = h["duration"] - ann["promised_sec"]
    discrepancies.append(
     "Agent promised about " + _dur(ann["promised_sec"]) + " at " + _ts(ann["time"])
     + " but hold lasted " + _dur(h["duration"]) + " (+" + _dur(over) + " over)"
    )
   if longest >= hold_checkin:
    no_checkin.append("Caller on hold " + _dur(longest) + " without an agent update from " + _ts(longest_from))
  for ls in long_silences:
   discrepancies.append(
    "Line open but silent for " + _dur(ls["duration"]) + " at " + _ts(ls["start"])
    + " (caller not on hold) - dead air over " + str(int(dead_checkin)) + "s"
   )

  # ---- turns ----
  pii_types = set()
  turns = []
  gap_limit = _to_float(self.turn_gap, 1.5)
  for s in all_segs:
   text = s["display_text"]
   if self.redact_pii:
    text, hits = redact_text(text)
    pii_types.update(hits)
   on_hold = s["speaker"] == "CALLER" and _during_hold(s["start"])
   if turns:
    cur = turns[-1]
    if (cur["speaker"] == s["speaker"] and cur["on_hold"] == on_hold
        and s["start"] - cur["end"] <= gap_limit and cur["end"] - cur["start"] <= 15):
     cur["text"] = cur["text"] + " " + text
     cur["end"] = max(cur["end"], s["end"])
     continue
   turns.append({"start": s["start"], "end": s["end"], "speaker": s["speaker"], "text": text, "on_hold": on_hold})

  markers = []
  for h in hold_events:
   label = "ON HOLD" if h.get("source") == "genesys" else "HOLD / SILENCE"
   markers.append((h["start"], label, h))
  for ls in long_silences:
   markers.append((ls["start"], "DEAD AIR (not on hold)", ls))
  markers.sort(key=lambda x: x[0])
  lines = []
  mi = 0
  for t in turns:
   while mi < len(markers) and markers[mi][0] < t["start"]:
    _, label, ev = markers[mi]
    lines.append(
     "        ----- " + label + ": " + _dur(ev["duration"])
     + " (" + _ts(ev["start"]) + " -> " + _ts(ev["end"]) + ") -----"
    )
    mi += 1
   prefix = (t["speaker"] + ": ") if t["speaker"] else ""
   body = (HOLD_LABEL + " " + t["text"]) if t["on_hold"] else t["text"]
   lines.append("[" + _ts(t["start"]) + "] " + prefix + body)
  clean_transcript = "\n".join(lines)

  # ---- metrics ----
  if not audio_duration and all_words:
   audio_duration = all_words[-1][1]
  total_hold = sum(e["duration"] for e in hold_events)
  total_dead = sum(e["duration"] for e in dead_air_events) + sum(e["duration"] for e in long_silences)
  longest_hold = max([e["duration"] for e in hold_events] or [0.0])
  agent_talk = sum(e - s for s, e in agent_words)
  caller_talk = sum(e - s for s, e in caller_words)
  talk_total = agent_talk + caller_talk
  overtalk = _overlap_seconds(agent_words, caller_words) if dual else 0.0
  cred_windows = []
  if credential_events:
   credential_events.sort(key=lambda c: c["start"])
   cur = dict(credential_events[0])
   for c in credential_events[1:]:
    if c["start"] - cur["end"] <= 15:
     cur["end"] = max(cur["end"], c["end"])
    else:
     cred_windows.append(cur)
     cur = dict(c)
   cred_windows.append(cur)
  agents = meta.get("agents") or []
  agent_names = ", ".join([a.get("name") or "" for a in agents if a.get("name")])
  if use_genesys:
   hold_source = "genesys"
  elif meta.get("hold_data_error"):
   hold_source = "silence (Genesys hold data unavailable: " + str(meta.get("hold_data_error")) + ")"
  else:
   hold_source = "silence"

  return {
   "status": "DONE",
   "channel": "CALL",
   "conversation_id": meta.get("conversation_id"),
   "recording_id": meta.get("recording_id"),
   "call_start": meta.get("start_time"),
   "call_end": meta.get("end_time"),
   "agent_names": agent_names,
   "agent_emails": [a.get("email") for a in agents if a.get("email")],
   "queue_ids": meta.get("queue_ids") or [],
   "wrapup_code_ids": meta.get("wrapup_code_ids") or [],
   "mode": "dual-channel" if dual else "single-track",
   "duplicate_tracks_detected": duplicate_tracks,
   "agent_track": per_track[agent_index]["track"] if dual else None,
   "speaker_mapping_confidence": mapping_confidence,
   "clean_transcript": clean_transcript,
   "turn_count": len(turns),
   "language": language,
   "language_probability": round(language_prob, 3),
   "audio_duration_sec": round(audio_duration, 1),
   "agent_talk_sec": round(agent_talk, 1),
   "caller_talk_sec": round(caller_talk, 1),
   "agent_talk_ratio": round(agent_talk / talk_total * 100, 1) if talk_total else 0.0,
   "overtalk_sec": round(overtalk, 1),
   "hold_source": hold_source,
   "total_hold_sec": round(total_hold, 1),
   "hold_percentage": round(total_hold / audio_duration * 100, 1) if audio_duration else 0.0,
   "hold_event_count": len(hold_events),
   "longest_hold_sec": round(longest_hold, 1),
   "hold_events": hold_events,
   "long_silence_events": long_silences,
   "long_silence_count": len(long_silences),
   "dead_air_event_count": len(dead_air_events) + len(long_silences),
   "total_dead_air_sec": round(total_dead, 1),
   "hold_checkin_sec": hold_checkin,
   "dead_air_checkin_sec": dead_checkin,
   "hold_announcement_count": len(hold_announcements),
   "discrepancies": discrepancies,
   "no_checkin_flags": no_checkin,
   "credential_windows": cred_windows,
   "credential_event_count": len(cred_windows),
   "repeated_segment_count": len(repeats),
   "loops_collapsed": loops_collapsed,
   "low_confidence_count": low_conf_count,
   "transcript_confidence": transcript_confidence,
   "agent_confidence": _mean(agent_probs),
   "caller_confidence": _mean(caller_probs),
   "uncertain_word_count": uncertain_words,
   "uncertain_word_pct": uncertain_pct,
   "segments_refined": segments_refined,
   "hallucinations_dropped": hallucinations_dropped,
   "transcript_review_required": bool(review_reasons),
   "transcript_review_reason": "; ".join(review_reasons),
   "preprocessed_tracks": preprocessed_tracks,
   "preprocess_errors": preprocess_errors,
   "hotwords_used": opts.get("hotwords") or "",
   "beam_size": opts.get("beam_size"),
   "refine_model": opts.get("refine_model") if opts.get("refine_enabled") else None,
   "pii_redaction_active": bool(self.redact_pii),
   "pii_types_found": sorted(pii_types),
   "model_used": self.model_size,
   "device_used": device,
   "threads_per_track": threads,
   "engine_source": engine_source,
   "engine_version": engine_version,
   "collect_sec": round(time.time() - t0, 1),
  }

 # ---------------- text blocks ----------------

 def _signals_block(self, r):
  out = []
  dur = r["audio_duration_sec"] or 1
  genesys = r["hold_source"] == "genesys"
  label = "HOLD (Genesys)" if genesys else "HOLD (inferred from silence)"
  if r["hold_events"]:
   for h in r["hold_events"]:
    line = (
     "[!] " + label + ": " + _ts(h["start"]) + " -> " + _ts(h["end"]) + ", " + _dur(h["duration"])
     + " (" + str(round(h["duration"] / dur * 100, 1)) + "% of call)"
    )
    if h.get("announced_at") is not None:
     line += " - announced at " + _ts(h["announced_at"]) + ' ("' + str(h["announced_phrase"]) + '")'
    else:
     line += " - NOT announced"
    if h.get("acknowledged_phrase"):
     line += '; acknowledged on return ("' + h["acknowledged_phrase"] + '")'
    else:
     line += "; no acknowledgement on return"
    line += "; longest stretch without agent update " + _dur(h.get("longest_without_update_sec") or 0)
    out.append(line)
  else:
   out.append("[ok] No holds detected.")
  for d in r["discrepancies"]:
   out.append("[!] DISCREPANCY: " + d)
  for c in r["no_checkin_flags"]:
   out.append("[!] " + c)
  if r["dead_air_event_count"]:
   out.append(
    "[i] Dead air (gaps of " + str(_to_int(self.dead_air_threshold, 10)) + "s or more, not on hold): "
    + str(r["dead_air_event_count"]) + " pause(s) totalling " + _dur(r["total_dead_air_sec"])
   )
  if r["mode"] == "dual-channel":
   out.append(
    "[i] Talk time: agent " + _dur(r["agent_talk_sec"]) + ", caller " + _dur(r["caller_talk_sec"])
    + " (agent " + str(r["agent_talk_ratio"]) + "%)"
   )
   if r["overtalk_sec"] >= 3:
    out.append("[!] Overtalk: both parties speaking at once for " + _dur(r["overtalk_sec"]))
  if r["credential_windows"]:
   for c in r["credential_windows"]:
    note = "Content redacted. " if r.get("pii_redaction_active") else ""
    out.append(
     "[!] CREDENTIAL HANDLING: login details or codes discussed " + _ts(c["start"]) + " -> "
     + _ts(c["end"]) + ". " + note + "Assess against user account security guidelines."
    )
  else:
   out.append("[ok] No credential disclosure detected.")
  if r["repeated_segment_count"]:
   out.append("[!] " + str(r["repeated_segment_count"]) + " repeated phrase(s) - possible looping audio")
  if r["loops_collapsed"]:
   out.append("[i] " + str(r["loops_collapsed"]) + " transcription loop(s) collapsed")
  if r["low_confidence_count"]:
   out.append("[!] " + str(r["low_confidence_count"]) + " low-confidence passage(s) - quote with caution")
  out.append(
   "[i] Transcript confidence: " + str(r["transcript_confidence"]) + " (agent " + str(r["agent_confidence"])
   + ", caller " + str(r["caller_confidence"]) + "); " + str(r["uncertain_word_pct"])
   + "% uncertain words marked (?). Never score the agent on words marked (?)."
  )
  out.append("[i] Caller lines marked " + HOLD_LABEL + " were spoken while on hold and are not agent-facing.")
  if r["transcript_review_required"]:
   out.append("[!] TRANSCRIPT QUALITY: review recommended (" + r["transcript_review_reason"] + ")")
  if r["mode"] == "dual-channel":
   out.append("[i] Speaker labels come from separate audio channels (mapping confidence: " + r["speaker_mapping_confidence"] + ").")
  else:
   out.append("[i] Single-track audio: speaker labels are rule-based guesses.")
  return "\n".join(out)

 def _metadata_block(self, r):
  pii = "DISABLED"
  if r["pii_redaction_active"]:
   pii = "ACTIVE - " + (", ".join(r["pii_types_found"]) if r["pii_types_found"] else "nothing matched")
  if r["hold_source"] == "genesys":
   hold_src = "Genesys hold events (exact)"
  else:
   hold_src = "Inferred from silence - " + r["hold_source"]
  transcription = str(r["model_used"]) + " on " + str(r["device_used"]) + ", beam " + str(r["beam_size"])
  if r["refine_model"]:
   transcription += ", second pass " + str(r["refine_model"]) + " (" + str(r["segments_refined"]) + " segments improved)"
  if r.get("preprocessed_tracks"):
   preprocess = "ON (" + str(r["preprocessed_tracks"]) + " track(s) filtered)"
  elif r.get("preprocess_errors"):
   preprocess = "FAILED - " + r["preprocess_errors"][0][:120]
  else:
   preprocess = "off"
  return "\n".join([
   "CHANNEL: CALL",
   "Conversation ID: " + str(r["conversation_id"]),
   "Agent: " + (r["agent_names"] or "unknown"),
   "Call start (UTC): " + str(r["call_start"]),
   "Duration: " + _dur(r["audio_duration_sec"]) + " (" + str(r["audio_duration_sec"]) + "s)",
   "Audio mode: " + r["mode"],
   "Speaking turns: " + str(r["turn_count"]),
   "Hold data: " + hold_src,
   "Hold time: " + _dur(r["total_hold_sec"]) + " (" + str(r["hold_percentage"]) + "% of call)",
   "Hold events: " + str(r["hold_event_count"]) + " (longest " + _dur(r["longest_hold_sec"]) + ")",
   "Transcription: " + transcription,
   "Audio preprocessing: " + preprocess,
   "Engine: faster-whisper " + str(r.get("engine_version")) + " (" + str(r.get("engine_source")) + ")",
   "Transcript confidence: " + str(r["transcript_confidence"]) + (" - REVIEW RECOMMENDED" if r["transcript_review_required"] else ""),
   "PII redaction: " + pii,
  ])

 def _pending_text(self, r):
  lines = [
   "TRANSCRIPTION_PENDING",
   "Conversation: " + str(r.get("conversation_id")),
   "Overall progress: " + str(r.get("overall_progress_pct")) + "%",
  ]
  for t in r.get("tracks", []):
   lines.append("  Track " + str(t["track"]) + ": " + str(t["state"]) + " " + str(t["progress_pct"]) + "%")
  lines.append(r.get("detail", ""))
  return "\n".join(lines)

 def _stop_branch(self, output_name):
  try:
   self.stop(output_name)
  except Exception:
   pass

 # ---------------- outputs ----------------

 def build_mission_payload(self) -> Message:
  try:
   r = self._run()
  except Exception as e:
   self._stop_branch("mission_payload")
   return Message(text="TRANSCRIPTION_ERROR: " + str(e))
  if r.get("status") == "PROCESSING":
   self._stop_branch("mission_payload")
   return Message(text=self._pending_text(r))
  transcript = r["clean_transcript"]
  limit = _to_int(self.max_transcript_chars, 0)
  note = ""
  if limit and len(transcript) > limit:
   transcript = transcript[:limit]
   note = "\n\n[NOTE: transcript truncated at " + str(limit) + " characters.]"
  payload = "\n".join([
   self.audit_instructions or "",
   "",
   "=== CALL METADATA ===",
   self._metadata_block(r),
   "",
   "=== AUTOMATED SIGNALS (measured - treat as evidence, do not re-estimate) ===",
   self._signals_block(r),
   "",
   "=== TRANSCRIPT (timestamps mm:ss; words marked (?) are uncertain) ===",
   transcript + note,
   "",
   "=== END OF CALL ===",
   "Produce your audit now. Cite a timestamp for every score justification.",
   "Base all hold and silence judgements on the AUTOMATED SIGNALS block and VERIFIED FACTS.",
  ])
  return Message(text=payload)

 def build_report(self) -> Message:
  try:
   r = self._run()
  except Exception as e:
   return Message(text="Component Error: " + str(e))
  if r.get("status") == "PROCESSING":
   return Message(text=self._pending_text(r))
  report = "\n".join([
   "CALL QA REPORT",
   "========================",
   self._metadata_block(r),
   "",
   "========================",
   "AUTOMATED SIGNALS",
   "========================",
   self._signals_block(r),
   "",
   "========================",
   "TRANSCRIPT",
   "========================",
   r["clean_transcript"],
  ])
  return Message(text=report)

 def build_structured(self) -> Data:
  try:
   return Data(data=self._run())
  except Exception as e:
   return Data(data={"error": str(e)})
   

