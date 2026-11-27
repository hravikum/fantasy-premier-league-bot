"""Messages written once, rendered three times: HTML for the inbox, plain text for logs and the
fallback, and Slack posts (to_slack).

A message is a title, a subtitle, status pills and a list of blocks: Heading, Para, Callout (a
coloured box), Table and Bullets. Table cells and text can hold Badge (a small label, e.g. C for
captain) or Tone (coloured text). HTML uses inline styles and tables, which every mail app shows.
"""
import html as html_lib
import re
from dataclasses import dataclass, field, replace

import slack

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
class Button:
    text: str
    href: str
    tone: str | None = None
    plain: str = ""                # what plain text shows instead (the button is HTML only)


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
    slack: list | None = None      # lines (each like text) Slack shows instead


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
    slack: list | None = None      # lines (each like Para text) Slack shows instead of the grid
    slack_rows: list | None = None  # or rows of text, header first, Slack shows as a table under those lines


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
    if isinstance(part, Button):
        return part.plain
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
    if isinstance(part, Button):
        colour = PURPLE if part.tone is None else TONES[part.tone]
        return (f'<a href="{html_lib.escape(part.href, quote=True)}" style="display:inline-block;background:{colour};'
                f'color:#ffffff;text-decoration:none;border-radius:4px;padding:3px 14px;font-size:13px;'
                f'font-weight:700;margin:2px 6px 2px 0">{html_lib.escape(part.text)}</a>')
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


# ---------------------------------------------------------------- Slack

MARKS = {"good": ":white_check_mark:", "bad": ":rotating_light:", "warn": ":warning:", "info": ":information_source:"}
DOTS = {"good": "🟢", "bad": "🔴", "warn": "⚠️", "info": "🔵"}    # where Slack can't colour
NUMBER = re.compile(r"^[£+-]?\d+(\.\d+)?$")         # a table column of these is aligned right
ICONS = (("Transfers", "🔁"), ("Fixtures", "📅"), ("Starting XI", "👕"), ("Lineup", "👕"), ("Plan", "🗓️"),
         ("Chips", "🎟️"), ("Top projections", "📈"))


def text_of(part):
    """A part as plain text, a badge after a space in brackets: "Rogers (OUT)"."""
    if isinstance(part, list):
        return "".join(text_of(p) for p in part)
    return f" ({part.text})" if isinstance(part, Badge) else _text(part)


def dot(tone):
    """A fixture's colour as Slack shows it: 🟢 easier, 🔴 harder, ⚪ average."""
    return {"good": "🟢", "bad": "🔴"}.get(tone, "⚪")


def _md(part):
    """Slack's markup (mrkdwn), escaped. Colour can't go, so Tone keeps only its bold."""
    if isinstance(part, list):
        return "".join(_md(p) for p in part)
    if isinstance(part, Badge):
        return f" `{part.text}`"
    if isinstance(part, Button):
        return slack.link(part.href, part.text) + "   "
    if isinstance(part, Tone):
        text = slack.escape(part.text)
        if not (part.bold and text.strip()):
            return text
        core = text.strip()               # Slack won't bold "*Bench: *": the spaces go outside
        start = text.index(core)
        return f"{text[:start]}*{core}*{text[start + len(core):]}"
    text = slack.escape("" if part is None else str(part))
    return slack.LINK.sub(lambda m: f"<{m.group(0)}>", text)


def _lines(lines):
    return slack.sections("\n".join(_md(line).strip() for line in lines))


def _row_line(row):
    """A table row on one line, for tables without lines of their own: the first cell in bold, the
    rest after it, an arrow cell kept as an arrow."""
    first, *rest = [_md(cell).strip() for cell in row]
    head = f"*{first}*" if first else ""
    line = "  ·  ".join(c for c in [head, *rest] if c).replace("  ·  →  ·  ", "  →  ")
    return head + line[len(head):].replace("  ·  ", "  ", 1) if head and line.startswith(head + "  ·  ") else line


def _slack_block(block):
    if isinstance(block, Heading):
        icon = next((i for word, i in ICONS if block.text.startswith(word)), "")
        return [slack.section(f"{icon}  *{slack.escape(block.text)}*" if icon else f"*{slack.escape(block.text)}*")]
    if isinstance(block, Para):
        if block.slack is not None:
            return _lines(block.slack)
        text = _md(block.text).strip()
        return [slack.context(text)] if block.tone == "muted" and len(text) <= slack.SECTION else slack.sections(text)
    if isinstance(block, Callout):
        title = " ".join(t for t in (MARKS.get(block.tone, ""), f"*{slack.escape(block.title)}*" if block.title else "") if t)
        return slack.sections("\n".join(([title] if title else []) + [_md(line).strip() for line in block.lines]))
    if isinstance(block, Bullets):
        return slack.sections("\n".join("• " + _md(item).strip() for item in block.items))
    if isinstance(block, Table):
        if block.slack_rows is not None:     # a real table, which Slack shows at the bottom of its message
            numbers = {n for n in range(len(block.slack_rows[0]))
                       if all(NUMBER.match(str(row[n])) for row in block.slack_rows[1:])}
            above = _lines(block.slack) if block.slack is not None else \
                [slack.context(slack.escape(block.note))] if block.note else []
            return above + [slack.table(block.slack_rows, right=numbers)]
        if block.slack is not None:          # lines of its own say what the note would
            return _lines(block.slack)
        note = [slack.context(slack.escape(block.note))] if block.note else []
        return note + slack.sections("\n".join(_row_line(row) for row in block.rows))
    raise TypeError(block)


def _fit(blocks, room=slack.MAX_BLOCKS):
    """At most room blocks: anything past that is cut, and says so."""
    return blocks if len(blocks) <= room else blocks[:room - 1] + [slack.context(slack.CUT_SHORT)]


def to_slack(message):
    """The message as Slack posts: a list of {"text", "layouts"}, the first the message in the channel
    and the rest replies in its thread. "text" is what a notification shows; "layouts" are tried in
    turn (slack.post_first): the laid-out version, then the plain text in monospace.

    The message in the channel holds what comes before the first heading (warnings, decisions, team
    news) and the first heading's section (what the bot changes). The rest, the report, goes in its
    thread, its sections between dividers. Tables become lines (Table.slack), except those with
    slack_rows: Slack takes one table a message and shows it at the bottom, so a reply ends with the
    section that has the table, and the next section starts a new reply. More replies too past 50 blocks.
    """
    parts, current = [], []
    for block in message.blocks:
        if current and isinstance(block, Heading):
            parts.append(current)
            current = []
        current.append(block)
    if current:
        parts.append(current)
    main, report = (parts[0] if parts else []), parts[1:]
    if report and not isinstance(main[0], Heading):
        main, report = main + report[0], report[1:]

    title = ("🚨 " if message.alert else "") + message.title
    pills = " · ".join(f"{DOTS[tone]} {text}" if tone in DOTS else text for text, tone in message.pills)
    top = [slack.header(title)]
    if message.subtitle or pills:
        top.append(slack.context("\n".join(slack.escape(t) for t in (message.subtitle, pills) if t)))
    body = []
    for block in main:
        if isinstance(block, Heading) and body:
            body.append(slack.divider())
        body += _slack_block(block)
    footer = re.sub(r"Run: <([^>|]+)>", r"<\1|Run log>", _md(message.footer)) if message.footer else ""
    end = " · ".join(t for t in ("🧵 The full report is in the thread" if report else "", footer) if t)
    laid_out = _fit(top + [slack.divider()] + body + ([slack.divider(), slack.context(end)] if end else []),
                    slack.MAX_BLOCKS - 1)                                  # room for an @-mention
    plain = [slack.header(title)] + slack.monospace(to_text(replace(message, blocks=main)), slack.MAX_BLOCKS - 2)
    posts = [{"text": message.title, "layouts": [laid_out, plain]}]

    replies, blocks, text = [], [], []
    for part in report:
        rendered = [b for block in part for b in _slack_block(block)]
        if blocks and len(blocks) + 1 + len(rendered) > slack.MAX_BLOCKS:
            replies.append((blocks, text))
            blocks, text = [], []
        blocks += ([slack.divider()] if blocks else []) + rendered
        text += part
        if any(b["type"] == "table" for b in rendered):        # the table is this reply's end
            replies.append((blocks, text))
            blocks, text = [], []
    if blocks:
        replies.append((blocks, text))
    for blocks, part in replies:
        posts.append({"text": f"{message.title}: the full report",
                      "layouts": [_fit(blocks), slack.monospace(to_text(Message("", blocks=part)))]})
    return posts
