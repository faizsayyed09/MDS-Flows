import json
from typing import Any

from lfx.base.prompts.api_utils import process_prompt_template
from lfx.custom.custom_component.component import Component
from lfx.inputs.input_mixin import FieldTypes
from lfx.inputs.inputs import DefaultPromptField
from lfx.io import BoolInput, MessageTextInput, Output, PromptInput
from lfx.log.logger import logger
from lfx.schema.dotdict import dotdict
from lfx.schema.message import Message
from lfx.template.utils import update_template_values
from lfx.utils.mustache_security import validate_mustache_template


_RESERVED = {"template", "use_double_brackets", "tool_placeholder"}


def _value_to_text(value):
    if value is None:
        return ""
    if isinstance(value, Message) or hasattr(value, "text"):
        return str(getattr(value, "text", "") or "")
    if hasattr(value, "data") and isinstance(getattr(value, "data"), dict):
        try:
            return json.dumps(value.data, ensure_ascii=False)
        except Exception:
            return str(value.data)
    if isinstance(value, (list, tuple)):
        return "\n".join(_value_to_text(v) for v in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


class PromptComponent(Component):
    display_name: str = "Prompt Template"
    description: str = "Create a prompt template with dynamic variables."
    documentation: str = "https://docs.langflow.org/components-prompts"
    icon = "prompts"
    trace_type = "prompt"
    name = "Prompt Template"

    inputs = [
        PromptInput(name="template", display_name="Template"),
        BoolInput(
            name="use_double_brackets",
            display_name="Use Double Brackets",
            value=False,
            advanced=True,
            info="Use {{variable}} syntax instead of {variable}.",
            real_time_refresh=True,
        ),
        MessageTextInput(
            name="tool_placeholder",
            display_name="Tool Placeholder",
            tool_mode=True,
            advanced=True,
            show=False,
            info="A placeholder input for tool mode.",
        ),
    ]

    outputs = [
        Output(display_name="Prompt", name="prompt", method="build_prompt"),
    ]

    def update_build_config(self, build_config: dotdict, field_value: Any, field_name: str | None = None) -> dotdict:
        """Update the template field type based on the selected mode."""
        if field_name == "use_double_brackets":
            is_mustache = field_value is True
            if is_mustache:
                build_config["template"]["type"] = FieldTypes.MUSTACHE_PROMPT.value
            else:
                build_config["template"]["type"] = FieldTypes.PROMPT.value

            template_value = build_config.get("template", {}).get("value", "")
            if template_value:
                if "custom_fields" not in build_config:
                    build_config["custom_fields"] = {}

                old_custom_fields = build_config["custom_fields"].get("template", [])
                for old_field in list(old_custom_fields):
                    if old_field in old_custom_fields:
                        old_custom_fields.remove(old_field)
                    build_config.pop(old_field, None)

                try:
                    if is_mustache:
                        validate_mustache_template(template_value)
                    _ = process_prompt_template(
                        template=template_value,
                        name="template",
                        custom_fields=build_config["custom_fields"],
                        frontend_node_template=build_config,
                        is_mustache=is_mustache,
                    )
                except ValueError as e:
                    logger.debug(f"Template validation failed during mode switch: {e}")
        return build_config

    async def build_prompt(self) -> Message:
        attributes = dict(self._attributes or {})
        template = str(attributes.get("template") or "")
        use_double_brackets = bool(attributes.get("use_double_brackets", False))

        text = template
        for key, value in attributes.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            replacement = _value_to_text(value)
            if use_double_brackets:
                for token in ("{{" + key + "}}", "{{ " + key + " }}"):
                    text = text.replace(token, replacement)
            else:
                text = text.replace("{" + key + "}", replacement)

        prompt = Message(text=text)
        self.status = text
        return prompt

    def _update_template(self, frontend_node: dict):
        prompt_template = frontend_node["template"]["template"]["value"]
        use_double_brackets = frontend_node["template"].get("use_double_brackets", {}).get("value", False)
        is_mustache = use_double_brackets is True

        try:
            if is_mustache:
                validate_mustache_template(prompt_template)
            custom_fields = frontend_node["custom_fields"]
            frontend_node_template = frontend_node["template"]
            _ = process_prompt_template(
                template=prompt_template,
                name="template",
                custom_fields=custom_fields,
                frontend_node_template=frontend_node_template,
                is_mustache=is_mustache,
            )
        except ValueError as e:
            logger.debug(f"Template validation failed in _update_template: {e}")
        return frontend_node

    async def update_frontend_node(self, new_frontend_node: dict, current_frontend_node: dict):
        """This function is called after the code validation is done."""
        frontend_node = await super().update_frontend_node(new_frontend_node, current_frontend_node)
        template = frontend_node["template"]["template"]["value"]
        use_double_brackets = frontend_node["template"].get("use_double_brackets", {}).get("value", False)
        is_mustache = use_double_brackets is True

        try:
            if is_mustache:
                validate_mustache_template(template)
            _ = process_prompt_template(
                template=template,
                name="template",
                custom_fields=frontend_node["custom_fields"],
                frontend_node_template=frontend_node["template"],
                is_mustache=is_mustache,
            )
        except ValueError as e:
            logger.debug(f"Template validation failed in update_frontend_node: {e}")
        update_template_values(new_template=frontend_node, previous_template=current_frontend_node["template"])
        return frontend_node

    def _get_fallback_input(self, **kwargs):
        return DefaultPromptField(**kwargs)
