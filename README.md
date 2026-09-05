# Antigravity Usage

Adds **Google Antigravity (`agy`)** to Omarchy's **existing AI toolbar widget** — the AI icon already present on the top bar. It does not add a second icon.

After installation, click the AI button on your bar. You will see an **Antigravity** chip alongside your other agents (Claude Code, Grok, Codex):
- **Quota & Limits**: Gemini weekly pool and 5-hour rolling session window, plus 3P model limits (Claude/GPT) and reset countdowns.
- **Activity**: Prompt counts, active session count, and 7-day daily activity chart parsed directly from `~/.gemini/antigravity-cli/history.jsonl`.

## Requirements

- Omarchy with the stock AI agent widget enabled (`omarchy.agents`, enabled by default)
- Python 3 on `PATH` (standard library only, no external dependencies)
- Antigravity CLI (`agy`) installed and authenticated

## Installation

```sh
omarchy plugin add https://github.com/gokivego/omarchy-antigravity-usage.git --enable
```

## How It Works

This plugin is a headless background service (`kinds: ["service"]`). It periodically writes a standard agent usage record to:
`~/.local/state/omarchy/agents/usage/antigravity.json`

The built-in `omarchy.agents` widget continuously monitors that directory and renders the Antigravity tab dynamically.

- **Automatic refresh**: Every 5 minutes.
- **Manual refresh**: Pressing `r` or Enter inside the Omarchy AI panel triggers an immediate scan.

## Removal

```sh
omarchy plugin remove io.github.gokivego.antigravity-usage
```

Removal uninstalls the plugin and clears `~/.local/state/omarchy/agents/usage/antigravity.json`, cleanly removing the Antigravity tab from your top bar without affecting your CLI history or settings.

## License

MIT. See [LICENSE](LICENSE).
