// Tests for the Slack Worker. Run: node slack-worker/worker.test.js (Node 18 or later; nothing to install).
// Nothing here reaches Slack: fetch is replaced by a fake that records each request.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import worker, { answered, clock, fromSlack } from "./worker.js";

const ENV = { SLACK_SIGNING_SECRET: "shh", SLACK_BOT_TOKEN: "xoxb-test", SLACK_USER_ID: "U0HARI" };
const NOW = Date.parse("2026-11-28T19:02:00Z"); // Sat 2:02 PM in New York

function signed(payload, { secret = "shh", time = Math.floor(Date.now() / 1000), method = "POST" } = {}) {
  const body = new URLSearchParams({ payload: JSON.stringify(payload) }).toString();
  const signature = "v0=" + createHmac("sha256", secret).update(`v0:${time}:${body}`).digest("hex");
  return new Request("https://fpl-bot.example.workers.dev/", { method, body: method === "POST" ? body : undefined,
    headers: { "x-slack-request-timestamp": String(time), "x-slack-signature": signature,
      "content-type": "application/x-www-form-urlencoded" } });
}

let sent = [];
globalThis.fetch = async (url, init) => {
  sent.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
  return new Response(JSON.stringify({ ok: true }), { status: 200 });
};

function preview() {
  return [
    { type: "header", block_id: "h", text: { type: "plain_text", text: "GW13 preview" } },
    { type: "section", block_id: "d1", text: { type: "mrkdwn", text: "*1.* Take 1 hit" } },
    { type: "actions", block_id: "b1", elements: [
      { type: "button", value: "fpl-3f9a1c|1|yes", action_id: "fpl-3f9a1c|1|yes" },
      { type: "button", value: "fpl-3f9a1c|1|no", action_id: "fpl-3f9a1c|1|no" }] },
    { type: "section", block_id: "d2", text: { type: "mrkdwn", text: "*2.* Play Bench Boost" } },
    { type: "actions", block_id: "b2", elements: [
      { type: "button", value: "fpl-3f9a1c|2|yes", action_id: "fpl-3f9a1c|2|yes" },
      { type: "button", value: "fpl-3f9a1c|2|no", action_id: "fpl-3f9a1c|2|no" }] },
  ];
}

function tap(value, blockId, { user = "U0HARI", blocks = preview() } = {}) {
  return { type: "block_actions", user: { id: user }, channel: { id: "C0CHAN" },
    container: { message_ts: "1.2", channel_id: "C0CHAN" },
    message: { ts: "1.2", text: "GW13 preview", blocks },
    actions: [{ action_id: value, value, block_id: blockId }] };
}

const tests = {
  "the time is shown in New York": () => {
    assert.equal(clock(NOW), "Sat 2:02 PM");
  },

  "a tap writes your answer under that decision's buttons": () => {
    const blocks = answered(preview(), "b1", "fpl-3f9a1c|1|yes", NOW);
    assert.deepEqual(blocks.map((b) => b.block_id), ["h", "d1", "b1", `answer-1-yes-${NOW / 1000}`, "d2", "b2"]);
    assert.equal(blocks[3].elements[0].text, "✅ You said *yes*, Sat 2:02 PM ET. Tap the other button to change it.");
  },

  "a later tap replaces the earlier answer, and other decisions keep theirs": () => {
    let blocks = answered(preview(), "b2", "fpl-3f9a1c|2|yes", NOW);
    blocks = answered(blocks, "b1", "fpl-3f9a1c|1|yes", NOW);
    blocks = answered(blocks, "b1", "fpl-3f9a1c|1|no", NOW + 60000);
    assert.deepEqual(blocks.map((b) => b.block_id),
      ["h", "d1", "b1", `answer-1-no-${NOW / 1000 + 60}`, "d2", "b2", `answer-2-yes-${NOW / 1000}`]);
    assert.ok(blocks[3].elements[0].text.startsWith("❌ You said *no*"));
  },

  "a value the bot didn't make, or buttons that are gone, change nothing": () => {
    assert.equal(answered(preview(), "b1", "fpl-3f9a1c|1|maybe", NOW), null);
    assert.equal(answered(preview(), "b1", '{"bid": 4}', NOW), null);
    assert.equal(answered(preview(), "gone", "fpl-3f9a1c|1|yes", NOW), null);
  },

  "your tap updates the message in Slack": async () => {
    sent = [];
    const response = await worker.fetch(signed(tap("fpl-3f9a1c|2|no", "b2")), ENV);
    assert.equal(response.status, 200);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].url, "https://slack.com/api/chat.update");
    assert.equal(sent[0].body.channel, "C0CHAN");
    assert.equal(sent[0].body.ts, "1.2");
    assert.ok(sent[0].body.blocks.some((b) => /^answer-2-no-\d+$/.test(b.block_id)));
  },

  "a tap from anyone else does nothing": async () => {
    sent = [];
    const response = await worker.fetch(signed(tap("fpl-3f9a1c|1|yes", "b1", { user: "U0SOMEONE" })), ENV);
    assert.equal(response.status, 200);
    assert.equal(sent.length, 0);
  },

  "a request not signed with the app's secret, or too old, is refused": async () => {
    sent = [];
    assert.equal((await worker.fetch(signed(tap("fpl-3f9a1c|1|yes", "b1"), { secret: "wrong" }), ENV)).status, 401);
    const old = Math.floor(Date.now() / 1000) - 600;
    assert.equal((await worker.fetch(signed(tap("fpl-3f9a1c|1|yes", "b1"), { time: old }), ENV)).status, 401);
    assert.equal(sent.length, 0);
  },

  "a browser visit gets a note, not an error": async () => {
    const response = await worker.fetch(new Request("https://fpl-bot.example.workers.dev/"), ENV);
    assert.equal(await response.text(), "FPL bot: Slack posts here.");
  },

  "fromSlack checks the signature": async () => {
    const request = signed({ type: "x" });
    const body = await request.clone().text();
    assert.equal(await fromSlack(request, body, "shh"), true);
    assert.equal(await fromSlack(request, body + "x", "shh"), false);
  },
};

let failed = 0;
for (const [name, test] of Object.entries(tests)) {
  try {
    await test();
    console.log(`ok   ${name}`);
  } catch (error) {
    failed++;
    console.log(`FAIL ${name}\n${error.stack}`);
  }
}
console.log(failed ? `${failed} failed` : `${Object.keys(tests).length} passed`);
process.exit(failed ? 1 : 0);
