def normalize_inputs(required: dict, optional: dict, prefix: str = "") -> dict:
    """保留预检需要的标准字段，按 V3 规则展开点分输入，不保存 UI 默认内容。"""
    fields = {}
    for key, declaration in {**optional, **required}.items():
        if not declaration or not isinstance(declaration[0], (str, list)):
            continue
        kind = declaration[0]
        options = (
            declaration[1] if len(declaration) > 1 and isinstance(declaration[1], dict) else {}
        )
        name = prefix + key
        if kind == "COMFY_AUTOGROW_V3":
            template = options.get("template", {})
            minimum, maximum = template.get("min"), template.get("max")
            names = template.get("names")
            if (
                isinstance(template.get("prefix"), str)
                and type(maximum) is int
                and 1 <= maximum <= 100
            ):
                names = [template["prefix"] + str(index) for index in range(maximum)]
            inputs = template.get("input", {})
            slots = {**inputs.get("optional", {}), **inputs.get("required", {})}
            if (
                isinstance(names, list)
                and all(isinstance(item, str) for item in names)
                and type(minimum) is int
                and 0 <= minimum <= len(names) <= 100
                and len(slots) == 1
            ):
                slot, value = next(iter(slots.items()))
                for index, child in enumerate(names):
                    mandatory = index < minimum and slot in inputs.get("required", {})
                    fields.update(
                        normalize_inputs(
                            {child: value} if mandatory else {},
                            {} if mandatory else {child: value},
                            name + ".",
                        )
                    )
                continue
        field = {
            "type": kind,
            "required": key in required,
            **{
                limit: options[limit]
                for limit in ("min", "max")
                if type(options.get(limit)) in (int, float)
            },
        }
        if kind == "COMBO" and options.get("multiselect") is not True:
            choices = options.get("options")
            if isinstance(choices, list) and all(
                type(item) in (str, int, float, bool) for item in choices
            ):
                field["type"] = choices
        elif kind == "COMFY_DYNAMICCOMBO_V3":
            choices = options.get("options")
            if isinstance(choices, list) and all(
                isinstance(item, dict)
                and isinstance(item.get("key"), str)
                and isinstance(item.get("inputs"), dict)
                for item in choices
            ):
                field["type"] = [item["key"] for item in choices]
                field["branches"] = {
                    item["key"]: normalize_inputs(
                        item["inputs"].get("required", {}),
                        item["inputs"].get("optional", {}),
                        name + ".",
                    )
                    for item in choices
                }
        for media in ("image", "video"):
            if options.get(media + "_upload") is True:
                field["upload"] = media
        fields[name] = field
    return fields


def select_inputs(fields: dict, values: dict) -> dict:
    selected = dict(fields)
    for key, field in fields.items():
        choice = values.get(key)
        if isinstance(choice, str) and choice in field.get("branches", {}):
            selected.update(select_inputs(field["branches"][choice], values))
    return selected
