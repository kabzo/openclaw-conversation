"""Conversation agent for OpenClaw."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable
from operator import attrgetter
from typing import Any, Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.components.homeassistant import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    llm,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import yaml as yaml_util

from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MODEL,
    CONF_SYSTEM_PROMPT,
    CONF_TIMEOUT,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONVERSATION_DOMAIN = "conversation"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up conversation entities."""
    async_add_entities([OpenClawConversationEntity(config_entry)])


class OpenClawConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
):
    """OpenClaw conversation agent."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the agent."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="OpenClaw",
            model=entry.data.get(CONF_MODEL, DEFAULT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Process the user input and call the API."""
        options = self.entry.data

        base_prompt = options.get(CONF_SYSTEM_PROMPT) or llm.DEFAULT_INSTRUCTIONS_PROMPT

        prompt_parts: list[str] = [base_prompt]

        user_context = await self._async_get_user_context(user_input)
        if user_context:
            prompt_parts.append(user_context)

        area_context = self._get_area_context(
            user_input.device_id, user_input.satellite_id
        )
        if area_context:
            prompt_parts.append(area_context)

        entities_prompt = self._get_exposed_entities_prompt()
        if entities_prompt:
            prompt_parts.append(entities_prompt)

        full_prompt = "\n".join(prompt_parts)

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                None,
                full_prompt,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        user_id = user_input.context.user_id or "default"
        await self._async_handle_chat_log(chat_log, user_id=user_id)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
        *,
        user_id: str = "default",
    ) -> None:
        """Generate an answer for the chat log via streaming."""
        options = self.entry.data
        base_url: str = options[CONF_BASE_URL]
        api_key: str = options[CONF_API_KEY]
        model: str = options.get(CONF_MODEL, DEFAULT_MODEL)
        timeout_seconds: int = max(
            options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT), DEFAULT_TIMEOUT
        )

        stable_user = f"homeassistant:{user_id}"
        is_followup = any(c.role == "assistant" for c in chat_log.content)

        if is_followup:
            last_user = next(
                c for c in reversed(chat_log.content) if c.role == "user"
            )
            messages = [{"role": "user", "content": last_user.content}]
        else:
            messages = _convert_chat_log_to_messages(chat_log.content)

        _LOGGER.debug(
            "user=%s is_followup=%s roles=%s",
            stable_user,
            is_followup,
            [c.role for c in chat_log.content],
        )
        _LOGGER.debug("Messages to API: %s", messages)

        delta_stream = _stream_api(
            base_url,
            api_key,
            model,
            messages,
            timeout_seconds,
            user=stable_user,
        )

        async for _content in chat_log.async_add_delta_content_stream(
            self.entity_id, delta_stream
        ):
            pass

    async def _async_get_user_context(
        self,
        user_input: conversation.ConversationInput,
    ) -> str | None:
        """Resolve the requesting user to a context string."""
        if user_input.context.user_id is None:
            return None

        user = await self.hass.auth.async_get_user(user_input.context.user_id)
        if user is None:
            return None

        parts: list[str] = [f"The user speaking is {user.name}."]

        if user.is_admin:
            parts.append("They are an administrator.")

        _LOGGER.debug(
            "User context: id=%s, name=%s, is_admin=%s",
            user.id,
            user.name,
            user.is_admin,
        )

        return " ".join(parts)

    @callback
    def _get_area_context(
        self,
        device_id: str | None,
        satellite_id: str | None,
    ) -> str | None:
        """Resolve device_id / satellite_id to area and floor context string."""
        area_id: str | None = None

        if satellite_id is not None:
            entity_reg = er.async_get(self.hass)
            entity_entry = entity_reg.async_get(satellite_id)
            if entity_entry is not None:
                area_id = entity_entry.area_id
                if area_id is None and entity_entry.device_id is not None:
                    device_id = entity_entry.device_id

        if area_id is None and device_id is not None:
            device_reg = dr.async_get(self.hass)
            device = device_reg.async_get(device_id)
            if device is not None:
                area_id = device.area_id

        if area_id is None:
            return None

        area_reg = ar.async_get(self.hass)
        area = area_reg.async_get_area(area_id)
        if area is None:
            return None

        suffix = (
            "and all generic commands like 'turn on the lights' "
            "should target this area."
        )

        if area.floor_id is not None:
            floor_reg = fr.async_get(self.hass)
            floor = floor_reg.async_get_floor(area.floor_id)
            if floor is not None:
                return (
                    f"You are in area {area.name} "
                    f"(floor {floor.name}) {suffix}"
                )

        return f"You are in area {area.name} {suffix}"

    @callback
    def _get_exposed_entities_prompt(self) -> str | None:
        """Gather entities exposed to the conversation assistant."""
        area_registry = ar.async_get(self.hass)
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        entities: list[dict[str, Any]] = []

        for state in sorted(
            self.hass.states.async_all(), key=attrgetter("name")
        ):
            if not async_should_expose(
                self.hass, CONVERSATION_DOMAIN, state.entity_id
            ):
                continue

            entity_entry = entity_registry.async_get(state.entity_id)
            names: list[str] = [state.name]
            if entity_entry is not None and entity_entry.aliases:
                names.extend(entity_entry.aliases)

            area_names: list[str] = []
            if entity_entry is not None:
                if entity_entry.area_id is not None:
                    area_entry = area_registry.async_get_area(
                        entity_entry.area_id
                    )
                    if area_entry is not None:
                        area_names.append(area_entry.name)
                        area_names.extend(area_entry.aliases)
                elif entity_entry.device_id is not None:
                    device_entry = device_registry.async_get(
                        entity_entry.device_id
                    )
                    if (
                        device_entry is not None
                        and device_entry.area_id is not None
                    ):
                        area_entry = area_registry.async_get_area(
                            device_entry.area_id
                        )
                        if area_entry is not None:
                            area_names.append(area_entry.name)
                            area_names.extend(area_entry.aliases)

            info: dict[str, Any] = {
                "names": ", ".join(names),
                "domain": state.domain,
            }
            if area_names:
                info["areas"] = ", ".join(area_names)

            entities.append(info)

        if not entities:
            return None

        return (
            "An overview of the areas and the devices in this smart home:\n"
            + yaml_util.dump(entities)
        )


def _convert_chat_log_to_messages(
    content_list: list[conversation.Content],
) -> list[dict[str, Any]]:
    """Convert the full ChatLog into OpenAI-style messages for the API."""
    messages: list[dict[str, Any]] = []
    for content in content_list:
        if content.role in ("system", "user", "assistant"):
            messages.append({"role": content.role, "content": content.content})
    return messages


async def _stream_api(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout_seconds: int,
    *,
    user: str | None = None,
) -> AsyncIterable[dict[str, Any]]:
    """Stream OpenClaw chat completions API via SSE."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if user is not None:
        payload["user"] = user

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=timeout_seconds,
        sock_read=timeout_seconds,
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise HomeAssistantError(
                    f"OpenClaw returned {resp.status}: {body[:200]}"
                )

            first_chunk = True
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()

                if not decoded or not decoded.startswith("data: "):
                    continue

                data_str = decoded[len("data: "):]
                if data_str == "[DONE]":
                    break

                chunk: dict[str, Any] = json.loads(data_str)
                delta = chunk.get("choices", [{}])[0].get("delta", {})

                if not delta:
                    continue

                if first_chunk:
                    yield {"role": "assistant", "content": delta.get("content", "")}
                    first_chunk = False
                elif content := delta.get("content"):
                    yield {"content": content}
