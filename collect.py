#!/usr/bin/env python3
"""Collect Google Antigravity (agy) usage into an Omarchy agents-panel JSON record.

Omarchy's stock agents widget monitors ~/.local/state/omarchy/agents/usage/*.json
and automatically creates a tab/chip for each agent found there.

This collector gathers:
- Live rate limits (Gemini and 3P models) via `agy --output-format json --print "/usage"`
- Local prompt and session history from ~/.gemini/antigravity-cli/history.jsonl
- Active model configuration from ~/.gemini/antigravity-cli/settings.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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


def probe_limits(agy_path: str, force: bool = False) -> tuple[list[dict[str, Any]], str, str]:
  """Returns (limits, tier_label, status_text)."""
  cached_data, age = load_cached_limits()
  if not force and cached_data and age < LIMITS_CACHE_SECONDS:
    return cached_data.get("limits", []), cached_data.get("tierLabel", ""), cached_data.get("usageStatusText", "")

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
        return cached_data.get("limits", []), cached_data.get("tierLabel", ""), "Showing cached limits"
      return [], "", "Limits unavailable"

    data = json.loads(res.stdout)
    groups = data.get("command", {}).get("data", {}).get("groups", [])
    limits: list[dict[str, Any]] = []

    for group in groups:
      for bucket in group.get("buckets", []):
        rem = float(bucket.get("remaining_fraction", 1.0))
        used = max(0.0, min(1.0, 1.0 - rem))
        bid = str(bucket.get("id", "")).lower()
        window = str(bucket.get("window", "")).lower()
        reset_time = bucket.get("reset_time", "")

        if "gemini" in bid:
          title = "Weekly" if window == "weekly" else "Session"
          label = f"Gemini ({title})"
        else:
          title = "3P Weekly" if window == "weekly" else "3P Session"
          label = f"Claude/GPT ({title})"

        limits.append({
            "label": label,
            "title": title,
            "percent": round(used, 4),
            "resetsAt": reset_time,
        })

    tier = "Gemini"
    cache_payload = {
        "limits": limits,
        "tierLabel": tier,
        "usageStatusText": "",
    }
    save_cached_limits(cache_payload)
    return limits, tier, ""
  except subprocess.TimeoutExpired:
    if cached_data:
      return cached_data.get("limits", []), cached_data.get("tierLabel", ""), "Showing cached limits"
    return [], "", "Timed out fetching limits"
  except Exception as e:
    if cached_data:
      return cached_data.get("limits", []), cached_data.get("tierLabel", ""), "Showing cached limits"
    return [], "", f"Error: {e}"


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

  # Supplement session count with conversations directory count
  conv_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
  if conv_dir.is_dir():
    try:
      db_count = sum(1 for p in conv_dir.glob("*.db") if p.is_file())
      if db_count > len(sessions):
        total_sessions = db_count
      else:
        total_sessions = len(sessions)
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


def read_settings_tier() -> str:
  settings_file = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
  if settings_file.exists():
    try:
      with settings_file.open("r", encoding="utf-8") as f:
        data = json.load(f)
        model = str(data.get("model") or "").strip()
        if model:
          return model
    except Exception:
      pass
  return "Gemini"


def build_record(force: bool = False) -> dict[str, Any]:
  agy_path = agy_bin()
  stats = collect_local_history()
  tier = read_settings_tier()

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
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": stats["recentDays"],
        "totalPrompts": stats["totalPrompts"],
        "totalSessions": stats["totalSessions"],
        "activeDays": stats["activeDays"],
        "activeDates": stats["activeDates"],
        "modelUsage": {},
        "limits": [],
        "tierLabel": tier,
        "usageStatusText": "agy CLI not found",
        "authHelpText": "Install the Antigravity CLI to view live quota.",
    }

  limits, probed_tier, status_text = probe_limits(agy_path, force=force)
  if probed_tier:
    tier = probed_tier

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
      "todayTotalTokens": 0,
      "todayTokensByModel": {},
      "recentDays": stats["recentDays"],
      "totalPrompts": stats["totalPrompts"],
      "totalSessions": stats["totalSessions"],
      "activeDays": stats["activeDays"],
      "activeDates": stats["activeDates"],
      "modelUsage": {},
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
