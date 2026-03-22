# OpenClaw Conversation for Home Assistant

[![HACS](https://img.shields.io/badge/HACS-Custom-orange?style=flat-square)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/kabzo/openclaw-conversation?style=flat-square)](https://github.com/kabzo/openclaw-conversation/releases)
[![License](https://img.shields.io/github/license/kabzo/openclaw-conversation?style=flat-square)](LICENSE)

**Turn your [OpenClaw](https://openclaw.ai) agent into a Home Assistant voice assistant.**

Say a wake word, ask a question, get a spoken answer — powered by your own OpenClaw agent with all its tools, memory, and personality.

```
Wake word → STT → OpenClaw Agent → TTS → Speaker
```

## Features

- Full OpenClaw agent as a HA conversation agent (tools, memory, MCP integrations)
- **Streaming responses** — TTS starts speaking before the full reply is generated
- **Sticky sessions** — each HA user gets one persistent OpenClaw session via the `user` field, so your agent remembers prior conversations
- **Multi-turn conversations** — when the agent asks a clarifying question, the mic stays open and follow-ups are routed to the same session
- **User context** — the agent knows which HA user is speaking and whether they're an admin
- **Area/floor awareness** — voice satellites automatically provide room context
- **Exposed entities** — HA entities exposed to the conversation agent are included in the system prompt
- Works with any STT/TTS engine (Gemini, Whisper, Piper, HA Cloud, etc.)
- Works with ESP32-based voice satellites

## How it works

```
┌─────────────┐     ┌──────────────────────┐     ┌──────────────────┐
│  ESP32 / App │────▶│   Home Assistant      │────▶│  OpenClaw Gateway│
│  (STT/TTS)  │◀────│   Assist Pipeline     │◀────│  (your agent)    │
└─────────────┘     └──────────────────────┘     └──────────────────┘
```

1. User speaks → STT transcribes to text
2. HA builds a system prompt (user context + area + exposed entities + time)
3. Integration sends the message to the OpenClaw Gateway via `/v1/chat/completions`
4. Gateway streams the response back via SSE
5. HA feeds the stream to TTS, which starts speaking immediately
6. If the agent's response ends with `?`, HA keeps the mic open for a follow-up

Session persistence: every API call includes `"user": "homeassistant:{user_id}"`, so the gateway maintains a single persistent session per HA user. Follow-up messages within the same HA conversation send only the latest user message (the gateway already has the history).

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the 3 dots menu > **Custom repositories**
3. Add `kabzo/openclaw-conversation` as **Integration**
4. Search for and install **OpenClaw Conversation**
5. Restart Home Assistant
6. Go to **Settings** > **Integrations** > **Add Integration** > **OpenClaw Conversation**

### Manual

Copy `custom_components/openclaw_conversation` into your HA `config/custom_components/` directory and restart.

### Docker (development)

```yaml
version: "3"
services:
  homeassistant:
    image: ghcr.io/home-assistant/homeassistant:stable
    container_name: homeassistant
    volumes:
      - ha_config:/config
      - ./custom_components:/config/custom_components
    ports:
      - "127.0.0.1:8123:8123"
    restart: unless-stopped

volumes:
  ha_config:
```

## Configuration

### 1. Enable Chat Completions on your OpenClaw Gateway

Add this to your `openclaw.json` inside the `gateway` block:

```json
{
  "gateway": {
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true }
      }
    }
  }
}
```

Restart your gateway after the change.

### 2. Add the integration

**Settings > Devices & Services > Add Integration > OpenClaw Conversation**

| Field | Description | Example |
|-------|-------------|---------|
| Name | Display name in HA | `Claw` |
| Gateway URL | Your OpenClaw Gateway address | `http://192.168.1.100:18789` |
| API Token | Gateway auth token | (from `gateway.auth.token`) |
| Model | Agent selector | `openclaw:home` or `openclaw` (default) |
| Timeout | Seconds before giving up on a response | `90` (default) |
| System Prompt | Override the default HA instructions prompt | (optional, leave blank for default) |

**Model field**: use `openclaw:<agent_id>` to target a specific OpenClaw agent (e.g., `openclaw:home`). Use `openclaw` for the default/main agent.

### 3. Set up a Voice Assistant

**Settings > Voice Assistants** > create or edit an assistant:

- **Conversation agent**: select your OpenClaw entity
- **Speech-to-Text**: your preferred STT engine
- **Text-to-Speech**: your preferred TTS engine
- **Wake word**: e.g. "Ok Nabu" via openWakeWord

### 4. Assign to a voice device

For ESP32 satellites or Voice PE: set **Preferred Assistant** to your OpenClaw assistant in the device settings.

## Network notes

- HA must reach your OpenClaw Gateway over HTTP
- If they're on different machines, use the gateway's LAN IP (not `127.0.0.1`)
- Default gateway port is `18789`
- Docker users: `127.0.0.1` inside the container refers to the container itself — use the host's LAN IP

## Architecture

### Session management

The integration uses the OpenAI-compatible `user` field to maintain persistent sessions:

- Each HA user maps to `homeassistant:{user_id}` — one persistent OpenClaw session per user
- The gateway derives a stable session key from the `user` field
- All messages from the same HA user thread into one session, even across separate HA conversations
- The agent retains full conversation history on the gateway side

### Follow-up detection

When the agent asks a clarifying question (response ends with `?`):

1. HA's `continue_conversation` heuristic keeps the mic open
2. The user's follow-up answer arrives in the same HA ChatLog session
3. The integration detects prior assistant messages in the ChatLog and sends **only the latest user message** to the gateway (no redundant history)
4. The gateway already has the context from the persistent session

### System prompt

On the first message of each HA conversation, the integration sends `[system, user]`. The system prompt includes:

- Base instructions (configurable or HA's default LLM prompt)
- User identity (name, admin status)
- Area and floor context (from the voice satellite or device)
- Exposed entity list (names, domains, areas)
- Current date and time

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Cannot connect to gateway | Check URL: `curl http://<ip>:18789/v1/chat/completions`. Check firewall. Don't use `127.0.0.1` across machines. |
| Endpoint disabled (405) | Enable `chatCompletions` in `openclaw.json`, restart gateway |
| Invalid auth (401) | Check token matches `gateway.auth.token` in your config |
| "Unexpected error during intent recognition" | Check HA logs: likely a timeout. Increase timeout in integration config or reconfigure. |
| Agent not in dropdown | Restart HA after installing. Check logs for errors. |
| Two sessions in OpenClaw | Make sure you're on v0.3.0+ which uses the `user` field for session persistence. |

### Debug logging

Add this to your HA `configuration.yaml` to see detailed integration logs:

```yaml
logger:
  logs:
    custom_components.openclaw_conversation: debug
```

## Prerequisites

- [OpenClaw Gateway](https://openclaw.ai) with Chat Completions endpoint enabled
- Home Assistant 2025.1+

## Links

- [OpenClaw](https://openclaw.ai)
- [OpenClaw Documentation](https://docs.clawd.bot)
- [OpenClaw Chat Completions API](https://docs.clawd.bot/gateway/openai-http-api)
- [Home Assistant Voice](https://www.home-assistant.io/voice_control/)
- [HACS](https://hacs.xyz/)

## License

MIT
