"""Character data model — YAML loading and validation.

The :class:`Character` dataclass represents a player character card loaded from
a YAML configuration file.  It holds player attributes and skills without any
NPC personality/roleplay data (those belong to :class:`NPCCharacter`).

Typical usage::

    from trpg_agent.character import Character

    char = Character.load("config.yaml")
    summary = char.summary()
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml


# ---------------------------------------------------------------------------
#  Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Character:
    """Immutable (by convention) player character card.

    Parameters
    ----------
    name : str
        Character name.
    core : list of str
        Background / role description lines.
    attributes : dict of str → int
        Numeric attributes such as strength, agility, etc.
    skills : list of dict
        Each entry has ``name`` (str) and ``value`` (int) keys.
    pathway : str
        LotM beyonder pathway name (e.g. "占卜家", "观众").
    sequence : int
        LotM sequence level (9=lowest, 0=god).  Default 9.
    anchor : str
        Character's mental anchor description.
    beyonder_abilities : list of dict
        Each entry has ``name`` and ``description`` keys.
    """

    name: str
    core: List[str]
    attributes: Dict[str, int] = field(default_factory=dict)
    skills: List[Dict[str, Any]] = field(default_factory=list)
    pathway: str = ""
    sequence: int = 9
    anchor: str = ""
    relations: Dict[str, str] = field(default_factory=dict)
    beyonder_abilities: List[Dict[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        """Return a concise summary of character attributes and skills."""
        lines: List[str] = [f"角色：{self.name}"]

        if self.pathway:
            lines.append("")
            lines.append("【非凡途径】")
            lines.append(f"  途径：{self.pathway}")
            lines.append(f"  序列：序列{self.sequence}")
            if self.anchor:
                lines.append(f"  锚：{self.anchor}")

        if self.relations:
            lines.append("")
            lines.append("【人物关系】")
            for k, v in self.relations.items():
                lines.append(f"  {k}: {v}")

        if self.beyonder_abilities:
            lines.append("")
            lines.append("【非凡能力】")
            for ab in self.beyonder_abilities:
                lines.append(f"  {ab.get('name', '')}: {ab.get('description', '')}")

        if self.attributes:
            lines.append("")
            lines.append("【属性】")
            for k, v in self.attributes.items():
                lines.append(f"  {k}: {v}")

        if self.skills:
            lines.append("")
            lines.append("【技能】")
            for skill in self.skills:
                lines.append(f"  {skill['name']}: {skill['value']}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    #  Class-method alias
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str) -> Character:
        """Load a character from *config_path*.

        Equivalent to :func:`load_character`.
        """
        return load_character(config_path)

    @classmethod
    def from_dict(cls, data: dict) -> Character:
        """Build a character from an already-parsed mapping.

        Used by the web start screen, which posts a structured player card
        instead of editing config.yaml.  Unlike :func:`load_character` this
        raises ``ValueError`` rather than calling ``sys.exit``, so an API
        request can report the problem instead of killing the server.
        """
        if not isinstance(data, dict):
            raise ValueError("角色卡必须是对象")

        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("角色卡缺少 name")

        raw_core = data.get("core") or []
        if isinstance(raw_core, str):
            core = [c.strip() for c in raw_core.replace("；", ";").split(";") if c.strip()]
        else:
            core = [str(c).strip() for c in raw_core if str(c).strip()]
        if not core:
            raise ValueError("角色卡缺少 core（角色背景）")

        raw_attrs = data.get("attributes") or {}
        if not isinstance(raw_attrs, dict) or not raw_attrs:
            raise ValueError("角色卡缺少 attributes（属性）")
        attributes: dict[str, int] = {}
        for key, value in raw_attrs.items():
            try:
                attributes[str(key)] = max(1, min(20, int(value)))
            except (TypeError, ValueError):
                raise ValueError(f"属性「{key}」必须是 1-20 的整数")

        skills: list[dict[str, Any]] = []
        for entry in data.get("skills") or []:
            if not isinstance(entry, dict):
                continue
            sname = str(entry.get("name") or "").strip()
            if not sname:
                continue
            try:
                svalue = max(1, min(99, int(entry.get("value", 50))))
            except (TypeError, ValueError):
                svalue = 50
            skills.append({"name": sname, "value": svalue})

        relations = data.get("relations") or {}
        if not isinstance(relations, dict):
            relations = {}

        try:
            sequence = int(data.get("sequence", 9))
        except (TypeError, ValueError):
            sequence = 9

        return cls(
            name=name,
            core=core,
            attributes=attributes,
            skills=skills,
            pathway=str(data.get("pathway") or "").strip(),
            sequence=sequence,
            anchor=str(data.get("anchor") or "").strip(),
            relations={str(k): str(v) for k, v in relations.items()},
            beyonder_abilities=list(data.get("beyonder_abilities") or []),
        )


# ---------------------------------------------------------------------------
#  Module-level loader
# ---------------------------------------------------------------------------

def load_character(config_path: str) -> Character:
    """Load and validate a :class:`Character` from a YAML file.

    Parameters
    ----------
    config_path : str
        Path to the YAML configuration file.

    Returns
    -------
    Character
        A fully populated character instance.

    Raises
    ------
    SystemExit
        If the file cannot be read, the YAML is malformed, or required
        fields are missing (the message is a friendly description rather
        than a raw traceback).
    """
    # --- file I/O ---
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"错误：找不到配置文件 {config_path}")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式有误 — {e}")
        sys.exit(1)

    # --- top-level key ---
    if not isinstance(data, dict) or "player" not in data:
        print("错误：配置文件中缺少 'player' 字段")
        sys.exit(1)

    char_data = data["player"]

    # --- required field validation ---
    # Each required field is checked individually so the user gets a clear,
    # specific message instead of a generic error.
    _require_field(char_data, "name", "角色名称")
    _require_field(char_data, "core", "角色背景")
    _require_field(char_data, "attributes", "属性")

    return Character(
        name=char_data["name"],
        core=char_data["core"],
        attributes=char_data["attributes"],
        skills=char_data.get("skills", []),
        pathway=char_data.get("pathway", ""),
        sequence=char_data.get("sequence", 9),
        anchor=char_data.get("anchor", ""),
        relations=char_data.get("relations", {}),
        beyonder_abilities=char_data.get("beyonder_abilities", []),
    )


def _require_field(data: dict, key: str, cn_name: str) -> None:
    """Check that *key* exists in *data* and is truthy; exit otherwise."""
    if key not in data or not data[key]:
        print(f"错误：角色配置缺少必填字段「{cn_name}」({key})")
        sys.exit(1)
