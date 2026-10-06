"""Locator strategies for an element the agent acted on.

Generated at record time, while the element is still on screen, and kept only if
each one matches exactly that element right then. Locators cannot be rebuilt
reliably afterwards from logs, which is why this happens during discovery.
"""

import re

from ..models.capability import Css, LabelAnchor, RoleName, RowMatch, Strategy, TableCell, TableRow
from ..screen.screen_interface import Screen

MONEYISH = re.compile(r"^[\s$€£+\-(),.\d%]+$")
# ids with a random-looking tail (ctl00_txtMbrNo_8f3k2) or long digit runs change between renders
RANDOM_ID = re.compile(r"(?:[_-][0-9a-f]{4,}$)|\d{3,}")
NAMED_ROLES = {"button", "link", "checkbox", "radio", "menuitem", "tab", "textbox", "combobox"}
FIELD_ROLES = {"textbox", "combobox", "listbox", "checkbox", "radio"}


def key_columns(el: dict, input_values: set[str]) -> list[tuple[str, str]]:
    """Columns that pick out this element's row, best first.

    Prefer a column whose value is one of the capability's inputs (it becomes a
    ${param}), then plain words ("REGULAR SAVINGS"), then anything with digits.
    Money-like values are a poor key because they change between runs.
    """
    header, row = el.get("header") or [], el.get("row_texts") or []
    ranked = []
    for order, col in enumerate(el.get("unique_columns") or []):
        if not col or col == el.get("column") or col not in header:
            continue
        i = header.index(col)
        value = row[i] if i < len(row) else ""
        if not value:
            continue
        if value in input_values:
            score = 0
        elif MONEYISH.match(value):
            score = 3
        elif re.search(r"\d", value):
            score = 2
        else:
            score = 1
        ranked.append((score, order, col, value))
    return [(col, value) for _, _, col, value in sorted(ranked)]


def candidates(el: dict, input_values: set[str]) -> list[Strategy]:
    out: list[Strategy] = []
    role, name = el.get("role", ""), el.get("name", "")
    in_data_table = bool(el.get("header"))
    if el.get("kind") == "control":
        if name and role in NAMED_ROLES:
            out.append(RoleName(role=role, name=name))
        if el.get("label") and role in FIELD_ROLES:
            out.append(LabelAnchor(label=el["label"], role=role))
        if in_data_table and name:
            for col, value in key_columns(el, input_values)[:2]:
                out.append(TableRow(row=RowMatch(column=col, equals=value), role=role, name=name))
    else:  # a text cell holding a value
        if in_data_table and el.get("column"):
            for col, value in key_columns(el, input_values)[:2]:
                out.append(TableCell(row=RowMatch(column=col, equals=value), column=el["column"]))
        elif not in_data_table and el.get("pos"):
            label = (el.get("row_texts") or [""])[el["pos"] - 1]
            if label and not MONEYISH.match(label):
                out.append(LabelAnchor(label=label, role="cell"))
    if el.get("attr_name"):
        out.append(Css(selector=f'{el["tag"]}[name="{el["attr_name"]}"]'))
    if el.get("id") and not RANDOM_ID.search(el["id"]) and re.fullmatch(r"[A-Za-z][\w-]*", el["id"]):
        out.append(Css(selector=f'#{el["id"]}'))
    return out


async def proven(screen: Screen, el: dict, input_values: set[str]) -> list[Strategy]:
    """Candidates that match exactly this element on the live screen, in preference order."""
    keep = []
    for strategy in candidates(el, input_values):
        if await screen.check_strategy(el.get("frame"), strategy.model_dump(), el["ref"]):
            keep.append(strategy)
    return keep
