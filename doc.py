"""Emails written once, rendered twice: HTML for the inbox, plain text for logs and the fallback.

A message is a title, a subtitle, status pills and a list of blocks: Heading, Para, Callout (a
coloured box), Table and Bullets. Table cells and text can hold Badge (a small label, e.g. C for
captain) or Tone (coloured text). HTML uses inline styles and tables, which every mail app shows.
"""
import html as html_lib
from dataclasses import dataclass, field

PURPLE, GREEN, RED, AMBER, BLUE = "#37003c", "#00a85a", "#d0004a", "#b45309", "#1d4ed8"
TONES = {"good": GREEN, "bad": RED, "warn": AMBER, "info": BLUE, "muted": "#6b7280", None: "#1a1a1a"}
BOXES = {"good": ("#ecfdf3", GREEN), "bad": ("#fdecf2", RED), "warn": ("#fff7e6", AMBER),
         "info": ("#eef4ff", BLUE), None: ("#f4f4f7", "#9ca3af")}
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


@dataclass
class Badge:
    text: str
    tone: str | None = None        # None = purple


@dataclass
class Tone:
    text: str
    tone: str | None = None
    bold: bool = False


@dataclass
class Heading:
    text: str


@dataclass
class Para:
    text: object                   # str, Tone, Badge or a list of them
    tone: str | None = None


@dataclass
class Callout:
    title: str
    lines: list
    tone: str | None = None


@dataclass
class Bullets:
    items: list


@dataclass
class Table:
    headers: list
    rows: list
    right: set = field(default_factory=set)        # column numbers to right-align (numbers)
    row_tones: dict = field(default_factory=dict)  # row number -> tone, for a tinted row
    note: str = ""


@dataclass
class Message:
    title: str
    subtitle: str = ""
    pills: list = field(default_factory=list)      # [(text, tone)]
    blocks: list = field(default_factory=list)
    footer: str = ""
    alert: bool = False                            # red header: something needs you


# ---------------------------------------------------------------- plain text

def _text(part):
    if isinstance(part, list):
        return "".join(_text(p) for p in part)
    if isinstance(part, Badge):
        return f"({part.text})"
    if isinstance(part, Tone):
        return part.text
    return "" if part is None else str(part)


def to_text(message):
    out = [message.title + (f" | {message.subtitle}" if message.subtitle else "")]
    if message.pills:
        out.append(" · ".join(text for text, _ in message.pills))
    for block in message.blocks:
        out.append("")
        if isinstance(block, Heading):
            out.append(block.text.upper())
        elif isinstance(block, Para):
            out.append(_text(block.text))
        elif isinstance(block, Callout):
            out.append(block.title.upper() if block.title else "")
            out += ["  " + _text(line) for line in block.lines]
        elif isinstance(block, Bullets):
            out += ["  - " + _text(item) for item in block.items]
        elif isinstance(block, Table):
            cells = [[_text(c) for c in block.headers]] + [[_text(c) for c in row] for row in block.rows]
            widths = [max(len(r[i]) for r in cells) for i in range(len(block.headers))]
            for r in cells:
                out.append("  " + "  ".join(c.rjust(w) if i in block.right else c.ljust(w)
                                             for i, (c, w) in enumerate(zip(r, widths))).rstrip())
            if block.note:
                out.append("  " + block.note)
    if message.footer:
        out += ["", message.footer]
    return "\n".join(line for line in out if line is not None).strip() + "\n"


# ---------------------------------------------------------------- HTML

def _h(part):
    if isinstance(part, list):
        return "".join(_h(p) for p in part)
    if isinstance(part, Badge):
        colour = PURPLE if part.tone is None else TONES[part.tone]
        return (f'<span style="display:inline-block;background:{colour};color:#fff;border-radius:3px;'
                f'padding:0 5px;font-size:11px;font-weight:700;line-height:17px;margin-left:4px">'
                f'{html_lib.escape(part.text)}</span>')
    if isinstance(part, Tone):
        weight = "font-weight:600;" if part.bold else ""
        return f'<span style="color:{TONES[part.tone]};{weight}">{html_lib.escape(part.text)}</span>'
    return html_lib.escape("" if part is None else str(part))


def _block(block):
    if isinstance(block, Heading):
        return (f'<div style="font-size:15px;font-weight:700;color:{PURPLE};margin:22px 0 8px;'
                f'padding-bottom:4px;border-bottom:2px solid {PURPLE}">{html_lib.escape(block.text)}</div>')
    if isinstance(block, Para):
        return f'<div style="margin:8px 0;font-size:14px;color:{TONES[block.tone]}">{_h(block.text)}</div>'
    if isinstance(block, Callout):
        background, edge = BOXES[block.tone]
        title = (f'<div style="font-weight:700;margin-bottom:4px;color:{edge}">{html_lib.escape(block.title)}</div>'
                 if block.title else "")
        lines = "".join(f'<div style="margin:3px 0">{_h(line)}</div>' for line in block.lines)
        return (f'<div style="background:{background};border-left:4px solid {edge};border-radius:4px;'
                f'padding:10px 12px;margin:12px 0;font-size:14px">{title}{lines}</div>')
    if isinstance(block, Bullets):
        items = "".join(f'<li style="margin:3px 0">{_h(item)}</li>' for item in block.items)
        return f'<ul style="margin:8px 0;padding-left:20px;font-size:14px">{items}</ul>'
    if isinstance(block, Table):
        align = lambda i: "right" if i in block.right else "left"
        head = "".join(f'<th style="text-align:{align(i)};padding:6px 8px;background:#f4f4f7;font-size:12px;'
                       f'color:#4b5563;font-weight:600;border-bottom:1px solid #e5e7eb;white-space:nowrap">'
                       f'{_h(h)}</th>' for i, h in enumerate(block.headers))
        rows = []
        for n, row in enumerate(block.rows):
            tint = BOXES[block.row_tones[n]][0] if n in block.row_tones else "#ffffff"
            cells = "".join(f'<td style="text-align:{align(i)};padding:6px 8px;border-bottom:1px solid #eef0f3;'
                            f'vertical-align:top">{_h(c)}</td>' for i, c in enumerate(row))
            rows.append(f'<tr style="background:{tint}">{cells}</tr>')
        note = (f'<div style="font-size:12px;color:#6b7280;margin-top:4px">{html_lib.escape(block.note)}</div>'
                if block.note else "")
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;'
                f'font-size:13px;margin:6px 0">{head and "<tr>" + head + "</tr>"}{"".join(rows)}</table>{note}')
    raise TypeError(block)


def to_html(message):
    header = RED if message.alert else PURPLE
    pills = "".join(
        f'<span style="display:inline-block;background:{TONES[tone] if tone else "rgba(255,255,255,.18)"};'
        f'color:#fff;border-radius:10px;padding:2px 9px;font-size:12px;margin:8px 6px 0 0">{html_lib.escape(text)}</span>'
        for text, tone in message.pills)
    body = "".join(_block(b) for b in message.blocks)
    footer = (f'<tr><td style="padding:12px 20px;font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb">'
              f'{_h(message.footer)}</td></tr>' if message.footer else "")
    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:#f4f4f7">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7">'
        f'<tr><td align="center" style="padding:16px 8px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;'
        f'background:#ffffff;border-radius:8px;font-family:{FONT};color:#1a1a1a;line-height:1.4">'
        f'<tr><td style="background:{header};color:#ffffff;padding:18px 20px;border-radius:8px 8px 0 0">'
        f'<div style="font-size:20px;font-weight:700">{html_lib.escape(message.title)}</div>'
        f'<div style="font-size:13px;opacity:.85;margin-top:2px">{html_lib.escape(message.subtitle)}</div>{pills}</td></tr>'
        f'<tr><td style="padding:6px 20px 18px">{body}</td></tr>{footer}'
        f'</table></td></tr></table></body></html>')
