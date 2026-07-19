import { RappBase, RappBaseError } from "./sdk/rapp-base.js";

const elements = Object.fromEntries(
  [
    "health",
    "generation",
    "collection-count",
    "event-count",
    "generated-at",
    "collection-select",
    "collection-description",
    "raw-link",
    "pinned-link",
    "records",
    "composer",
    "operation",
    "command-collection",
    "record-id-label",
    "record-id",
    "revision-label",
    "revision",
    "data-label",
    "command-data",
    "issue-link",
    "compose-error",
    "command-preview",
    "receipt-form",
    "receipt-command-id",
    "receipt-output",
  ].map((id) => [id, document.getElementById(id)]),
);

let client;
let registry;

function inferRepository() {
  if (location.hostname.endsWith(".github.io")) {
    const owner = location.hostname.slice(0, -".github.io".length);
    const [name] = location.pathname.split("/").filter(Boolean);
    if (owner && name) return `${owner}/${name}`;
  }
  return null;
}

async function start() {
  client = new RappBase({
    baseUrl: new URL("./", location.href).href,
    repository: inferRepository() ?? undefined,
  });
  registry = await client.getRegistry();
  const status = await getJson("api/v1/status.json");
  renderStatus(status);
  populateCollections(registry.collections);
  await renderCollection(registry.collections[0]?.name);
}

async function getJson(path) {
  const response = await fetch(new URL(path, location.href), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} loading ${path}`);
  return response.json();
}

function renderStatus(status) {
  elements.health.textContent = status.healthy ? "Healthy static snapshot" : "Status unavailable";
  elements.health.classList.toggle("healthy", status.healthy === true);
  elements.generation.textContent = status.generation_sha256.slice(0, 12);
  elements.generation.title = status.generation_sha256;
  elements["collection-count"].textContent = String(status.collections);
  elements["event-count"].textContent = String(status.events);
  elements["generated-at"].textContent = status.generated_at;
  elements["generated-at"].dateTime = status.generated_at;
}

function populateCollections(collections) {
  for (const collection of collections) {
    elements["collection-select"].append(option(collection.name));
    elements["command-collection"].append(option(collection.name));
  }
  syncStarterData();
}

function option(value) {
  const element = document.createElement("option");
  element.value = value;
  element.textContent = value;
  return element;
}

async function renderCollection(name) {
  if (!name) return;
  const metadata = registry.collections.find((item) => item.name === name);
  elements["collection-description"].textContent = metadata?.description ?? "";
  setLink(elements["raw-link"], metadata?.records_url);
  setLink(elements["pinned-link"], metadata?.immutable_url);
  elements.records.replaceChildren(message("Loading records…"));
  const list = await client.collection(name).getList(
    1,
    registry.capabilities.limits.snapshot_items,
  );
  const nodes = list.items.map((record) => {
    const article = document.createElement("article");
    article.className = "record";
    const header = document.createElement("header");
    const title = document.createElement("h3");
    title.textContent = record.data?.title ?? record.data?.name ?? record.id;
    const id = document.createElement("code");
    id.textContent = record.id;
    const revision = document.createElement("code");
    revision.textContent = record.revision;
    revision.title = "Full content revision";
    header.append(title, id);
    const data = document.createElement("pre");
    data.textContent = JSON.stringify(record.data, null, 2);
    const actions = document.createElement("div");
    actions.className = "actions";
    const update = actionButton("Update", () => populateMutation(name, record, "update"));
    const remove = actionButton("Delete", () => populateMutation(name, record, "delete"));
    const copy = actionButton("Copy revision", () => {
      navigator.clipboard?.writeText?.(record.revision)?.catch(() => {});
    });
    actions.append(update, remove, copy);
    article.append(header, revision, data, actions);
    return article;
  });
  elements.records.replaceChildren(...(nodes.length ? nodes : [message("No current records.")]));
}

function actionButton(text, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = text;
  button.addEventListener("click", handler);
  return button;
}

function populateMutation(collection, record, operation) {
  elements["command-collection"].value = collection;
  elements.operation.value = operation;
  elements["record-id"].value = record.id;
  elements.revision.value = record.revision;
  elements["command-data"].value = JSON.stringify(record.data, null, 2);
  syncComposer();
  elements.composer.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setLink(element, href) {
  if (typeof href === "string") {
    element.href = href;
    element.removeAttribute("aria-disabled");
  } else {
    element.removeAttribute("href");
    element.setAttribute("aria-disabled", "true");
  }
}

function message(text) {
  const paragraph = document.createElement("p");
  paragraph.className = "muted";
  paragraph.textContent = text;
  return paragraph;
}

function syncComposer() {
  const operation = elements.operation.value;
  const hasRecord = operation !== "create";
  elements["record-id-label"].hidden = !hasRecord;
  elements["revision-label"].hidden = !hasRecord;
  elements["data-label"].hidden = operation === "delete";
  elements["issue-link"].hidden = true;
  elements["command-preview"].textContent = "";
  elements["compose-error"].textContent = "";
}

function syncStarterData() {
  const metadata = registry?.collections?.find(
    (item) => item.name === elements["command-collection"].value,
  );
  if (!metadata?.fields) return;
  const data = {};
  for (const [name, field] of Object.entries(metadata.fields)) {
    if (field.required === true) data[name] = starterValue(name, field);
  }
  elements["command-data"].value = JSON.stringify(data, null, 2);
  syncComposer();
}

function starterValue(name, field) {
  if (Array.isArray(field.enum) && field.enum.length) return field.enum[0];
  if (field.type === "string") {
    if (field.format === "url") return `https://example.com/${name}`;
    const minimum = Math.max(1, field.minLength ?? 1);
    const value = `Example ${name}`.padEnd(minimum, "x");
    return Number.isInteger(field.maxLength) ? value.slice(0, field.maxLength) : value;
  }
  if (field.type === "string[]") {
    const count = Math.max(1, field.minLength ?? 1);
    return Array.from({ length: count }, (_, index) => `${name}-${index + 1}`);
  }
  if (field.type === "boolean") return true;
  if (field.type === "integer") return Math.ceil(field.min ?? 0);
  if (field.type === "number") return field.min ?? 0;
  throw new Error(`Unsupported field type for ${name}.`);
}

function prepareCommand(event) {
  event.preventDefault();
  elements["compose-error"].textContent = "";
  elements["issue-link"].hidden = true;
  try {
    const collection = client.collection(elements["command-collection"].value);
    const operation = elements.operation.value;
    let prepared;
    if (operation === "create") {
      prepared = collection.prepareCreate(parseData());
    } else if (operation === "update") {
      prepared = collection.prepareUpdate(
        elements["record-id"].value.trim(),
        elements.revision.value.trim(),
        parseData(),
      );
    } else {
      prepared = collection.prepareDelete(
        elements["record-id"].value.trim(),
        elements.revision.value.trim(),
      );
    }
    elements["command-preview"].textContent = prepared.json;
    elements["issue-link"].href = prepared.issueUrl;
    elements["issue-link"].textContent = prepared.requiresCopy
      ? "Open form, paste JSON, then attest"
      : "Open prefilled Issue";
    elements["issue-link"].hidden = false;
    if (prepared.requiresCopy) {
      elements["compose-error"].textContent =
        "The command is too long for a safe prefilled URL. Copy the JSON above into the Command field, then check the required publication attestation.";
    }
    elements["receipt-command-id"].value = prepared.command.command_id;
  } catch (error) {
    elements["compose-error"].textContent =
      error instanceof Error ? error.message : "Could not prepare command.";
  }
}

function parseData() {
  const value = JSON.parse(elements["command-data"].value);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Data must be a JSON object.");
  }
  return value;
}

async function pollReceipt(event) {
  event.preventDefault();
  elements["receipt-output"].textContent = "Waiting for a committed receipt…";
  try {
    const receipt = await client.pollReceipt(
      elements["receipt-command-id"].value.trim(),
      {
        intervalMs: 5000,
        timeoutMs: 300000,
        onState: ({ status }) => {
          if (status === "pending") {
            elements["receipt-output"].textContent =
              "Pending. GitHub Actions may take several minutes…";
          }
        },
      },
    );
    elements["receipt-output"].textContent = JSON.stringify(receipt, null, 2);
  } catch (error) {
    const messageText =
      error instanceof RappBaseError ? `${error.code}: ${error.message}` : String(error);
    elements["receipt-output"].textContent = messageText;
  }
}

elements["collection-select"].addEventListener("change", (event) => {
  elements["command-collection"].value = event.target.value;
  syncStarterData();
  renderCollection(event.target.value).catch(showFatal);
});
elements.operation.addEventListener("change", syncComposer);
elements["command-collection"].addEventListener("change", syncStarterData);
elements.composer.addEventListener("submit", prepareCommand);
elements["receipt-form"].addEventListener("submit", pollReceipt);
syncComposer();
start().catch(showFatal);

function showFatal(error) {
  elements.health.textContent = "Explorer could not load";
  elements.health.classList.remove("healthy");
  elements.records.replaceChildren(
    message(error instanceof Error ? error.message : String(error)),
  );
}
