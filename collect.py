#!/usr/bin/env python3
"""Collect Google Antigravity (agy) usage into an Omarchy agents-panel JSON record.

Omarchy's stock agents widget monitors ~/.local/state/omarchy/agents/usage/*.json
and automatically creates a tab/chip for each agent found there.

This collector gathers:
- Live model-by-model quota limits (Gemini Flash/Pro, Claude Sonnet/Opus, GPT-OSS) via `agy --output-format json --print "/usage"`
- Subscription tier detection (Free Tier, AI Pro Tier, AI Plus Tier) via `agy /credits` and optional config override
- Per-model token usage breakdown from conversation transcripts
- Local prompt and session history from ~/.gemini/antigravity-cli/history.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

AGENT_ID = "antigravity"
AGENT_NAME = "Antigravity"
AUTH_HELP = "Run `agy` in your terminal to authenticate."
LIMITS_CACHE_SECONDS = 180.0


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
      return json.load(f), age
  except Exception:
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


def resolve_tier(agy_path: str | None) -> str:
  """Detects or loads user subscription tier (Free Tier, AI Pro Tier, AI Plus Tier)."""
  # 1. Custom user override in ~/.config/omarchy/agents/antigravity.json
  config_file = Path.home() / ".config" / "omarchy" / "agents" / "antigravity.json"
  if config_file.exists():
    try:
      with config_file.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
        configured_tier = str(cfg.get("tier") or "").strip()
        if configured_tier:
          return configured_tier
    except Exception:
      pass

  # 2. Check settings in ~/.gemini/antigravity-cli/settings.json
  settings_file = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
  if settings_file.exists():
    try:
      with settings_file.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
        configured_tier = str(cfg.get("tier") or cfg.get("plan") or "").strip()
        if configured_tier:
          return configured_tier
    except Exception:
      pass

  # 3. Auto-detect via `agy /credits`
  if agy_path:
    try:
      res = subprocess.run(
          [agy_path, "--output-format", "json", "--print", "/credits"],
          capture_output=True,
          text=True,
          timeout=5,
          check=False,
      )
      if res.returncode == 0 and res.stdout.strip():
        data = json.loads(res.stdout)
        cdata = data.get("command", {}).get("data", {})
        rem_credits = int(cdata.get("remaining_credits", 0) or 0)
        upgrade_uri = str(cdata.get("upgrade_uri", "") or "")

        if "g1-upgrade" in upgrade_uri and rem_credits == 0:
          return "Free Tier"
        if rem_credits > 0:
          return "AI Pro Tier"
    except Exception:
      pass

  return "Free Tier"


def probe_limits(agy_path: str, force: bool = False) -> tuple[list[dict[str, Any]], str]:
  """Returns (model_breakdown_limits, status_text)."""
  cached_data, age = load_cached_limits()
  if not force and cached_data and age < LIMITS_CACHE_SECONDS:
    return cached_data.get("limits", []), cached_data.get("usageStatusText", "")

  try:
    res = subprocess.run(
        [agy_path, "--output-format", "json", "--print", "/usage"],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
      if cached_data:
        return cached_data.get("limits", []), "Showing cached limits"
      return [], "Limits unavailable"

    data = json.loads(res.stdout)
    groups = data.get("command", {}).get("data", {}).get("groups", [])

    # Extract raw buckets
    bucket_map: dict[str, dict[str, Any]] = {}
    for group in groups:
      for bucket in group.get("buckets", []):
        bid = str(bucket.get("id", "")).lower()
        rem = float(bucket.get("remaining_fraction", 1.0))
        used = max(0.0, min(1.0, 1.0 - rem))
        bucket_map[bid] = {
            "percent": round(used, 4),
            "resetsAt": bucket.get("reset_time", ""),
        }

    gemini_weekly = bucket_map.get("gemini-weekly", {"percent": 0.0, "resetsAt": ""})
    gemini_5h = bucket_map.get("gemini-5h", {"percent": 0.0, "resetsAt": ""})
    p3_weekly = bucket_map.get("3p-weekly", {"percent": 0.0, "resetsAt": ""})
    p3_5h = bucket_map.get("3p-5h", {"percent": 0.0, "resetsAt": ""})

    # Clear, model-specific breakdown requested by user
    limits: list[dict[str, Any]] = [
        # Gemini Models (1P)
        {
            "label": "Gemini 3.8 Flash (Weekly)",
            "title": "Gemini 3.8 Flash (Weekly)",
            "percent": gemini_weekly["percent"],
            "resetsAt": gemini_weekly["resetsAt"],
        },
        {
            "label": "Gemini 3.8 Flash (5-Hour)",
            "title": "Gemini 3.8 Flash (5-Hour)",
            "percent": gemini_5h["percent"],
            "resetsAt": gemini_5h["resetsAt"],
        },
        {
            "label": "Gemini 3.1 Pro (Weekly)",
            "title": "Gemini 3.1 Pro (Weekly)",
            "percent": gemini_weekly["percent"],
            "resetsAt": gemini_weekly["resetsAt"],
        },
        {
            "label": "Gemini 3.1 Pro (5-Hour)",
            "title": "Gemini 3.1 Pro (5-Hour)",
            "percent": gemini_5h["percent"],
            "resetsAt": gemini_5h["resetsAt"],
        },
        # 3P Models
        {
            "label": "Claude Sonnet 4.6 (Weekly)",
            "title": "Claude Sonnet 4.6 (Weekly)",
            "percent": p3_weekly["percent"],
            "resetsAt": p3_weekly["resetsAt"],
        },
        {
            "label": "Claude Sonnet 4.6 (5-Hour)",
            "title": "Claude Sonnet 4.6 (5-Hour)",
            "percent": p3_5h["percent"],
            "resetsAt": p3_5h["resetsAt"],
        },
        {
            "label": "Claude Opus 4.6 (Weekly)",
            "title": "Claude Opus 4.6 (Weekly)",
            "percent": p3_weekly["percent"],
            "resetsAt": p3_weekly["resetsAt"],
        },
        {
            "label": "Claude Opus 4.6 (5-Hour)",
            "title": "Claude Opus 4.6 (5-Hour)",
            "percent": p3_5h["percent"],
            "resetsAt": p3_5h["resetsAt"],
        },
        {
            "label": "GPT-OSS 120B (Weekly)",
            "title": "GPT-OSS 120B (Weekly)",
            "percent": p3_weekly["percent"],
            "resetsAt": p3_weekly["resetsAt"],
        },
        {
            "label": "GPT-OSS 120B (5-Hour)",
            "title": "GPT-OSS 120B (5-Hour)",
            "percent": p3_5h["percent"],
            "resetsAt": p3_5h["resetsAt"],
        },
    ]

    cache_payload = {
        "limits": limits,
        "usageStatusText": "",
    }
    save_cached_limits(cache_payload)
    return limits, ""
  except subprocess.TimeoutExpired:
    if cached_data:
      return cached_data.get("limits", []), "Showing cached limits"
    return [], "Timed out fetching limits"
  except Exception as e:
    if cached_data:
      return cached_data.get("limits", []), "Showing cached limits"
    return [], f"Error: {e}"


def collect_model_usage() -> tuple[dict[str, Any], dict[str, int], int]:
  """Parses local conversation transcripts to calculate token metrics per model.

  Returns (model_usage_map, today_tokens_by_model, today_total_tokens).
  """
  brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
  model_usage: dict[str, dict[str, int]] = {}
  today_tokens_by_model: dict[str, int] = {}
  today_total_tokens = 0
  today_str = local_date_string()

  if not brain_dir.is_dir():
    return {}, {}, 0

  for tpath in brain_dir.glob("*/.system_generated/logs/transcript.jsonl"):
    conv_model = "gemini-3-8-flash"
    conv_in = 0
    conv_out = 0
    is_today = False

    try:
      with tpath.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
          if "Model Selection" in line:
            m = re.search(r"Model Selection` from (?:None|\w+) to ([\w\s\.\(\)-]+)\.", line)
            if m:
              raw = m.group(1).lower()
              if "opus" in raw:
                conv_model = "claude-opus-4-6"
              elif "sonnet" in raw:
                conv_model = "claude-sonnet-4-6"
              elif "gpt" in raw or "oss" in raw:
                conv_model = "gpt-oss-120b"
              elif "pro" in raw:
                conv_model = "gemini-3-1-pro"
              elif "3.7" in raw:
                conv_model = "gemini-3-7-flash"
              elif "3.6" in raw:
                conv_model = "gemini-3-6-flash"
              else:
                conv_model = "gemini-3-8-flash"

          try:
            step = json.loads(line)
            created_at = str(step.get("created_at") or "")
            if created_at.startswith(today_str):
              is_today = True

            content = step.get("content") or ""
            stype = step.get("type") or ""
            tcount = max(1, len(content) // 4)
            if stype == "USER_INPUT":
              conv_in += tcount
            elif stype in ("PLANNER_RESPONSE", "GENERIC", "MODEL"):
              conv_out += tcount
          except Exception:
            pass
    except Exception:
      pass

    if conv_model not in model_usage:
      model_usage[conv_model] = {
          "inputTokens": 0,
          "outputTokens": 0,
          "cacheReadInputTokens": 0,
          "cacheCreationInputTokens": 0,
      }

    model_usage[conv_model]["inputTokens"] += conv_in
    model_usage[conv_model]["outputTokens"] += conv_out

    if is_today:
      tokens = conv_in + conv_out
      today_tokens_by_model[conv_model] = today_tokens_by_model.get(conv_model, 0) + tokens
      today_total_tokens += tokens

  return model_usage, today_tokens_by_model, today_total_tokens


def collect_local_history() -> dict[str, Any]:
  history_file = Path.home() / ".gemini" / "antigravity-cli" / "history.jsonl"
  prompts_by_date: dict[str, int] = {}
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
          prompts_by_date[date_str] = prompts_by_date.get(date_str, 0) + 1
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

  recent_days = []
  for date_str in recent_date_strings():
    recent_days.append({
        "date": date_str,
        "messageCount": prompts_by_date.get(date_str, 0),
    })

  return {
      "totalPrompts": total_prompts,
      "todayPrompts": today_prompts,
      "totalSessions": total_sessions,
      "todaySessions": len(today_sessions),
      "activeDays": len(active_dates),
      "activeDates": sorted(list(active_dates)),
      "recentDays": recent_days,
  }


def build_record(force: bool = False) -> dict[str, Any]:
  agy_path = agy_bin()
  stats = collect_local_history()
  tier = resolve_tier(agy_path)
  model_usage, today_tokens_by_model, today_total_tokens = collect_model_usage()

  if not agy_path:
    return {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ready": stats["totalPrompts"] > 0,
        "hasLocalStats": True,
        "hasPromptStats": True,
        "todayPrompts": stats["todayPrompts"],
        "todaySessions": stats["todaySessions"],
        "todayTotalTokens": today_total_tokens,
        "todayTokensByModel": today_tokens_by_model,
        "recentDays": stats["recentDays"],
        "totalPrompts": stats["totalPrompts"],
        "totalSessions": stats["totalSessions"],
        "activeDays": stats["activeDays"],
        "activeDates": stats["activeDates"],
        "modelUsage": model_usage,
        "limits": [],
        "tierLabel": tier,
        "usageStatusText": "agy CLI not found",
        "authHelpText": "Install the Antigravity CLI to view live quota.",
    }

  limits, status_text = probe_limits(agy_path, force=force)
  ready = stats["totalPrompts"] > 0 or len(limits) > 0

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
      "todayTotalTokens": today_total_tokens,
      "todayTokensByModel": today_tokens_by_model,
      "recentDays": stats["recentDays"],
      "totalPrompts": stats["totalPrompts"],
      "totalSessions": stats["totalSessions"],
      "activeDays": stats["activeDays"],
      "activeDates": stats["activeDates"],
      "modelUsage": model_usage,
      "limits": limits,
      "tierLabel": tier,
      "usageStatusText": status_text,
      "authHelpText": AUTH_HELP,
  }


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
  target = usage_dir() / f"{AGENT_ID}.json"
  try:
    if target.exists():
      target.unlink()
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
