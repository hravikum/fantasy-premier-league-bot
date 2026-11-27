// FPL bot: the Slack end of your answers and overrides. Paste this whole file into a Cloudflare Worker
// (README, "Slack").
//
// Taps. Slack sends every tap on the bot's buttons and menus here: a decision's Yes or No, Skip it / Do it
// by a planned transfer, the captain menu, and Undo on an override. The Worker checks the request really
// came from Slack and from you, then writes what you chose into the message under the button ("✅ You
// said yes, Sat 2:02 PM ET"). That note is the record: its block id, answer-<key>-<choice>-<unix time>,
// is what the main run reads back (approvals.records, overrides.py), and a later tap replaces it. Once
// the main run has read them it swaps the preview's controls for a closed note.
//
// /fpl commands. keep, ban, captain, explain and list need FPL's player list, which only the bot has, so
// the Worker starts the workflow's slack-command job on GitHub (overrides.py) and answers you at once;
// the bot's answer appears in the channel a minute or so later. /fpl help is answered here.
//
// Secrets, set in Cloudflare on the Worker (Settings, Variables and Secrets), never in this file:
//   SLACK_SIGNING_SECRET  the Slack app's Signing Secret (api.slack.com/apps, Basic Information)
//   SLACK_BOT_TOKEN       the same xoxb- token as the repository secret
//   SLACK_USER_ID         your Slack member ID; taps and commands from anyone else do nothing
//   GITHUB_TOKEN          a fine-grained token for this repository only, Actions: read and write
//
// Tests: node slack-worker/worker.test.js

const REPO = "hravikum/fantasy-premier-league-bot";
const MAX_AGE = 300; // seconds: an older request is refused, so a copied one can't be sent again later
const VALUE = /^(fpl-[0-9a-z]+)\|([a-z0-9.]+)\|([a-z0-9]+)$/; // what the bot's controls send: code|key|choice
const COMMANDS = ["keep", "ban", "captain", "explain", "list"];
const HELP = "*/fpl* commands, for the coming gameweek, until the main run (about 2 hours before the deadline):\n"
  + "• `/fpl keep Saka`: the bot won't sell him\n"
  + "• `/fpl ban Haaland`: the bot won't buy him\n"
  + "• `/fpl captain Salah`: your captain, if he's in your squad and fit\n"
  + "• `/fpl explain Isak`: why the model rates him as it does\n"
  + "• `/fpl list`: what you've set so far\n"
  + "Each answer has an Undo. Add the club if a name is ambiguous: `/fpl keep Gabriel ARS`.";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("FPL bot: Slack posts here.");
    }
    const body = await request.text();
    if (!(await fromSlack(request, body, env.SLACK_SIGNING_SECRET))) {
      return new Response("bad signature", { status: 401 });
    }
    const form = new URLSearchParams(body);
    if (form.get("command")) {
      return command(form, env);
    }
    const payload = JSON.parse(form.get("payload") || "{}");
    if (payload.type !== "block_actions" || !payload.user || payload.user.id !== env.SLACK_USER_ID) {
      return ok();
    }
    const action = (payload.actions || [])[0];
    const message = payload.message;
    if (!action || !message) {
      return ok();
    }
    const chosen = action.type === "static_select" ? action.selected_option || {} : action;
    const blocks = answered(message.blocks || [], action.block_id, chosen.value || "", Date.now(),
      chosen.text && chosen.text.text);
    if (!blocks) {
      return ok();
    }
    const update = slack("chat.update", {
      channel: (payload.channel && payload.channel.id) || (payload.container && payload.container.channel_id),
      ts: (payload.container && payload.container.message_ts) || message.ts,
      text: message.text || "",
      blocks,
    }, env).then((answer) => {
      if (!answer.ok) {
        console.log(`chat.update failed: ${answer.error}`);
      }
    });
    if (ctx && ctx.waitUntil) {
      ctx.waitUntil(update); // Slack wants an answer within 3 seconds; the message updates a moment later
    } else {
      await update;
    }
    return ok();
  },
};

// /fpl: help here; the rest on GitHub (overrides.py), as only the bot has FPL's player list.
async function command(form, env) {
  if (form.get("user_id") !== env.SLACK_USER_ID) {
    return reply("This bot only takes commands from its owner.");
  }
  const text = (form.get("text") || "").trim().replace(/\s+/g, " ");
  const verb = text.split(" ")[0].toLowerCase();
  if (!verb || verb === "help") {
    return reply(HELP);
  }
  if (!COMMANDS.includes(verb)) {
    return reply(`I don't know \`/fpl ${text}\`.\n${HELP}`);
  }
  if (verb !== "list" && text.split(" ").length < 2) {
    return reply(`Which player? For example \`/fpl ${verb} Saka\`.`);
  }
  const started = await startJob(text, env).catch((error) => ({ status: 0, statusText: String(error) }));
  if (started.status !== 204) {
    return reply(`Couldn't start the bot's job: GitHub answered ${started.status} ${started.statusText}. `
      + "Check the Worker's GITHUB_TOKEN (README, Slack).");
  }
  return reply(`⏳ \`/fpl ${text}\`: on it. The bot answers in the channel in a minute or so.`);
}

// The same request cron-job.org sends for the scheduled runs, for the slack-command job.
function startJob(text, env) {
  return fetch(`https://api.github.com/repos/${REPO}/actions/workflows/bot.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fpl-bot-slack-worker",
      "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: { job: "slack-command", command: text } }),
  });
}

// Slack signs each request with the app's signing secret: v0= and the HMAC-SHA256 of "v0:time:body".
export async function fromSlack(request, body, secret, now = Date.now()) {
  const time = request.headers.get("x-slack-request-timestamp");
  const signature = request.headers.get("x-slack-signature") || "";
  if (!secret || !time || Math.abs(now / 1000 - Number(time)) > MAX_AGE) {
    return false;
  }
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`v0:${time}:${body}`));
  const expected = "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return sameText(expected, signature);
}

function sameText(a, b) {
  // Compares every character whatever happens, so the time taken gives nothing away.
  if (a.length !== b.length) {
    return false;
  }
  let difference = 0;
  for (let i = 0; i < a.length; i++) {
    difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return difference === 0;
}

// The message's blocks with what you chose written under the control you used, replacing an earlier
// choice for the same key; null if it isn't a control the bot made, or the control is gone (closed).
// label: the menu option's text, for the captain.
export function answered(blocks, tappedBlock, value, now = Date.now(), label = "") {
  const match = VALUE.exec(value);
  if (!match || !blocks.some((b) => b.block_id === tappedBlock)) {
    return null;
  }
  const [, , key, choice] = match;
  const text = note(key, choice, clock(now), label);
  if (!text) {
    return null;
  }
  const kept = blocks.filter((b) => !String(b.block_id || "").startsWith(`answer-${key}-`));
  const at = kept.findIndex((b) => b.block_id === tappedBlock);
  kept.splice(at + 1, 0, {
    type: "context",
    block_id: `answer-${key}-${choice}-${Math.floor(now / 1000)}`,
    elements: [{ type: "mrkdwn", text }],
  });
  return kept;
}

// What the note under a control says, by the key's kind (overrides.py lists the keys).
function note(key, choice, when, label) {
  const kind = key.split(".")[0];
  if (/^\d{1,2}$/.test(key) && (choice === "yes" || choice === "no")) {
    return `${choice === "yes" ? "✅" : "❌"} You said *${choice}*, ${when} ET. Tap the other button to change it.`;
  }
  if (kind === "skip" && choice === "on") {
    return `⏭️ Skipped, ${when} ET: the main run re-plans without it. Tap Do it to change that.`;
  }
  if (kind === "skip" && choice === "off") {
    return `▶️ Back on, ${when} ET: the bot decides as planned.`;
  }
  if (kind === "captain" && /^\d+$/.test(choice)) {
    return choice === "0" ? `↩️ Undone, ${when} ET: the bot picks the captain.`
      : `©️ Captain: *${String(label || "your pick").replace(" (the bot’s pick)", "")}*, ${when} ET, if he's fit.`;
  }
  if ((kind === "keep" || kind === "ban") && choice === "off") {
    return `↩️ Undone, ${when} ET.`;
  }
  return null;
}

export function clock(now) {
  return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric",
    minute: "2-digit" }).format(new Date(now));
}

async function slack(method, body, env) {
  const resp = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`, "Content-Type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  });
  return resp.json().catch(() => ({ ok: false, error: `HTTP ${resp.status}` }));
}

function reply(text) {
  return new Response(JSON.stringify({ response_type: "ephemeral", text }),
    { headers: { "Content-Type": "application/json" } });
}

function ok() {
  return new Response("");
}
