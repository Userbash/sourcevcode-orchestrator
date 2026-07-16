#!/usr/bin/env node

import readline from "node:readline";
import process from "node:process";

const endpoint = process.env.CHAT_WS_URL || "ws://127.0.0.1:8010/chat/ws";
const project = process.env.CHAT_PROJECT || "external-chat";
const taskType = process.env.CHAT_TYPE || "docs";
const protocol = "chat.v1";

function usage() {
  console.error("Usage:");
  console.error("  node scripts/chat-ws-relay.mjs --message \"text\"");
  console.error("  echo \"text\" | node scripts/chat-ws-relay.mjs");
  console.error("  node scripts/chat-ws-relay.mjs --interactive");
}

function parseArgs(argv) {
  const options = { interactive: false, message: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--interactive") {
      options.interactive = true;
      continue;
    }
    if (arg === "--message") {
      options.message = argv[i + 1] || "";
      i += 1;
      continue;
    }
    if (arg === "--help" || arg === "-h") {
      usage();
      process.exit(0);
    }
  }
  return options;
}

function readStdin() {
  return new Promise((resolve) => {
    if (process.stdin.isTTY) {
      resolve("");
      return;
    }
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data.trim()));
  });
}

function buildPayload(message, requestId) {
  return {
    type: "command",
    request_id: requestId,
    action: "chat.submit",
    ack: true,
    data: {
      description: message,
      type: taskType,
      project,
    },
  };
}

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    const requestId = `chat-${Date.now()}`;
    const frames = [];
    const ws = new WebSocket(endpoint, protocol);
    const timeout = setTimeout(() => {
      try {
        ws.close();
      } catch {}
      reject(new Error(`timeout waiting for response from ${endpoint}`));
    }, 45000);

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify(buildPayload(message, requestId)));
    });

    ws.addEventListener("message", (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data.toString());
      } catch {
        frame = { raw: event.data.toString() };
      }
      frames.push(frame);
      if (frame.type === "response" && frame.request_id === requestId) {
        clearTimeout(timeout);
        try {
          ws.close();
        } catch {}
        resolve(frames);
      }
      if (frame.type === "error") {
        clearTimeout(timeout);
        try {
          ws.close();
        } catch {}
        reject(new Error(JSON.stringify(frame)));
      }
    });

    ws.addEventListener("error", () => {
      clearTimeout(timeout);
      reject(new Error(`websocket connection failed: ${endpoint}`));
    });
  });
}

function printFrames(frames) {
  for (const frame of frames) {
    console.log(JSON.stringify(frame, null, 2));
  }
}

async function interactiveLoop() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    prompt: "chat> ",
  });

  rl.prompt();
  rl.on("line", async (line) => {
    const message = line.trim();
    if (!message) {
      rl.prompt();
      return;
    }
    if (message === "/exit" || message === "/quit") {
      rl.close();
      return;
    }
    try {
      const frames = await sendMessage(message);
      printFrames(frames);
    } catch (error) {
      console.error(String(error.message || error));
    }
    rl.prompt();
  });
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const stdinMessage = await readStdin();
  const message = options.message || stdinMessage;

  if (options.interactive) {
    await interactiveLoop();
    return;
  }

  if (!message) {
    usage();
    process.exit(1);
  }

  const frames = await sendMessage(message);
  printFrames(frames);
}

main().catch((error) => {
  console.error(String(error.message || error));
  process.exit(1);
});
