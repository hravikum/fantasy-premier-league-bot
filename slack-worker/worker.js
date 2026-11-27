// FPL bot: the Slack end of the Yes/No buttons on a preview's decisions. Paste this whole file into a
// Cloudflare Worker (README, "Slack").
//
// Slack sends every tap on a Yes or No to this Worker's address. It checks the request really came from
// Slack and from you, then writes your answer into the message under that decision's buttons
// ("✅ You said yes, Sat 2:02 PM ET"). That note is the record: its block id, answer-<n>-<yes|no>-<unix
// time>, is what the main run reads back (approvals.slack_answers), and a later tap replaces it. The
// Worker keeps nothing itself and starts nothing on GitHub. Once the main run has read your answers it
// swaps the buttons for a closed note, so there's nothing left to tap.
//
// Secrets, set in Cloudflare on the Worker (Settings, Variables and Secrets), never in this file:
//   SLACK_SIGNING_SECRET  the Slack app's Signing Secret (api.slack.com/apps, Basic Information)
//   SLACK_BOT_TOKEN       the same xoxb- token as the repository secret
//   SLACK_USER_ID         your Slack member ID; a tap from anyone else does nothing
//
// Tests: node slack-worker/worker.test.js

const MAX_AGE = 300; // seconds: an older request is refused, so a copied one can't be sent again later
const VALUE = /^(fpl-[0-9a-z]+)\|(\d{1,2})\|(yes|no)$/; // approvals.buttons: code|decision|answer

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("FPL bot: Slack posts here.");
    }
    const body = await request.text();
    if (!(await fromSlack(request, body, env.SLACK_SIGNING_SECRET))) {
      return new Response("bad signature", { status: 401 });
    }
    const payload = JSON.parse(new URLSearchParams(body).get("payload") || "{}");
    if (payload.type !== "block_actions" || !payload.user || payload.user.id !== env.SLACK_USER_ID) {
      return ok();
    }
    const action = (payload.actions || [])[0];
    const message = payload.message;
    const blocks = action && message && answered(message.blocks || [], action.block_id, action.value || "");
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

// The message's blocks with your answer to one decision written under its buttons, replacing an earlier
// answer to it; null if the tap isn't a Yes/No the bot made, or its buttons are gone (closed).
export function answered(blocks, tappedBlock, value, now = Date.now()) {
  const match = VALUE.exec(value);
  if (!match || !blocks.some((b) => b.block_id === tappedBlock)) {
    return null;
  }
  const [, , number, answer] = match;
  const kept = blocks.filter((b) => !String(b.block_id || "").startsWith(`answer-${number}-`));
  const at = kept.findIndex((b) => b.block_id === tappedBlock);
  kept.splice(at + 1, 0, {
    type: "context",
    block_id: `answer-${number}-${answer}-${Math.floor(now / 1000)}`,
    elements: [mrkdwn(`${answer === "yes" ? "✅" : "❌"} You said *${answer}*, ${clock(now)} ET. `
      + "Tap the other button to change it.")],
  });
  return kept;
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

function ok() {
  return new Response("");
}

function mrkdwn(text) {
  return { type: "mrkdwn", text };
}
