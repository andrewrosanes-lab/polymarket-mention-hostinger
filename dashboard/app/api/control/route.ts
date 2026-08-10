import { timingSafeEqual } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export const dynamic = "force-dynamic";

type Control = {
  paused: boolean;
  minimumConfidence: number;
  minModelEdgePct: number;
  minTimingScore: number;
  maxHoursBeforeEvent: number;
  updatedAt?: string;
};

const defaults: Control = {
  paused: false,
  minimumConfidence: 65,
  minModelEdgePct: 6,
  minTimingScore: 0,
  maxHoursBeforeEvent: 24,
};

const headers = { "Cache-Control": "no-store" };
const controlFile = () => process.env.MENTION_BOT_CONTROL_FILE || "/app/state/control.json";

function bounded(value: unknown, name: string, minimum: number, maximum: number) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`);
  }
  return parsed;
}

async function current(): Promise<Control> {
  try {
    const parsed = JSON.parse(await readFile(controlFile(), "utf8"));
    return { ...defaults, ...parsed };
  } catch {
    return defaults;
  }
}

function authorized(request: Request) {
  const expected = process.env.DASHBOARD_ADMIN_TOKEN || "";
  const supplied = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") || "";
  const suppliedBytes = Buffer.from(supplied);
  const expectedBytes = Buffer.from(expected);
  if (expected.length < 24 || suppliedBytes.length !== expectedBytes.length) return false;
  return timingSafeEqual(suppliedBytes, expectedBytes);
}

export async function GET() {
  return Response.json({
    configured: (process.env.DASHBOARD_ADMIN_TOKEN || "").length >= 24,
    control: await current(),
  }, { headers });
}

export async function POST(request: Request) {
  if (!authorized(request)) {
    return Response.json({ error: "Invalid dashboard admin token" }, { status: 401, headers });
  }
  try {
    const body = await request.json();
    const next: Control = {
      paused: Boolean(body.paused),
      minimumConfidence: bounded(body.minimumConfidence, "Confidence", 65, 90),
      minModelEdgePct: bounded(body.minModelEdgePct, "Model edge", 6, 20),
      minTimingScore: bounded(body.minTimingScore, "Timing score", 0, 90),
      maxHoursBeforeEvent: bounded(body.maxHoursBeforeEvent, "Entry window", 1, 24),
      updatedAt: new Date().toISOString(),
    };
    const path = controlFile();
    const temporary = `${path}.tmp`;
    await mkdir(dirname(path), { recursive: true });
    await writeFile(temporary, JSON.stringify(next), { encoding: "utf8", mode: 0o600 });
    await rename(temporary, path);
    return Response.json({ ok: true, control: next }, { headers });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Invalid control request";
    return Response.json({ error: message }, { status: 400, headers });
  }
}
