// Open Brain MCP Edge Function — Supabase
// Exposes capture_thought, search_thoughts, list_thoughts, thought_stats
// over the MCP Streamable HTTP transport.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BRAIN_KEY = Deno.env.get("BRAIN_KEY") || "";

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

// --- Auth -------------------------------------------------------------------

function authenticate(req: Request): boolean {
  if (!BRAIN_KEY) return true; // no key configured = open
  const header = req.headers.get("x-brain-key") || "";
  const param = new URL(req.url).searchParams.get("brain_key") || "";
  return header === BRAIN_KEY || param === BRAIN_KEY;
}

// --- Tool implementations ---------------------------------------------------

async function capturethought(content: string, metadata: Record<string, unknown> = {}) {
  const { data, error } = await supabase
    .from("thoughts")
    .insert({ content, metadata })
    .select("id, created_at")
    .single();
  if (error) throw new Error(error.message);
  return { id: data.id, created_at: data.created_at, status: "captured" };
}

async function searchThoughts(query: string, metadataFilter: Record<string, unknown> = {}, limit = 10) {
  let q = supabase
    .from("thoughts")
    .select("id, content, metadata, created_at")
    .ilike("content", `%${query}%`)
    .order("created_at", { ascending: false })
    .limit(limit);

  if (Object.keys(metadataFilter).length > 0) {
    q = q.contains("metadata", metadataFilter);
  }

  const { data, error } = await q;
  if (error) throw new Error(error.message);
  return data;
}

async function listThoughts(limit = 20, metadataFilter: Record<string, unknown> = {}) {
  let q = supabase
    .from("thoughts")
    .select("id, content, metadata, created_at")
    .order("created_at", { ascending: false })
    .limit(limit);

  if (Object.keys(metadataFilter).length > 0) {
    q = q.contains("metadata", metadataFilter);
  }

  const { data, error } = await q;
  if (error) throw new Error(error.message);
  return data;
}

async function thoughtStats() {
  const { count } = await supabase
    .from("thoughts")
    .select("*", { count: "exact", head: true });

  const { data: newest } = await supabase
    .from("thoughts")
    .select("created_at")
    .order("created_at", { ascending: false })
    .limit(1)
    .single();

  const { data: oldest } = await supabase
    .from("thoughts")
    .select("created_at")
    .order("created_at", { ascending: true })
    .limit(1)
    .single();

  return {
    total_thoughts: count || 0,
    oldest: oldest?.created_at || null,
    newest: newest?.created_at || null,
  };
}

// --- MCP tool definitions ---------------------------------------------------

const TOOLS = [
  {
    name: "capture_thought",
    description:
      "Store a thought in Open Brain with optional metadata tags. Use metadata to tag source, user, project, etc.",
    inputSchema: {
      type: "object",
      properties: {
        content: { type: "string", description: "The thought content to store" },
        metadata: { type: "object", description: "Optional metadata tags (source, user, project, etc.)" },
      },
      required: ["content"],
    },
  },
  {
    name: "search_thoughts",
    description: "Search stored thoughts by keyword with optional metadata filter.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Keyword to search for" },
        metadata_filter: { type: "object", description: "Filter by metadata fields" },
        limit: { type: "integer", description: "Max results (default 10)", default: 10 },
      },
      required: ["query"],
    },
  },
  {
    name: "list_thoughts",
    description: "List recent thoughts, newest first, with optional metadata filter.",
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer", description: "Max results (default 20)", default: 20 },
        metadata_filter: { type: "object", description: "Filter by metadata fields" },
      },
    },
  },
  {
    name: "thought_stats",
    description: "Get overview metrics: total thoughts, date range.",
    inputSchema: { type: "object", properties: {} },
  },
];

const RESOURCES = [
  {
    uri: "open-brain://stats",
    name: "Open Brain Stats",
    description: "Current thought count and oldest/newest timestamps.",
    mimeType: "application/json",
  },
];

const RESOURCE_TEMPLATES = [
  {
    uriTemplate: "open-brain://thoughts/recent/{limit}",
    name: "Recent Thoughts",
    description: "Read the most recent thoughts with a caller-provided limit.",
    mimeType: "application/json",
  },
];

// --- REST API (for mobile apps) ---------------------------------------------

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, x-brain-key, Authorization",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

async function handleREST(req: Request, path: string): Promise<Response> {
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data, null, 2), {
      status,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });

  if (req.method === "GET" && path === "/stats") {
    return json(await thoughtStats());
  }

  if (req.method === "GET" && path === "/thoughts") {
    const url = new URL(req.url);
    const limit = parseInt(url.searchParams.get("limit") || "20");
    const filterParam = url.searchParams.get("metadata") || "{}";
    const metadataFilter = JSON.parse(filterParam);
    return json(await listThoughts(limit, metadataFilter));
  }

  if (req.method === "GET" && path === "/search") {
    const url = new URL(req.url);
    const query = url.searchParams.get("q") || "";
    const limit = parseInt(url.searchParams.get("limit") || "10");
    const filterParam = url.searchParams.get("metadata") || "{}";
    const metadataFilter = JSON.parse(filterParam);
    if (!query) return json({ error: "Missing ?q= parameter" }, 400);
    return json(await searchThoughts(query, metadataFilter, limit));
  }

  if (req.method === "POST" && path === "/thoughts") {
    const body = await req.json();
    if (!body.content) return json({ error: "Missing content field" }, 400);
    return json(await capturethought(body.content, body.metadata || {}), 201);
  }

  return json({ error: "Not found", routes: ["/stats", "/thoughts", "/search?q=", "POST /thoughts"] }, 404);
}

// --- Combined handler -------------------------------------------------------

async function handleRequest(req: Request): Promise<Response> {
  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response("", { status: 204, headers: CORS_HEADERS });
  }

  // Auth check
  if (!authenticate(req)) {
    return new Response(JSON.stringify({ error: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
    });
  }

  // Route: check Content-Type to distinguish MCP vs REST
  const contentType = req.headers.get("content-type") || "";
  const url = new URL(req.url);
  const path = url.pathname.replace(/^\/open-brain/, "").replace(/\/$/, "") || "/";

  // REST routes (GET requests, or POST to /thoughts)
  if (req.method === "GET" || (req.method === "POST" && path === "/thoughts")) {
    return handleREST(req, path);
  }

  // MCP route (POST with JSON-RPC body)
  if (req.method === "POST" && contentType.includes("application/json")) {
    const body = await req.json();

    // Detect: is this MCP (has "jsonrpc" or "method") or REST?
    if (body.jsonrpc || body.method) {
      return handleMCP_inner(body);
    }

    // REST POST fallback (e.g. {"content": "..."})
    if (body.content) {
      return new Response(
        JSON.stringify(await capturethought(body.content, body.metadata || {}), null, 2),
        { status: 201, headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
      );
    }
  }

  return new Response(JSON.stringify({ error: "Bad request" }), {
    status: 400,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

// --- MCP handler (extracted) ------------------------------------------------

async function handleMCP_inner(body: Record<string, unknown>): Promise<Response> {
  const method = body.method as string;
  const id = body.id;
  const params = body.params as Record<string, unknown> | undefined;

  let result: unknown;

  if (method === "initialize") {
    result = {
      protocolVersion: "2024-11-05",
      capabilities: {
        tools: { listChanged: false },
        resources: { subscribe: false, listChanged: false },
      },
      serverInfo: { name: "open-brain", version: "1.0.0" },
    };
  } else if (method === "tools/list") {
    result = { tools: TOOLS };
  } else if (method === "resources/list") {
    result = { resources: RESOURCES };
  } else if (method === "resources/templates/list") {
    result = { resourceTemplates: RESOURCE_TEMPLATES };
  } else if (method === "resources/read") {
    const uri = String((params as { uri?: unknown })?.uri || "");
    let resourcePayload: unknown;

    if (uri === "open-brain://stats") {
      resourcePayload = await thoughtStats();
    } else {
      const match = uri.match(/^open-brain:\/\/thoughts\/recent\/(\d+)$/);
      if (!match) return jsonrpcError(id, -32602, `Unknown resource: ${uri}`);
      resourcePayload = await listThoughts(parseInt(match[1], 10));
    }

    result = {
      contents: [
        {
          uri,
          mimeType: "application/json",
          text: JSON.stringify(resourcePayload, null, 2),
        },
      ],
    };
  } else if (method === "tools/call") {
    const toolName = (params as any)?.name;
    const args = (params as any)?.arguments || {};
    try {
      let toolResult: unknown;
      if (toolName === "capture_thought") {
        toolResult = await capturethought(args.content, args.metadata);
      } else if (toolName === "search_thoughts") {
        toolResult = await searchThoughts(args.query, args.metadata_filter, args.limit);
      } else if (toolName === "list_thoughts") {
        toolResult = await listThoughts(args.limit, args.metadata_filter);
      } else if (toolName === "thought_stats") {
        toolResult = await thoughtStats();
      } else {
        return jsonrpcError(id, -32601, `Unknown tool: ${toolName}`);
      }
      result = {
        content: [{ type: "text", text: JSON.stringify(toolResult, null, 2) }],
      };
    } catch (e) {
      result = {
        content: [{ type: "text", text: `Error: ${(e as Error).message}` }],
        isError: true,
      };
    }
  } else if (method === "notifications/initialized") {
    return new Response("", { status: 204 });
  } else {
    return jsonrpcError(id, -32601, `Unknown method: ${method}`);
  }

  return new Response(
    JSON.stringify({ jsonrpc: "2.0", id, result }),
    { headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
  );
}

function jsonrpcError(id: unknown, code: number, message: string): Response {
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }),
    { headers: { "Content-Type": "application/json", ...CORS_HEADERS } }
  );
}

Deno.serve(handleRequest);
