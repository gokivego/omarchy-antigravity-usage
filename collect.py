#!/usr/bin/env python3
"""Collect Google Antigravity (agy) usage into an Omarchy agents-panel JSON record.

Omarchy's stock agents widget watches ~/.local/state/omarchy/agents/usage/*.json
and draws a tab for each record. This collector writes antigravity.json from:

- `agy --print /usage` quota groups (shared weekly and 5-hour pools)
- `agy models` for the live catalog, so new models join the right pool
  without a plugin update
- `loadCodeAssist` paidTier.name for the plan shown in the AGY console
- ~/.gemini/antigravity-cli/history.jsonl and brain transcripts for local stats
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AGENT_ID = "antigravity"
AGENT_NAME = "Antigravity"
AUTH_HELP = "Run `agy` in your terminal to authenticate."
LIMITS_CACHE_SECONDS = 180.0
MAX_HTTP_BODY_BYTES = 256 * 1024
LOAD_CODE_ASSIST_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
EFFORT_SUFFIX = re.compile(r"\s*\((?:High|Medium|Low|Thinking)\)\s*$", re.IGNORECASE)
GROUP_MODELS_MARKER = "Models within this group:"
MODEL_SELECTION_RE = re.compile(
    r"Model Selection` from (?:None|\w+) to ([\w\s\.\(\)-]+)\."
)
TOKEN_STEP_TYPES = {"USER_INPUT", "PLANNER_RESPONSE", "GENERIC", "MODEL"}
DAILY_LEDGER_KEEP_DAYS = 45


def expand_path(value: str) -> Path:
  return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def agy_bin() -> str | None:
  bin_path = shutil.which("agy")
  if bin_path:
    return bin_path
  fallback = Path.home() / ".local" / "bin" / "agy"
  if fallback.is_file() and os.access(fallback, os.X_OK):
    return str(fallback)
  return None


def cache_root() -> Path:
  root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omarchy" / "agent-usage"
  root.mkdir(parents=True, exist_ok=True)
  return root


def usage_dir() -> Path:
  state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
  path = state / "omarchy" / "agents" / "usage"
  path.mkdir(parents=True, exist_ok=True)
  return path


def local_date_string(date: dt.date | None = None) -> str:
  d = date or dt.datetime.now().date()
  return d.strftime("%Y-%m-%d")


def recent_date_strings() -> list[str]:
  today = dt.datetime.now().date()
  return [local_date_string(today - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def load_cached_limits() -> tuple[dict[str, Any] | None, float]:
  cache_file = cache_root() / "antigravity-limits.json"
  if not cache_file.exists():
    return None, 0.0
  try:
    mtime = cache_file.stat().st_mtime
    age = max(0.0, dt.datetime.now().timestamp() - mtime)
    with cache_file.open("r", encoding="utf-8") as f:
      data = json.load(f)
    if isinstance(data, dict):
      return data, age
  except Exception:
    pass
  return None, 0.0


def save_cached_limits(data: dict[str, Any]) -> None:
  cache_file = cache_root() / "antigravity-limits.json"
  try:
    with tempfile.NamedTemporaryFile("w", dir=cache_file.parent, delete=False, encoding="utf-8") as tmp:
      json.dump(data, tmp)
      tmp_path = Path(tmp.name)
    tmp_path.replace(cache_file)
  except Exception:
    pass


def agy_json(agy_path: str, extra_args: list[str], timeout: float) -> dict[str, Any] | None:
  try:
    res = subprocess.run(
        [agy_path, "--output-format", "json", *extra_args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
  except (subprocess.TimeoutExpired, OSError):
    return None
  if res.returncode != 0 or not res.stdout.strip():
    return None
  try:
    data = json.loads(res.stdout)
  except json.JSONDecodeError:
    return None
  return data if isinstance(data, dict) else None


def family_label(label: str) -> str:
  return EFFORT_SUFFIX.sub("", str(label or "")).strip()


def model_slug(label: str) -> str:
  family = family_label(label).lower().replace(".", "-")
  slug = re.sub(r"[^a-z0-9]+", "-", family).strip("-")
  return slug or "unknown"


def group_short_name(name: str) -> str:
  short = re.sub(r"\s+models?\s*$", "", str(name or "").strip(), flags=re.IGNORECASE)
  short = re.sub(r"\s+and\s+", " + ", short, flags=re.IGNORECASE)
  return short or (name or "Models")


def window_word(bucket: dict[str, Any]) -> str:
  text = " ".join(
      str(bucket.get(key) or "")
      for key in ("window", "id", "name")
  ).lower()
  if "week" in text:
    return "weekly"
  if "5h" in text or "five hour" in text or "five-hour" in text:
    return "5-hour"
  window = str(bucket.get("window") or bucket.get("name") or "limit").strip()
  return window or "limit"


def description_models(group: dict[str, Any]) -> list[str]:
  desc = str(group.get("description") or "")
  if GROUP_MODELS_MARKER not in desc:
    return []
  listed = desc.split(GROUP_MODELS_MARKER, 1)[1]
  return [part.strip() for part in listed.split(",") if part.strip()]


def pool_member_name(label: str) -> str:
  """Collapse versioned models to the pool class the quota row can show.

  The stock agents LimitRow elides a single line. Listing Gemini 3.8/3.7/3.6
  Flash overflows. Flash, Pro, Sonnet, Opus, and GPT-OSS stay short, and a
  new 3.9 Flash still maps to Flash without a plugin change.
  """
  family = family_label(label)
  lab = family.lower()
  if "flash" in lab:
    return "Flash"
  if "opus" in lab:
    return "Opus"
  if "sonnet" in lab:
    return "Sonnet"
  if "gpt" in lab or "oss" in lab:
    return "GPT-OSS"
  if re.search(r"\bpro\b", lab):
    return "Pro"
  stripped = re.sub(r"\d+(?:\.\d+)*", "", family)
  stripped = re.sub(r"\s+", " ", stripped).strip(" -")
  return stripped or family


def catalog_models(agy_path: str) -> list[dict[str, str]]:
  data = agy_json(agy_path, ["models"], timeout=12)
  if not data:
    return []
  models = data.get("command", {}).get("data", {}).get("models")
  if not isinstance(models, list):
    return []
  out: list[dict[str, str]] = []
  seen: set[str] = set()
  for entry in models:
    if not isinstance(entry, dict):
      continue
    label = family_label(str(entry.get("label") or entry.get("id") or ""))
    if not label:
      continue
    key = label.lower()
    if key in seen:
      continue
    seen.add(key)
    out.append({
        "id": str(entry.get("id") or ""),
        "label": label,
    })
  return out


def model_matches_group(model: dict[str, str], group: dict[str, Any]) -> bool:
  name = str(group.get("name") or "").lower()
  mid = str(model.get("id") or "").lower()
  lab = str(model.get("label") or "").lower()
  hay = f"{mid} {lab}"

  if "gemini" in name:
    return "gemini" in hay
  if "claude" in name or "gpt" in name:
    return any(token in hay for token in ("claude", "gpt", "oss", "sonnet", "opus"))

  for item in description_models(group):
    token = item.lower()
    if token and token in hay:
      return True
  return False


def pool_members_for_group(group: dict[str, Any], catalog: list[dict[str, str]]) -> list[str]:
  members: list[str] = []
  seen: set[str] = set()

  def add(label: str) -> None:
    name = pool_member_name(label)
    key = name.lower()
    if not name or key in seen:
      return
    seen.add(key)
    members.append(name)

  for item in description_models(group):
    add(item)
  for model in catalog:
    if model_matches_group(model, group):
      add(model["label"])
  return members


def limits_from_usage(data: dict[str, Any], catalog: list[dict[str, str]]) -> list[dict[str, Any]]:
  groups = data.get("command", {}).get("data", {}).get("groups")
  if not isinstance(groups, list):
    return []

  limits: list[dict[str, Any]] = []
  for group in groups:
    if not isinstance(group, dict):
      continue
    short = group_short_name(str(group.get("name") or "Models"))
    members = pool_members_for_group(group, catalog)
    shared = " + ".join(members)
    buckets = group.get("buckets")
    if not isinstance(buckets, list):
      continue
    for bucket in buckets:
      if not isinstance(bucket, dict):
        continue
      try:
        remaining = float(bucket.get("remaining_fraction", 1.0))
      except (TypeError, ValueError):
        remaining = 1.0
      used = max(0.0, min(1.0, 1.0 - remaining))
      window = window_word(bucket)
      title = f"{short} {window}"
      if shared:
        title = f"{title} · {shared}"
      limits.append({
          "label": title,
          "title": title,
          "percent": round(used, 4),
          "resetsAt": str(bucket.get("reset_time") or ""),
      })
  return limits


def read_limited_http_body(response: Any, max_bytes: int) -> bytes:
  chunks: list[bytes] = []
  total = 0
  while True:
    chunk = response.read(min(65536, max_bytes - total + 1))
    if not chunk:
      break
    total += len(chunk)
    if total > max_bytes:
      raise ValueError("HTTP body exceeds size limit")
    chunks.append(chunk)
  return b"".join(chunks)


def oauth_token_block(raw: dict[str, Any]) -> dict[str, Any]:
  token = raw.get("token")
  if isinstance(token, dict):
    return token
  return raw


def read_oauth_creds() -> dict[str, Any] | None:
  path = Path.home() / ".gemini" / "oauth_creds.json"
  if path.is_file():
    try:
      data = json.loads(path.read_text(encoding="utf-8"))
      if isinstance(data, dict):
        return oauth_token_block(data)
    except Exception:
      pass

  try:
    res = subprocess.run(
        ["secret-tool", "lookup", "service", "gemini", "username", "antigravity"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if res.returncode != 0 or not res.stdout.strip():
    return None
  try:
    data = json.loads(res.stdout)
  except json.JSONDecodeError:
    return None
  if isinstance(data, dict):
    return oauth_token_block(data)
  return None


def fetch_console_plan() -> str:
  """Return the plan name from the same Cloud Code call the AGY console uses."""
  creds = read_oauth_creds()
  if not creds:
    return ""
  access = str(creds.get("access_token") or "").strip()
  if not access:
    return ""
  request = urllib.request.Request(
      LOAD_CODE_ASSIST_URL,
      data=json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode("utf-8"),
      headers={
          "Authorization": "Bearer " + access,
          "Content-Type": "application/json",
          "Accept": "application/json",
          "User-Agent": "antigravity/linux/amd64",
      },
      method="POST",
  )
  try:
    with urllib.request.urlopen(request, timeout=10) as response:
      body = read_limited_http_body(response, MAX_HTTP_BODY_BYTES)
      payload = json.loads(body.decode("utf-8", errors="replace"))
  except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
    return ""
  if not isinstance(payload, dict):
    return ""

  paid = payload.get("paidTier")
  if isinstance(paid, dict):
    name = str(paid.get("name") or "").strip()
    if name:
      return name
  current = payload.get("currentTier")
  if isinstance(current, dict):
    return str(current.get("name") or "").strip()
  return ""


def config_force_tier() -> str:
  config_file = Path.home() / ".config" / "omarchy" / "agents" / "antigravity.json"
  if not config_file.exists():
    return ""
  try:
    with config_file.open("r", encoding="utf-8") as f:
      cfg = json.load(f)
  except Exception:
    return ""
  if not isinstance(cfg, dict):
    return ""
  return str(cfg.get("forceTier") or "").strip()


def resolve_tier() -> str:
  forced = config_force_tier()
  if forced:
    return forced
  return fetch_console_plan()


def probe_limits(agy_path: str, force: bool = False) -> tuple[list[dict[str, Any]], str]:
  cached_data, age = load_cached_limits()
  if not force and cached_data and age < LIMITS_CACHE_SECONDS:
    return cached_data.get("limits", []), cached_data.get("usageStatusText", "")

  data = agy_json(agy_path, ["--print", "/usage"], timeout=12)
  if not data:
    if cached_data:
      return cached_data.get("limits", []), "Showing cached limits"
    return [], "Limits unavailable"

  catalog = catalog_models(agy_path)
  limits = limits_from_usage(data, catalog)
  cache_payload = {
      "limits": limits,
      "usageStatusText": "",
  }
  save_cached_limits(cache_payload)
  return limits, ""


def empty_bucket() -> dict[str, int]:
  return {
      "inputTokens": 0,
      "outputTokens": 0,
      "cacheReadInputTokens": 0,
      "cacheCreationInputTokens": 0,
  }


def estimate_tokens(content: str) -> int:
  if not content:
    return 0
  return max(1, len(content) // 4)


def local_date_from_iso(raw: str) -> str | None:
  text = str(raw or "").strip()
  if not text:
    return None
  try:
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
  except ValueError:
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
      return text[:10]
    return None
  if parsed.tzinfo is not None:
    parsed = parsed.astimezone()
  return local_date_string(parsed.date())


def daily_ledger_path() -> Path:
  return cache_root() / "antigravity-daily.json"


def load_daily_ledger() -> dict[str, int]:
  path = daily_ledger_path()
  if not path.exists():
    return {}
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return {}
  days = data.get("days") if isinstance(data, dict) else None
  if not isinstance(days, dict):
    return {}
  out: dict[str, int] = {}
  for date, value in days.items():
    try:
      tokens = int(value)
    except (TypeError, ValueError):
      continue
    if tokens >= 0:
      out[str(date)] = tokens
  return out


def save_daily_ledger(days: dict[str, int]) -> None:
  cutoff = local_date_string(dt.datetime.now().date() - dt.timedelta(days=DAILY_LEDGER_KEEP_DAYS))
  kept = {date: tokens for date, tokens in days.items() if date >= cutoff}
  payload = {
      "schemaVersion": 1,
      "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
      "days": kept,
  }
  path = daily_ledger_path()
  try:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
      json.dump(payload, tmp)
      tmp_path = Path(tmp.name)
    tmp_path.replace(path)
  except Exception:
    pass


def merge_daily_ledger(scanned: dict[str, int]) -> dict[str, int]:
  """Keep days we have already counted if their transcripts later disappear."""
  ledger = load_daily_ledger()
  ledger.update(scanned)
  save_daily_ledger(ledger)
  return ledger


def collect_token_stats() -> dict[str, Any]:
  """Build token totals from local transcripts.

  Antigravity /usage only reports remaining quota fractions, not tokens and
  not a per-day series. The stock panel's TOKENS BY DAY chart reads
  recentDays[].messageCount as tokens, so this walk attributes each
  transcript step to its local created_at date and the model selected at
  that step. A small ledger remembers days whose transcripts were later
  deleted.
  """
  brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
  model_usage: dict[str, dict[str, int]] = {}
  today_tokens_by_model: dict[str, int] = {}
  tokens_by_date: dict[str, int] = {}
  today_str = local_date_string()

  if brain_dir.is_dir():
    for tpath in brain_dir.glob("*/.system_generated/logs/transcript.jsonl"):
      conv_model = "unknown"
      try:
        with tpath.open("r", encoding="utf-8", errors="replace") as handle:
          for line in handle:
            match = MODEL_SELECTION_RE.search(line)
            if match:
              conv_model = model_slug(match.group(1))
            try:
              step = json.loads(line)
            except json.JSONDecodeError:
              continue
            stype = str(step.get("type") or "")
            if stype not in TOKEN_STEP_TYPES:
              continue
            content = str(step.get("content") or "")
            tokens = estimate_tokens(content)
            if tokens <= 0:
              continue
            day = local_date_from_iso(str(step.get("created_at") or "")) or today_str
            bucket = model_usage.setdefault(conv_model, empty_bucket())
            if stype == "USER_INPUT":
              bucket["inputTokens"] += tokens
            else:
              bucket["outputTokens"] += tokens
            tokens_by_date[day] = tokens_by_date.get(day, 0) + tokens
            if day == today_str:
              today_tokens_by_model[conv_model] = today_tokens_by_model.get(conv_model, 0) + tokens
      except Exception:
        continue

  ledger = merge_daily_ledger(tokens_by_date)
  recent_days = [
      {"date": date_str, "messageCount": ledger.get(date_str, 0)}
      for date_str in recent_date_strings()
  ]
  return {
      "modelUsage": model_usage,
      "todayTokensByModel": today_tokens_by_model,
      "todayTotalTokens": ledger.get(today_str, 0),
      "recentDays": recent_days,
  }


def collect_local_history() -> dict[str, Any]:
  history_file = Path.home() / ".gemini" / "antigravity-cli" / "history.jsonl"
  total_prompts = 0
  active_dates: set[str] = set()
  today_str = local_date_string()
  today_prompts = 0
  sessions: set[str] = set()
  today_sessions: set[str] = set()

  if history_file.exists():
    try:
      with history_file.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
          line = line.strip()
          if not line:
            continue
          try:
            entry = json.loads(line)
          except Exception:
            continue

          ts = entry.get("timestamp")
          if not ts:
            continue
          try:
            date_str = dt.datetime.fromtimestamp(float(ts) / 1000.0).strftime("%Y-%m-%d")
          except Exception:
            continue

          active_dates.add(date_str)
          total_prompts += 1

          conv_id = entry.get("conversationId")
          if conv_id:
            sessions.add(str(conv_id))
            if date_str == today_str:
              today_sessions.add(str(conv_id))

          if date_str == today_str:
            today_prompts += 1
    except Exception:
      pass

  conv_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
  if conv_dir.is_dir():
    try:
      db_count = sum(1 for p in conv_dir.glob("*.db") if p.is_file())
      total_sessions = max(db_count, len(sessions))
    except Exception:
      total_sessions = len(sessions)
  else:
    total_sessions = len(sessions)

  return {
      "totalPrompts": total_prompts,
      "todayPrompts": today_prompts,
      "totalSessions": total_sessions,
      "todaySessions": len(today_sessions),
      "activeDays": len(active_dates),
      "activeDates": sorted(list(active_dates)),
  }


def empty_record(
    stats: dict[str, Any],
    tokens: dict[str, Any],
    tier: str,
    limits: list[dict[str, Any]],
    status_text: str,
    auth_help: str,
) -> dict[str, Any]:
  ready = stats["totalPrompts"] > 0 or len(limits) > 0 or tokens["todayTotalTokens"] > 0
  return {
      "schemaVersion": 1,
      "id": AGENT_ID,
      "name": AGENT_NAME,
      "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
      "ready": ready,
      "hasLocalStats": True,
      "hasPromptStats": True,
      "todayPrompts": stats["todayPrompts"],
      "todaySessions": stats["todaySessions"],
      "todayTotalTokens": tokens["todayTotalTokens"],
      "todayTokensByModel": tokens["todayTokensByModel"],
      "recentDays": tokens["recentDays"],
      "totalPrompts": stats["totalPrompts"],
      "totalSessions": stats["totalSessions"],
      "activeDays": stats["activeDays"],
      "activeDates": stats["activeDates"],
      "modelUsage": tokens["modelUsage"],
      "limits": limits,
      "tierLabel": tier,
      "usageStatusText": status_text,
      "authHelpText": auth_help,
  }


def build_record(force: bool = False) -> dict[str, Any]:
  agy_path = agy_bin()
  stats = collect_local_history()
  tokens = collect_token_stats()
  tier = resolve_tier()

  if not agy_path:
    return empty_record(
        stats,
        tokens,
        tier,
        [],
        "agy CLI not found",
        "Install the Antigravity CLI to view live quota.",
    )

  limits, status_text = probe_limits(agy_path, force=force)
  return empty_record(stats, tokens, tier, limits, status_text, AUTH_HELP)


def write_record(force: bool = False) -> None:
  record = build_record(force=force)
  dest_dir = usage_dir()
  dest_file = dest_dir / f"{AGENT_ID}.json"

  with tempfile.NamedTemporaryFile("w", dir=dest_dir, delete=False, encoding="utf-8") as tmp:
    json.dump(record, tmp, indent=2)
    tmp.write("\n")
    tmp_path = Path(tmp.name)

  tmp_path.chmod(0o600)
  tmp_path.replace(dest_file)


def clear_record() -> None:
  for path in (usage_dir() / f"{AGENT_ID}.json", daily_ledger_path()):
    try:
      if path.exists():
        path.unlink()
    except Exception:
      pass


def main() -> None:
  parser = argparse.ArgumentParser(description="Antigravity usage collector for Omarchy")
  parser.add_argument("--write", action="store_true", help="Atomically write the usage record to disk")
  parser.add_argument("--clear", action="store_true", help="Remove the usage record from disk")
  parser.add_argument("--force", action="store_true", help="Bypass cached quota probe")
  args = parser.parse_args()

  if args.clear:
    clear_record()
    return

  if args.write:
    write_record(force=args.force)
    return

  record = build_record(force=args.force)
  print(json.dumps(record, indent=2))


if __name__ == "__main__":
  main()
